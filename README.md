# crawler-mcp

A web-crawler MCP server, built in the same style as `weather-mcp`
(TypeScript + `@modelcontextprotocol/sdk` + Express + Zod).

It fetches pages with a fast **axios + cheerio** path and automatically falls
back to a **Playwright** headless browser when a page needs JavaScript to
render. Designed to be used as a **Claude connector** (local stdio or remote
HTTP).

## Tools

| Tool | What it does |
|------|--------------|
| `fetch_page` | Fetch one URL and return its content as Markdown, text, or raw HTML. |
| `extract_links` | List all hyperlinks on a page (absolute URLs, optional same-domain filter). |
| `crawl_site` | Recursively crawl from a start URL up to a max depth/page count. |
| `extract_by_selector` | Pull specific data via a CSS selector (text or an attribute). |

Each tool accepts a `render` option: `auto` (default — static first, browser if
needed), `static` (never use a browser), or `browser` (always render with
Playwright).

## Setup

```bash
cd crawler-mcp
npm install

# Optional — only needed for JavaScript-heavy / SPA sites:
npx playwright install chromium

cp .env.example .env   # then edit if you want
```

## Run

```bash
# Local (stdio) — for Claude Desktop:
npm start

# Remote (HTTP) — for a custom/remote connector:
npm run start:http     # serves http://localhost:3001/mcp
```

## Use it as a Claude connector

### Claude Desktop (local, stdio)

Add to your `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "crawler": {
      "command": "npx",
      "args": ["tsx", "D:\\MCP\\crawler-mcp\\server.ts"]
    }
  }
}
```

Restart Claude Desktop. The four crawler tools appear under the connectors menu.

### Remote HTTP connector (claude.ai / Desktop "Add custom connector")

1. Start the server in HTTP mode and expose it over HTTPS (e.g. behind a reverse
   proxy or a tunnel such as `cloudflared` / `ngrok`):
   ```bash
   npm run start:http
   ```
2. In Claude → **Settings → Connectors → Add custom connector**, enter the URL:
   ```
   https://your-host/mcp
   ```

> Remote connectors on claude.ai generally require a public **HTTPS** URL.
> `http://localhost:3001/mcp` works for local testing tools but not for the
> hosted claude.ai web app.

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `MCP_HTTP_PORT` | `3001` | Port for HTTP mode. |
| `CRAWLER_USER_AGENT` | `crawler-mcp/1.0 …` | User-Agent for all requests. |
| `CRAWLER_TIMEOUT_MS` | `15000` | Per-request timeout. |
| `CRAWLER_MAX_CHARS` | `20000` | Default cap on returned page content. |

## Notes

- Fetched pages are cached in memory for 5 minutes to avoid refetching during a crawl.
- `crawl_site` is bounded (`max_depth ≤ 3`, `max_pages ≤ 50`) and adds a politeness delay between requests.
- Playwright is an **optional** dependency — if it isn't installed, `render: auto`
  still works for static sites and `render: browser` returns a clear error.
- This server does not currently parse `robots.txt`; crawl responsibibly and only
  sites you are authorized to crawl.
