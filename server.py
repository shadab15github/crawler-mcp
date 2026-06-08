"""Crawler MCP server (Python / crawl4ai).

Port of server.ts to the MCP Python SDK (FastMCP), with crawl4ai as the
rendering and extraction engine. The four original tools keep identical
contracts; the rest expose crawl4ai's advanced capabilities.

Tools
-----
Ported:   fetch_page · extract_links · crawl_site · extract_by_selector
crawl4ai: fetch_clean · extract_schema · extract_llm · deep_crawl ·
          crawl_dynamic · capture_page · extract_tables

Transports: stdio (default) or streamable-http (MCP_TRANSPORT=http).
"""

from __future__ import annotations

import base64
import contextlib
import heapq
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv

load_dotenv()

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402
from pydantic import Field  # noqa: E402

import extract  # noqa: E402
import fetcher  # noqa: E402
from errors import describe_fetch_error, normalize_url, tool_error  # noqa: E402

MAX_CHARS = int(os.environ.get("CRAWLER_MAX_CHARS", "20000"))
OUTPUT_DIR = Path(os.environ.get("CRAWLER_OUTPUT_DIR", "captures"))


@contextlib.asynccontextmanager
async def _lifespan(_server: "FastMCP"):
    try:
        yield
    finally:
        await fetcher.close_crawler()


mcp = FastMCP(
    "crawler-mcp-server",
    lifespan=_lifespan,
    host=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_HTTP_PORT", "3001")),
)


# =======================
# SHARED HELPERS
# =======================

RENDER_HELP = (
    "auto=fast static fetch, fall back to headless browser if the page needs JS; "
    "static=never use a browser; browser=always render with crawl4ai (handles SPAs)."
)


def _engine_label(rendered: str) -> str:
    """Human-readable name of the fetch engine behind a `rendered` value."""
    return "crawl4ai (browser)" if rendered == "browser" else "httpx (static)"


def _fit_markdown(result: Any) -> str:
    """Pull fit-markdown (or raw markdown) from a crawl4ai result, version-safe."""
    md = getattr(result, "markdown", None)
    if md is None:
        return ""
    for attr in ("fit_markdown", "raw_markdown"):
        val = getattr(md, attr, None)
        if val:
            return val
    return str(md)


# =======================
# TOOL 1: FETCH PAGE  (ported)
# =======================

@mcp.tool()
async def fetch_page(
    url: Annotated[str, Field(description="Page URL (http/https). Scheme-less input like `example.com` is allowed.")],
    format: Annotated[Literal["markdown", "text", "html"], Field(description="Output format for the page content.")] = "markdown",
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
    max_chars: Annotated[Optional[int], Field(description=f"Cap on returned characters (default {MAX_CHARS}).", ge=1, le=200000)] = None,
) -> str:
    """Fetch a single web page and return its readable content as Markdown, plain
    text, or raw HTML. Automatically renders JavaScript-heavy pages with a
    headless browser when needed."""
    target = normalize_url(url)
    try:
        page = await fetcher.fetch_html(target, render)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    title = extract.extract_title(page.html)
    if format == "html":
        body = page.html
    elif format == "text":
        body = extract.html_to_text(page.html)
    else:
        body = extract.html_to_markdown(page.html)

    limit = max_chars or MAX_CHARS
    header = (
        f"# {title or '(untitled)'}\n"
        f"URL: {page.final_url}\n"
        f"Status: {page.status} · Engine: {_engine_label(page.rendered)}\n\n---\n\n"
    )
    return header + extract.truncate(body, limit)


# =======================
# TOOL 2: EXTRACT LINKS  (ported)
# =======================

@mcp.tool()
async def extract_links(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    same_domain: Annotated[bool, Field(description="Only return links pointing to the same hostname as the page.")] = False,
    limit: Annotated[int, Field(description="Maximum number of links to return.", ge=1, le=500)] = 100,
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
) -> str:
    """Extract all hyperlinks from a web page, resolved to absolute URLs.
    Optionally restrict to links on the same domain."""
    target = normalize_url(url)
    try:
        page = await fetcher.fetch_html(target, render)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    links = extract.extract_links(page.html, page.final_url, same_domain=same_domain, limit=limit)
    if not links:
        return f"No links found on {page.final_url}."

    body = "\n".join(f"{i + 1}. {l.text or '(no text)'}\n   {l.href}" for i, l in enumerate(links))
    plural = "" if len(links) == 1 else "s"
    return f"Found {len(links)} link{plural} on {page.final_url} (Engine: {_engine_label(page.rendered)}):\n\n{body}"


# =======================
# TOOL 3: CRAWL SITE  (ported)
# =======================

@mcp.tool()
async def crawl_site(
    start_url: Annotated[str, Field(description="Page URL to start crawling from (http/https).")],
    max_depth: Annotated[int, Field(description="How many link-hops to follow from the start URL (0 = only the start page).", ge=0, le=3)] = 1,
    max_pages: Annotated[int, Field(description="Hard cap on the total number of pages fetched.", ge=1, le=50)] = 10,
    same_domain: Annotated[bool, Field(description="Only follow links on the start URL's hostname.")] = True,
    format: Annotated[Literal["markdown", "text"], Field(description="Output format for each page's content summary.")] = "markdown",
    chars_per_page: Annotated[int, Field(description="Characters of content to include per page.", ge=1, le=20000)] = 2000,
    delay_ms: Annotated[int, Field(description="Politeness delay between requests, in milliseconds.", ge=0, le=10000)] = 250,
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
) -> str:
    """Recursively crawl a website starting from a URL, following links up to a
    maximum depth and page count. Returns a short content summary for each page
    visited. Stays on the same domain by default."""
    import asyncio

    start = normalize_url(start_url)
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(start, 0)]
    results: list[str] = []
    first_error: Optional[str] = None

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        canonical = url.split("#")[0]
        if canonical in visited:
            continue
        visited.add(canonical)

        try:
            page = await fetcher.fetch_html(canonical, render)
            title = extract.extract_title(page.html)
            content = (
                extract.html_to_text(page.html)
                if format == "text"
                else extract.html_to_markdown(page.html)
            )
            results.append(
                f"## [depth {depth}] {title or '(untitled)'}\n"
                f"{page.final_url} (status {page.status}, {_engine_label(page.rendered)})\n\n"
                + extract.truncate(content, chars_per_page)
            )
            if depth < max_depth and len(visited) < max_pages:
                for l in extract.extract_links(page.html, page.final_url, same_domain=same_domain):
                    c = l.href.split("#")[0]
                    if c not in visited:
                        queue.append((c, depth + 1))
        except Exception as err:
            msg = describe_fetch_error(err, canonical)
            first_error = first_error or msg
            results.append(f"## [depth {depth}] FAILED\n{canonical}\n\n{msg}")

        if delay_ms > 0 and queue and len(visited) < max_pages:
            await asyncio.sleep(delay_ms / 1000.0)

    if not results:
        raise tool_error(first_error or f"Nothing crawled from {start}.")

    plural = "" if len(visited) == 1 else "s"
    scope = "same-domain" if same_domain else "cross-domain"
    header = (
        f"Crawled {len(visited)} page{plural} from {start} "
        f"(max depth {max_depth}, {scope}):\n\n"
    )
    return header + "\n\n---\n\n".join(results)


# =======================
# TOOL 4: EXTRACT BY SELECTOR  (ported)
# =======================

@mcp.tool()
async def extract_by_selector(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    selector: Annotated[str, Field(description="CSS selector, e.g. 'h2', '.price', 'table tr td', 'a.result-link'.", min_length=1, max_length=500)],
    attribute: Annotated[Optional[str], Field(description="Return this attribute instead of text (e.g. 'href', 'src', 'data-id').", max_length=100)] = None,
    limit: Annotated[int, Field(description="Maximum number of matches to return.", ge=1, le=1000)] = 100,
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
) -> str:
    """Extract specific data from a page using a CSS selector. Returns each
    matching element's text, or an attribute value when `attribute` is given
    (e.g. selector='a.product', attribute='href')."""
    target = normalize_url(url)
    try:
        page = await fetcher.fetch_html(target, render)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    matches = extract.extract_by_selector(
        page.html, selector, attribute=attribute, limit=limit, base_url=page.final_url
    )
    if not matches:
        return f"No elements matched selector `{selector}` on {page.final_url}."

    what = f"[{attribute}]" if attribute else "text"
    body = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(matches))
    plural = "" if len(matches) == 1 else "s"
    return f"Matched {len(matches)} element{plural} for `{selector}` {what} on {page.final_url} (Engine: {_engine_label(page.rendered)}):\n\n{body}"


# =======================
# TOOL 5: FETCH CLEAN  (crawl4ai fit-markdown)
# =======================

@mcp.tool()
async def fetch_clean(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    query: Annotated[Optional[str], Field(description="If given, keep only sections relevant to this query (BM25 filtering); otherwise prune boilerplate (nav/ads/footers).")] = None,
    max_chars: Annotated[Optional[int], Field(description=f"Cap on returned characters (default {MAX_CHARS}).", ge=1, le=200000)] = None,
) -> str:
    """Fetch a page and return clean 'fit markdown' with boilerplate (nav, ads,
    footers, cookie banners) removed via crawl4ai content filtering. Pass `query`
    to keep only the sections relevant to a topic. Best for feeding article-like
    content to an LLM with minimal token waste."""
    target = normalize_url(url)
    try:
        from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        content_filter = (
            BM25ContentFilter(user_query=query) if query else PruningContentFilter()
        )
        md_gen = DefaultMarkdownGenerator(content_filter=content_filter)
        result = await fetcher.crawl_page(target, markdown_generator=md_gen)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    title = (getattr(result, "metadata", None) or {}).get("title") or extract.extract_title(result.html or "")
    final_url = getattr(result, "url", None) or target
    body = _fit_markdown(result)
    limit = max_chars or MAX_CHARS
    focus = f"\nFocus: {query}" if query else ""
    header = f"# {title or '(untitled)'}\nURL: {final_url}\nEngine: crawl4ai (browser){focus}\n\n---\n\n"
    return header + extract.truncate(body, limit)


# =======================
# TOOL 6: EXTRACT SCHEMA  (crawl4ai JsonCssExtractionStrategy)
# =======================

@mcp.tool()
async def extract_schema(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    schema: Annotated[dict, Field(description=(
        "crawl4ai JsonCss schema. Shape: {\"name\": str, \"baseSelector\": <CSS for each record>, "
        "\"fields\": [{\"name\": str, \"selector\": <CSS>, \"type\": \"text\"|\"attribute\"|\"html\", "
        "\"attribute\": <attr name if type=attribute>}]}. Returns one JSON object per matched record."
    ))],
) -> str:
    """Extract structured, multi-field records from a page using a declarative
    CSS schema (crawl4ai JsonCssExtractionStrategy). Returns a JSON array of
    objects — ideal for product listings, search results, and tables where one
    selector per call (extract_by_selector) is too limited."""
    target = normalize_url(url)
    try:
        from crawl4ai import JsonCssExtractionStrategy

        strategy = JsonCssExtractionStrategy(schema, verbose=False)
        result = await fetcher.crawl_page(target, extraction_strategy=strategy)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    raw = getattr(result, "extracted_content", None)
    if not raw:
        return f"No records matched the schema on {getattr(result, 'url', target)}."
    try:
        records = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    count = len(records) if isinstance(records, list) else 1
    return f"Extracted {count} record(s) from {getattr(result, 'url', target)} (Engine: crawl4ai (browser) · JsonCssExtractionStrategy):\n\n```json\n{json.dumps(records, indent=2, ensure_ascii=False)}\n```"


# =======================
# TOOL 7: EXTRACT LLM  (crawl4ai LLMExtractionStrategy)
# =======================

@mcp.tool()
async def extract_llm(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    instruction: Annotated[str, Field(description="Natural-language extraction instruction, e.g. 'Extract each job posting's title, salary, and location.'", min_length=1)],
    schema: Annotated[Optional[dict], Field(description="Optional JSON schema describing the desired output object shape.")] = None,
) -> str:
    """Extract structured data from a page using an LLM (crawl4ai
    LLMExtractionStrategy). Works on messy pages where CSS selectors break.
    Requires CRAWLER_LLM_PROVIDER (e.g. 'openai/gpt-4o-mini') and the matching
    API key to be configured in the environment."""
    target = normalize_url(url)
    provider = os.environ.get("CRAWLER_LLM_PROVIDER")
    if not provider:
        raise tool_error(
            "LLM extraction is not configured. Set CRAWLER_LLM_PROVIDER "
            "(e.g. 'openai/gpt-4o-mini') and the provider's API key in your .env."
        )
    token = os.environ.get("CRAWLER_LLM_API_TOKEN")

    try:
        from crawl4ai import LLMExtractionStrategy

        kwargs: dict[str, Any] = {"instruction": instruction}
        if schema:
            kwargs["schema"] = schema
            kwargs["extraction_type"] = "schema"
        try:  # newer crawl4ai: provider/token live on LLMConfig
            from crawl4ai import LLMConfig

            strategy = LLMExtractionStrategy(
                llm_config=LLMConfig(provider=provider, api_token=token), **kwargs
            )
        except ImportError:  # older crawl4ai: kwargs on the strategy
            strategy = LLMExtractionStrategy(provider=provider, api_token=token, **kwargs)

        result = await fetcher.crawl_page(target, extraction_strategy=strategy)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    raw = getattr(result, "extracted_content", None)
    if not raw:
        return f"The LLM extracted nothing from {getattr(result, 'url', target)}."
    return f"LLM extraction from {getattr(result, 'url', target)} (Engine: crawl4ai (browser) · LLMExtractionStrategy via {provider}):\n\n```json\n{raw}\n```"


# =======================
# TOOL 8: DEEP CRAWL  (best-first, relevance-scored)
# =======================

def _relevance(text: str, keywords: list[str]) -> int:
    if not keywords:
        return 0
    low = text.lower()
    return sum(low.count(k) for k in keywords)


@mcp.tool()
async def deep_crawl(
    start_url: Annotated[str, Field(description="Page URL to start crawling from (http/https).")],
    keywords: Annotated[list[str], Field(description="Keywords used to score and prioritise which links to follow first (best-first). Empty = breadth-first.")] = [],
    max_depth: Annotated[int, Field(description="Maximum link-hops from the start URL.", ge=0, le=4)] = 2,
    max_pages: Annotated[int, Field(description="Hard cap on pages fetched.", ge=1, le=60)] = 15,
    same_domain: Annotated[bool, Field(description="Only follow links on the start URL's hostname.")] = True,
    chars_per_page: Annotated[int, Field(description="Characters of content to include per page.", ge=1, le=20000)] = 1500,
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
) -> str:
    """Goal-directed crawl: follows the most relevant links first using a
    best-first strategy scored by `keywords`, instead of blind breadth-first
    fan-out (crawl_site). Good for 'find the pages about X on this site'."""
    import asyncio

    start = normalize_url(start_url)
    kw = [k.lower() for k in keywords if k.strip()]
    visited: set[str] = set()
    results: list[str] = []
    counter = 0
    # Max-heap via negative score: (-score, depth, tiebreak, url)
    heap: list[tuple[int, int, int, str]] = [(0, 0, 0, start)]

    while heap and len(visited) < max_pages:
        neg_score, depth, _, url = heapq.heappop(heap)
        canonical = url.split("#")[0]
        if canonical in visited:
            continue
        visited.add(canonical)

        try:
            page = await fetcher.fetch_html(canonical, render)
            title = extract.extract_title(page.html)
            content = extract.html_to_markdown(page.html)
            score = -neg_score
            results.append(
                f"## [depth {depth} · score {score}] {title or '(untitled)'}\n"
                f"{page.final_url} ({_engine_label(page.rendered)})\n\n"
                + extract.truncate(content, chars_per_page)
            )
            if depth < max_depth and len(visited) < max_pages:
                for l in extract.extract_links(page.html, page.final_url, same_domain=same_domain):
                    c = l.href.split("#")[0]
                    if c in visited:
                        continue
                    s = _relevance(f"{l.text} {l.href}", kw)
                    counter += 1
                    heapq.heappush(heap, (-s, depth + 1, counter, c))
        except Exception as err:
            results.append(f"## [depth {depth}] FAILED\n{canonical}\n\n{describe_fetch_error(err, canonical)}")

        if heap and len(visited) < max_pages:
            await asyncio.sleep(0.2)

    if not results:
        raise tool_error(f"Nothing crawled from {start}.")
    strat = f"best-first on {kw}" if kw else "breadth-first"
    return f"Deep-crawled {len(visited)} page(s) from {start} ({strat}):\n\n" + "\n\n---\n\n".join(results)


# =======================
# TOOL 9: CRAWL DYNAMIC  (JS actions / SPA interaction)
# =======================

@mcp.tool()
async def crawl_dynamic(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    js_code: Annotated[list[str], Field(description="JavaScript snippets to run in the page, in order — e.g. click 'load more', scroll to bottom. Example: [\"document.querySelector('button.more')?.click()\", \"window.scrollTo(0, document.body.scrollHeight)\"]")] = [],
    wait_for: Annotated[Optional[str], Field(description="CSS selector to wait for, or a JS predicate prefixed with 'js:' (e.g. 'js:() => document.querySelectorAll(\".item\").length > 20').")] = None,
    max_chars: Annotated[Optional[int], Field(description=f"Cap on returned characters (default {MAX_CHARS}).", ge=1, le=200000)] = None,
) -> str:
    """Render a dynamic page, run JavaScript actions (clicks, scrolling for
    infinite-scroll, 'load more'), optionally wait for a condition, then return
    the resulting clean markdown. Handles SPAs and lazy-loaded content that a
    plain fetch can't reach."""
    target = normalize_url(url)
    try:
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        md_gen = DefaultMarkdownGenerator(content_filter=PruningContentFilter())
        result = await fetcher.crawl_page(
            target,
            js_code=js_code or None,
            wait_for=wait_for,
            markdown_generator=md_gen,
        )
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    final_url = getattr(result, "url", None) or target
    body = _fit_markdown(result)
    limit = max_chars or MAX_CHARS
    ran = f"\nRan {len(js_code)} JS action(s)." if js_code else ""
    header = f"URL: {final_url}\nEngine: crawl4ai (browser){ran}\n\n---\n\n"
    return header + extract.truncate(body, limit)


# =======================
# TOOL 10: CAPTURE PAGE  (screenshot / PDF)
# =======================

@mcp.tool()
async def capture_page(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    format: Annotated[Literal["png", "pdf"], Field(description="'png' returns a full-page screenshot image; 'pdf' renders the page to a PDF file on disk and returns its path.")] = "png",
):
    """Capture a full-page screenshot (PNG, returned as an image) or render the
    page to PDF (saved under CRAWLER_OUTPUT_DIR, path returned). Uses the already
    open crawl4ai browser."""
    target = normalize_url(url)
    try:
        result = await fetcher.crawl_page(
            target,
            screenshot=(format == "png"),
            pdf=(format == "pdf"),
        )
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    if format == "png":
        shot = getattr(result, "screenshot", None)
        if not shot:
            raise tool_error(f"crawl4ai returned no screenshot for {target}.")
        data = base64.b64decode(shot) if isinstance(shot, str) else shot
        return Image(data=data, format="png")

    pdf_bytes = getattr(result, "pdf", None)
    if not pdf_bytes:
        raise tool_error(f"crawl4ai returned no PDF for {target}.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", target).strip("-")[:80] or "page"
    out_path = OUTPUT_DIR / f"{slug}.pdf"
    out_path.write_bytes(pdf_bytes if isinstance(pdf_bytes, bytes) else base64.b64decode(pdf_bytes))
    return f"Saved PDF of {getattr(result, 'url', target)} to {out_path.resolve()} (Engine: crawl4ai (browser))"


# =======================
# TOOL 11: EXTRACT TABLES
# =======================

@mcp.tool()
async def extract_tables(
    url: Annotated[str, Field(description="Page URL (http/https).")],
    limit: Annotated[int, Field(description="Maximum number of tables to return.", ge=1, le=50)] = 10,
    render: Annotated[Literal["auto", "static", "browser"], Field(description=RENDER_HELP)] = "auto",
) -> str:
    """Extract all HTML tables from a page and return them as Markdown tables,
    ready for analysis or conversion to CSV."""
    target = normalize_url(url)
    try:
        page = await fetcher.fetch_html(target, render)
    except Exception as err:
        raise tool_error(describe_fetch_error(err, target))

    tables = extract.extract_tables(page.html, limit=limit)
    if not tables:
        return f"No tables found on {page.final_url}."
    plural = "" if len(tables) == 1 else "s"
    blocks = "\n\n".join(f"### Table {i + 1}\n\n{t}" for i, t in enumerate(tables))
    return f"Found {len(tables)} table{plural} on {page.final_url} (Engine: {_engine_label(page.rendered)}):\n\n{blocks}"


# =======================
# HEALTH ENDPOINT (HTTP mode)
# =======================

with contextlib.suppress(Exception):
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_req: "Request") -> "JSONResponse":
        return JSONResponse({"status": "ok", "server": "crawler-mcp-server"})


# =======================
# START SERVER
# =======================

def main() -> None:
    import sys

    # CLI flags override MCP_TRANSPORT so npm/run scripts stay cross-platform.
    argv = sys.argv[1:]
    if "--http" in argv:
        mode = "http"
    elif "--stdio" in argv:
        mode = "stdio"
    else:
        mode = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if mode == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
