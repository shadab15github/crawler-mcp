# crawler-mcp

A web-crawler **MCP server** (Python) powered by
[**crawl4ai**](https://github.com/unclecode/crawl4ai) and the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP).

It fetches pages with a fast **httpx** static path and automatically falls back
to a **crawl4ai headless browser** when a page needs JavaScript to render.
crawl4ai also powers clean "fit-markdown", structured/LLM extraction,
screenshots, PDF, and JS-driven interaction. Designed to be used as a **Claude
connector** (local stdio or remote HTTP).

> **Migrated from TypeScript.** This fully replaces the original Node/axios/
> cheerio/Playwright implementation — there are no `.ts` files left. `package.json`
> is kept only as a convenience task-runner that shells out to Python.

## Tools

| Tool | What it does |
|------|--------------|
| `fetch_page` | Fetch one URL → content as Markdown, text, or raw HTML. |
| `extract_links` | List all hyperlinks on a page (absolute URLs, optional same-domain). |
| `crawl_site` | Recursive breadth-first crawl from a start URL (depth/page bounded). |
| `extract_by_selector` | Pull data via a CSS selector (text or an attribute). |
| `fetch_clean` | **crawl4ai** fit-markdown — boilerplate stripped; optional query-focused (BM25). |
| `extract_schema` | **crawl4ai** declarative multi-field extraction → JSON array of records. |
| `extract_llm` | **crawl4ai** LLM extraction by natural-language instruction (needs a provider). |
| `deep_crawl` | Goal-directed **best-first** crawl, prioritising links by keywords. |
| `crawl_dynamic` | Render + run JS (click / scroll / "load more"), wait for a condition. |
| `capture_page` | Full-page screenshot (PNG image) or render to PDF on disk. |
| `extract_tables` | Extract HTML tables as Markdown tables. |

`fetch_page`, `extract_links`, `crawl_site`, `extract_by_selector`,
`deep_crawl`, and `extract_tables` accept a `render` option: `auto` (default —
static first, browser if needed), `static` (never use a browser), or `browser`
(always render with crawl4ai).

## Setup

Requires **Python 3.10+**.

```bash
cd crawler-mcp
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt   # or:  pip install -e .

# Install the headless browser crawl4ai drives (Chromium):
crawl4ai-setup
# (equivalent to: python -m playwright install --with-deps chromium)

copy .env.example .env            # Windows  (cp on macOS/Linux), then edit
```

## Run

```bash
# Local (stdio) — for Claude Desktop:
python server.py

# Remote (HTTP) — for a custom/remote connector (serves http://127.0.0.1:3001/mcp):
# Windows PowerShell:
$env:MCP_TRANSPORT="http"; python server.py
# macOS/Linux:
MCP_TRANSPORT=http python server.py
```

Or via the `package.json` task-runner shortcuts (cross-platform):

```bash
npm run setup          # pip install -r requirements.txt && crawl4ai-setup
npm start              # python server.py            (stdio)
npm run start:http     # python server.py --http     (HTTP on :3001)
```

## Use it as a Claude connector

### Claude Desktop (local, stdio)

Add to your `claude_desktop_config.json` (Settings → Developer → Edit Config).
Point at the venv's Python so dependencies resolve:

```json
{
  "mcpServers": {
    "crawler": {
      "command": "D:\\MCP\\crawler-mcp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\MCP\\crawler-mcp\\server.py"]
    }
  }
}
```

Restart Claude Desktop. The crawler tools appear under the connectors menu.

### Remote HTTP connector (claude.ai / Desktop "Add custom connector")

1. Start in HTTP mode and expose it over HTTPS (reverse proxy or a tunnel such
   as `cloudflared` / `ngrok`):
   ```bash
   MCP_TRANSPORT=http python server.py
   ```
2. In Claude → **Settings → Connectors → Add custom connector**, enter:
   ```
   https://your-host/mcp
   ```

> Remote connectors on claude.ai generally require a public **HTTPS** URL.
> `http://127.0.0.1:3001/mcp` works for local testing but not the hosted web app.

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `MCP_HTTP_HOST` | `127.0.0.1` | Bind host for HTTP mode. |
| `MCP_HTTP_PORT` | `3001` | Port for HTTP mode. |
| `CRAWLER_USER_AGENT` | `crawler-mcp/2.0 …` | User-Agent for all requests. |
| `CRAWLER_TIMEOUT_MS` | `15000` | Per-request / page timeout. |
| `CRAWLER_MAX_CHARS` | `20000` | Default cap on returned page content. |
| `CRAWLER_OUTPUT_DIR` | `captures` | Where `capture_page` writes PDFs. |
| `CRAWLER_LLM_PROVIDER` | _(unset)_ | Enables `extract_llm`, e.g. `openai/gpt-4o-mini`. |
| `CRAWLER_LLM_API_TOKEN` | _(unset)_ | API key for the LLM provider. |

## Notes

- A single crawl4ai browser is started lazily and **reused** across calls
  (browser pooling), and static fetches are cached in memory for 5 minutes.
- `crawl_site` / `deep_crawl` are bounded and add a politeness delay between
  requests. `deep_crawl` follows the highest-scoring links first.
- `extract_llm` is inert until you set `CRAWLER_LLM_PROVIDER` (+ API key).
- This server does not parse `robots.txt`; crawl responsibly and only sites you
  are authorized to crawl.
