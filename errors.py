"""URL validation and human-readable error formatting.

Port of errors.ts. With FastMCP we signal a tool error by *raising*
``ToolError`` — the framework turns it into a CallToolResult with
``isError=True`` and the message as text, matching the old ``errorResult``.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import httpx
from mcp.server.fastmcp.exceptions import ToolError


def describe_fetch_error(err: Exception, label: str) -> str:
    """Convert network/HTTP errors into a user-readable string.

    ``label`` is the subject of the failed request (e.g. the URL).
    """
    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        if status == 404:
            return f"{label} returned 404 Not Found."
        if status in (401, 403):
            return f"{label} refused access ({status}). The site may block crawlers."
        if status == 429:
            return f"{label} rate-limited the request (429). Slow down and retry."
        if status >= 500:
            return f"{label} returned a server error ({status})."
        return f"{label} returned HTTP {status}."
    if isinstance(err, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return f"Request to {label} timed out."
    if isinstance(err, httpx.ConnectError):
        return f"Could not connect to {label}. Check the URL / your connection."
    if isinstance(err, Exception):
        return f"Error fetching {label}: {err}"
    return f"Unknown error fetching {label}: {err!r}"


def tool_error(text: str) -> ToolError:
    """Build a ToolError; raise the return value to fail a tool cleanly."""
    return ToolError(text)


def normalize_url(raw: str) -> str:
    """Validate and normalise a URL, returning an absolute http(s) URL string.

    Scheme-less input like ``example.com/page`` is upgraded to ``https://``.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise ToolError('"" is not a valid URL.')

    parsed = urlparse(candidate)
    if not parsed.scheme:
        parsed = urlparse(f"https://{candidate}")

    if parsed.scheme not in ("http", "https"):
        raise ToolError(
            f"Only http and https URLs are supported (got {parsed.scheme or '<none>'})."
        )
    if not parsed.netloc:
        raise ToolError(f'"{raw}" is not a valid URL.')

    return urlunparse(parsed)
