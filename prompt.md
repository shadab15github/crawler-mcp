# Crawler MCP — Test Prompts

One (or more) prompt per tool. The `*.toscrape.com` sites are purpose-built for
scraping, so they're safe and stable targets to test against.

## Tools

### `fetch_page`
```
Using crawler-mcp, fetch https://example.com and give me the content as markdown.
```
```
Using crawler-mcp, read https://quotes.toscrape.com and summarize what the page contains.
```
```
Using crawler-mcp, get the raw HTML of https://example.com (use static render).
```

### `extract_links`
```
Using crawler-mcp, list all the links on https://quotes.toscrape.com.
```
```
Using crawler-mcp, extract only the same-domain links from https://books.toscrape.com (limit 20).
```

### `extract_by_selector`
```
Using crawler-mcp, from https://quotes.toscrape.com extract the text of every element matching ".quote .text".
```
```
Using crawler-mcp, on https://quotes.toscrape.com extract the href attribute of every "a" inside ".tags".
```
```
Using crawler-mcp, get all h1 headings from https://example.com.
```

### `crawl_site`
```
Using crawler-mcp, crawl https://quotes.toscrape.com at depth 1, max 5 pages, same domain only.
```
```
Using crawler-mcp, crawl https://books.toscrape.com to depth 2 with max 10 pages and tell me what kind of site it is.
```

---

## Optional flags

- **Render mode** — append `render=static` (never use a browser), `render=browser`
  (always render JS with Playwright), or omit for `auto` (static first, browser if needed).
- **Output format** — `fetch_page` accepts `format=markdown` (default), `format=text`, or `format=html`.
- **Limits** — `fetch_page` takes `max_chars`; `crawl_site` takes `max_depth` (≤3),
  `max_pages` (≤50), `chars_per_page`, and `delay_ms` (politeness delay).
- **Cache** — refetching the same URL within 5 minutes returns the cached page.

## JS-rendered sites (requires Playwright)

Install the browser once:
```
npx playwright install chromium
```
Then:
```
Using crawler-mcp, fetch https://react.dev using browser render and tell me the main headline.
```
If Playwright isn't installed, `render=auto` still works for static sites and
`render=browser` returns a clear "install Playwright" error.

## Quick smoke test

```
Using crawler-mcp, fetch example.com as markdown, then list its links.
```

## Running the server

```
npm start             # stdio transport (default) — for Claude Desktop
npm run start:http    # HTTP transport on MCP_HTTP_PORT (default 3001)
npm run typecheck     # tsc --noEmit
```

## Error-handling checks

```
Using crawler-mcp, fetch "not a real url ::: broken" and tell me what happens.
```
```
Using crawler-mcp, fetch https://httpstat.us/404 and report the status.
```
