"""Fetching engine.

Two paths, mirroring the original fetcher.ts but with crawl4ai as the browser:

* **static**  — a fast, no-browser fetch with ``httpx`` (replaces axios).
* **browser** — crawl4ai's ``AsyncWebCrawler`` (replaces raw Playwright) which
  also unlocks fit-markdown, link/media graphs, screenshots, PDF, JS actions,
  and structured/LLM extraction used by the advanced tools.

``auto`` tries static first and transparently upgrades to the browser when the
page looks like it needs JavaScript.

A single crawl4ai browser is started lazily and reused across calls (browser
pooling), which is far cheaper than launching Playwright per request.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx
from bs4 import BeautifulSoup

RenderMode = Literal["auto", "static", "browser"]

USER_AGENT = os.environ.get(
    "CRAWLER_USER_AGENT",
    "crawler-mcp/2.0 (+https://github.com/your-org/crawler-mcp)",
)
TIMEOUT_MS = int(os.environ.get("CRAWLER_TIMEOUT_MS", "15000"))
TIMEOUT_S = TIMEOUT_MS / 1000.0
FETCH_TTL_S = 5 * 60  # pages don't change second-to-second


@dataclass
class FetchResult:
    html: str
    final_url: str           # URL after following redirects
    status: int
    rendered: Literal["static", "browser"]


# ---------------------------------------------------------------------------
# Rendering heuristic (port of needsRendering)
# ---------------------------------------------------------------------------

_SPA_ROOTS = ["#root", "#app", "#__next", "[data-reactroot]", "#__nuxt"]


def needs_rendering(html: str) -> bool:
    """Guess whether a statically-fetched page needs a real browser."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    body = soup.body
    visible = " ".join((body.get_text(" ") if body else "").split())

    if len(visible) >= 400:
        return False

    has_empty_spa_root = False
    for sel in _SPA_ROOTS:
        el = soup.select_one(sel)
        if el is not None and len(el.get_text().strip()) < 50:
            has_empty_spa_root = True
            break

    mentions_js = bool(
        re.search(r"enable javascript|requires javascript", html or "", flags=re.I)
    )
    return has_empty_spa_root or mentions_js or len(visible) < 200


# ---------------------------------------------------------------------------
# Static path (httpx)
# ---------------------------------------------------------------------------

async def fetch_static(url: str) -> FetchResult:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT_S, headers=headers
    ) as client:
        resp = await client.get(url)

    # Match axios validateStatus: surface 5xx as errors, return 2xx/3xx/4xx.
    if resp.status_code >= 500:
        resp.raise_for_status()

    return FetchResult(
        html=resp.text,
        final_url=str(resp.url),
        status=resp.status_code,
        rendered="static",
    )


# ---------------------------------------------------------------------------
# Browser path (crawl4ai) — lazily started shared instance
# ---------------------------------------------------------------------------

_crawler: Optional[Any] = None
_crawler_lock = asyncio.Lock()


async def get_crawler():
    """Return a started, reusable crawl4ai AsyncWebCrawler (browser pool)."""
    global _crawler
    if _crawler is not None:
        return _crawler
    async with _crawler_lock:
        if _crawler is not None:
            return _crawler
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Browser rendering required, but crawl4ai is not installed. "
                "Run `pip install crawl4ai` then `crawl4ai-setup` (installs the "
                "headless browser)."
            ) from exc

        browser_cfg = BrowserConfig(headless=True, user_agent=USER_AGENT, verbose=False)
        crawler = AsyncWebCrawler(config=browser_cfg)
        await crawler.start()
        _crawler = crawler
        return _crawler


async def close_crawler() -> None:
    global _crawler
    if _crawler is not None:
        try:
            await _crawler.close()
        finally:
            _crawler = None


def _run_config(**overrides):
    """Build a CrawlerRunConfig with our defaults + per-call overrides."""
    from crawl4ai import CacheMode, CrawlerRunConfig

    params: dict[str, Any] = dict(
        cache_mode=CacheMode.BYPASS,  # we manage our own TTL cache
        page_timeout=TIMEOUT_MS,
        wait_until="networkidle",
        verbose=False,
    )
    params.update({k: v for k, v in overrides.items() if v is not None})
    return CrawlerRunConfig(**params)


async def crawl_page(url: str, **overrides):
    """Run one crawl4ai pass and return the raw CrawlResult.

    ``overrides`` are forwarded to CrawlerRunConfig — e.g. ``css_selector``,
    ``markdown_generator``, ``extraction_strategy``, ``js_code``, ``wait_for``,
    ``screenshot=True``, ``pdf=True``, ``session_id``.
    """
    crawler = await get_crawler()
    result = await crawler.arun(url=url, config=_run_config(**overrides))
    if not getattr(result, "success", True):
        raise RuntimeError(getattr(result, "error_message", None) or f"crawl4ai failed for {url}")
    return result


async def fetch_browser(url: str) -> FetchResult:
    result = await crawl_page(url)
    return FetchResult(
        html=result.html or "",
        final_url=getattr(result, "url", None) or url,
        status=getattr(result, "status_code", None) or 200,
        rendered="browser",
    )


# ---------------------------------------------------------------------------
# Public entry point (port of fetchHtml) with TTL cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    data: FetchResult
    expires_at: float


_cache: dict[str, _CacheEntry] = {}


async def fetch_html(url: str, mode: RenderMode = "auto") -> FetchResult:
    key = f"{mode}|{url}"
    entry = _cache.get(key)
    if entry and time.monotonic() < entry.expires_at:
        return entry.data

    data = await _fetch_html_uncached(url, mode)
    _cache[key] = _CacheEntry(data=data, expires_at=time.monotonic() + FETCH_TTL_S)
    return data


async def _fetch_html_uncached(url: str, mode: RenderMode) -> FetchResult:
    if mode == "browser":
        return await fetch_browser(url)

    static_result = await fetch_static(url)
    if mode == "static":
        return static_result

    # mode == "auto"
    if needs_rendering(static_result.html):
        try:
            return await fetch_browser(static_result.final_url)
        except Exception:
            # crawl4ai unavailable/failed — return what was server-rendered.
            return static_result
    return static_result
