"""HTML extraction helpers — port of extract.ts using BeautifulSoup.

crawl4ai already produces good markdown and a classified link graph, but these
helpers keep the *original tools' behaviour* identical (cheerio/turndown parity)
and add a dependency-free HTML table extractor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_WS = re.compile(r"\s+")
_BLANKS = re.compile(r"\n{3,}")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


@dataclass
class PageLink:
    text: str
    href: str


def extract_title(html: str) -> str:
    """Document title, falling back to og:title."""
    soup = _soup(html)
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title:
            return title
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def html_to_markdown(html: str) -> str:
    """Convert a page's main content to Markdown.

    Mirrors the turndown path: strip noise, prefer <main>/<article>, fall back
    to <body>. Uses a lightweight in-house HTML→Markdown pass so the basic tools
    don't depend on crawl4ai's browser pipeline.
    """
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "header", "footer"]):
        tag.decompose()

    container = soup.find("main") or soup.find("article") or soup.body or soup
    md = _node_to_markdown(container).strip()
    return _BLANKS.sub("\n\n", md)


def html_to_text(html: str) -> str:
    """Plain readable text of a page."""
    soup = _soup(html)
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    body = soup.body or soup
    text = body.get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def extract_links(
    html: str,
    base_url: str,
    *,
    same_domain: bool = False,
    limit: int | None = None,
) -> list[PageLink]:
    """All hyperlinks resolved to absolute http(s) URLs, de-duplicated."""
    soup = _soup(html)
    base_host = urlparse(base_url).hostname or ""
    seen: set[str] = set()
    links: list[PageLink] = []

    for a in soup.select("a[href]"):
        raw = (a.get("href") or "").strip()
        if not raw or re.match(r"^(javascript:|mailto:|tel:|#)", raw, re.I):
            continue
        try:
            abs_url = urljoin(base_url, raw)
        except ValueError:
            continue
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if same_domain and parsed.hostname != base_host:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        text = _WS.sub(" ", a.get_text(" ")).strip()
        links.append(PageLink(text=text, href=abs_url))

    return links[:limit] if limit else links


def extract_by_selector(
    html: str,
    selector: str,
    *,
    attribute: str | None = None,
    limit: int | None = None,
    base_url: str | None = None,
) -> list[str]:
    """Values matching a CSS selector: element text, or an attribute value."""
    soup = _soup(html)
    out: list[str] = []

    for el in soup.select(selector):
        if limit and len(out) >= limit:
            break
        if attribute:
            val = el.get(attribute)
            if val is None:
                continue
            if isinstance(val, list):  # e.g. class="" returns a list
                val = " ".join(val)
            if base_url and attribute.lower() in ("href", "src"):
                try:
                    val = urljoin(base_url, val)
                except ValueError:
                    pass
            out.append(val)
        else:
            text = _WS.sub(" ", el.get_text(" ")).strip()
            if text:
                out.append(text)

    return out


def extract_tables(html: str, *, limit: int | None = None) -> list[str]:
    """Extract HTML <table> elements as GitHub-flavoured Markdown tables."""
    soup = _soup(html)
    tables: list[str] = []

    for table in soup.find_all("table"):
        if limit and len(tables) >= limit:
            break
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            rows.append([_WS.sub(" ", c.get_text(" ")).strip() for c in cells])
        if not rows:
            continue

        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        header, *body = rows
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * width) + " |",
        ]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        tables.append("\n".join(lines))

    return tables


def truncate(text: str, max_chars: int) -> str:
    """Truncate long content and append a clear notice when cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n…[truncated {len(text) - max_chars} more characters]"


# ---------------------------------------------------------------------------
# Minimal HTML → Markdown converter (turndown stand-in for the basic tools)
# ---------------------------------------------------------------------------

_BLOCK = {"p", "div", "section", "ul", "ol", "table", "blockquote", "pre"}


def _node_to_markdown(node) -> str:
    from bs4 import NavigableString, Tag

    parts: list[str] = []

    def walk(n, list_depth=0, ordered=False, index=1):
        if isinstance(n, NavigableString):
            text = _WS.sub(" ", str(n))
            if text.strip():
                parts.append(text)
            return
        if not isinstance(n, Tag):
            return

        name = n.name.lower()
        if name in ("script", "style", "noscript"):
            return

        if re.fullmatch(r"h[1-6]", name):
            level = int(name[1])
            parts.append("\n\n" + "#" * level + " " + _inline(n) + "\n\n")
            return
        if name == "br":
            parts.append("  \n")
            return
        if name == "hr":
            parts.append("\n\n---\n\n")
            return
        if name == "a":
            href = n.get("href", "")
            txt = _inline(n) or href
            parts.append(f"[{txt}]({href})" if href else txt)
            return
        if name in ("strong", "b"):
            parts.append(f"**{_inline(n)}**")
            return
        if name in ("em", "i"):
            parts.append(f"*{_inline(n)}*")
            return
        if name == "code" and (n.parent is None or n.parent.name != "pre"):
            parts.append(f"`{_inline(n)}`")
            return
        if name == "pre":
            parts.append("\n\n```\n" + n.get_text() + "\n```\n\n")
            return
        if name == "li":
            bullet = f"{index}. " if ordered else "- "
            parts.append("\n" + "  " * list_depth + bullet + _inline(n))
            return
        if name in ("ul", "ol"):
            parts.append("\n")
            i = 1
            for child in n.find_all("li", recursive=False):
                walk(child, list_depth + 1, ordered=(name == "ol"), index=i)
                i += 1
            parts.append("\n")
            return
        if name == "img":
            alt = n.get("alt", "")
            src = n.get("src", "")
            if src:
                parts.append(f"![{alt}]({src})")
            return

        # Generic container: recurse, add spacing for block-level elements.
        if name in _BLOCK:
            parts.append("\n\n")
        for child in n.children:
            walk(child, list_depth, ordered, index)
        if name in _BLOCK:
            parts.append("\n\n")

    def _inline(n) -> str:
        return _WS.sub(" ", n.get_text(" ")).strip()

    walk(node)
    return "".join(parts)
