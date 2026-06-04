import dotenv from "dotenv";
dotenv.config({ quiet: true });

import { randomUUID } from "node:crypto";
import express from "express";
import { z } from "zod";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";

import { fetchHtml, type RenderMode, type FetchResult } from "./fetcher.js";
import {
  extractTitle,
  htmlToMarkdown,
  htmlToText,
  extractLinks,
  extractBySelector,
  truncate,
} from "./extract.js";
import { describeFetchError, errorResult, normalizeUrl } from "./errors.js";

const MAX_CHARS = parseInt(process.env.CRAWLER_MAX_CHARS ?? "20000", 10);

const server = new McpServer({
  name: "crawler-mcp-server",
  version: "1.0.0",
});

// =======================
// SHARED HELPERS
// =======================

const FETCH_TTL_MS = 5 * 60 * 1000; // 5 minutes — pages don't change second-to-second

interface CacheEntry {
  data: FetchResult;
  expiresAt: number;
}
const cache = new Map<string, CacheEntry>();

async function getPage(url: string, mode: RenderMode): Promise<FetchResult> {
  const key = `${mode}|${url}`;
  const entry = cache.get(key);
  if (entry && Date.now() < entry.expiresAt) return entry.data;

  const data = await fetchHtml(url, mode);
  cache.set(key, { data, expiresAt: Date.now() + FETCH_TTL_MS });
  return data;
}

const urlSchema = z
  .string()
  .trim()
  .min(1)
  .max(2048)
  .describe("Page URL (http/https). Scheme-less input like `example.com` is allowed.");

const renderSchema = z
  .enum(["auto", "static", "browser"])
  .default("auto")
  .describe(
    "auto=fast static fetch, fall back to headless browser if the page needs JS; " +
      "static=never use a browser; browser=always render with Playwright (handles SPAs).",
  );

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// =======================
// TOOL 1: FETCH PAGE
// =======================

server.registerTool(
  "fetch_page",
  {
    description:
      "Fetch a single web page and return its readable content as Markdown, plain text, or raw HTML. " +
      "Automatically renders JavaScript-heavy pages with a headless browser when needed.",
    inputSchema: {
      url: urlSchema,
      format: z
        .enum(["markdown", "text", "html"])
        .default("markdown")
        .describe("Output format for the page content."),
      render: renderSchema.optional(),
      max_chars: z
        .number()
        .int()
        .positive()
        .max(200000)
        .optional()
        .describe(`Cap on returned characters (default ${MAX_CHARS}).`),
    },
  },
  async ({ url, format, render, max_chars }) => {
    let target: string;
    try {
      target = normalizeUrl(url);
    } catch (err) {
      return errorResult(err instanceof Error ? err.message : String(err));
    }

    try {
      const page = await getPage(target, render ?? "auto");
      const title = extractTitle(page.html);

      let body: string;
      if (format === "html") body = page.html;
      else if (format === "text") body = htmlToText(page.html);
      else body = htmlToMarkdown(page.html);

      const limit = max_chars ?? MAX_CHARS;
      const header =
        `# ${title || "(untitled)"}\n` +
        `URL: ${page.finalUrl}\n` +
        `Status: ${page.status} · Rendered: ${page.rendered}\n\n---\n\n`;

      return {
        content: [{ type: "text", text: header + truncate(body, limit) }],
      };
    } catch (err) {
      return errorResult(describeFetchError(err, target));
    }
  },
);

// =======================
// TOOL 2: EXTRACT LINKS
// =======================

server.registerTool(
  "extract_links",
  {
    description:
      "Extract all hyperlinks from a web page, resolved to absolute URLs. " +
      "Optionally restrict to links on the same domain.",
    inputSchema: {
      url: urlSchema,
      same_domain: z
        .boolean()
        .default(false)
        .describe("Only return links pointing to the same hostname as the page."),
      limit: z
        .number()
        .int()
        .positive()
        .max(500)
        .default(100)
        .describe("Maximum number of links to return."),
      render: renderSchema.optional(),
    },
  },
  async ({ url, same_domain, limit, render }) => {
    let target: string;
    try {
      target = normalizeUrl(url);
    } catch (err) {
      return errorResult(err instanceof Error ? err.message : String(err));
    }

    try {
      const page = await getPage(target, render ?? "auto");
      const links = extractLinks(page.html, page.finalUrl, {
        sameDomain: same_domain,
        limit,
      });

      if (links.length === 0) {
        return { content: [{ type: "text", text: `No links found on ${page.finalUrl}.` }] };
      }

      const body = links
        .map((l, i) => `${i + 1}. ${l.text || "(no text)"}\n   ${l.href}`)
        .join("\n");

      return {
        content: [
          {
            type: "text",
            text: `Found ${links.length} link${links.length === 1 ? "" : "s"} on ${page.finalUrl}:\n\n${body}`,
          },
        ],
      };
    } catch (err) {
      return errorResult(describeFetchError(err, target));
    }
  },
);

// =======================
// TOOL 3: CRAWL SITE
// =======================

server.registerTool(
  "crawl_site",
  {
    description:
      "Recursively crawl a website starting from a URL, following links up to a maximum depth and page count. " +
      "Returns a short content summary for each page visited. Stays on the same domain by default.",
    inputSchema: {
      start_url: urlSchema,
      max_depth: z
        .number()
        .int()
        .min(0)
        .max(3)
        .default(1)
        .describe("How many link-hops to follow from the start URL (0 = only the start page)."),
      max_pages: z
        .number()
        .int()
        .positive()
        .max(50)
        .default(10)
        .describe("Hard cap on the total number of pages fetched."),
      same_domain: z
        .boolean()
        .default(true)
        .describe("Only follow links on the start URL's hostname."),
      format: z
        .enum(["markdown", "text"])
        .default("markdown")
        .describe("Output format for each page's content summary."),
      chars_per_page: z
        .number()
        .int()
        .positive()
        .max(20000)
        .default(2000)
        .describe("Characters of content to include per page."),
      delay_ms: z
        .number()
        .int()
        .min(0)
        .max(10000)
        .default(250)
        .describe("Politeness delay between requests, in milliseconds."),
      render: renderSchema.optional(),
    },
  },
  async ({
    start_url,
    max_depth,
    max_pages,
    same_domain,
    format,
    chars_per_page,
    delay_ms,
    render,
  }) => {
    let start: string;
    try {
      start = normalizeUrl(start_url);
    } catch (err) {
      return errorResult(err instanceof Error ? err.message : String(err));
    }

    const mode = render ?? "auto";
    const visited = new Set<string>();
    const queue: Array<{ url: string; depth: number }> = [{ url: start, depth: 0 }];
    const results: string[] = [];
    let firstError: string | null = null;

    while (queue.length > 0 && visited.size < max_pages) {
      const { url, depth } = queue.shift()!;
      // Normalise (drop hash) for the visited check.
      const canonical = url.split("#")[0];
      if (visited.has(canonical)) continue;
      visited.add(canonical);

      try {
        const page = await getPage(canonical, mode);
        const title = extractTitle(page.html);
        const content =
          format === "text" ? htmlToText(page.html) : htmlToMarkdown(page.html);

        results.push(
          `## [depth ${depth}] ${title || "(untitled)"}\n` +
            `${page.finalUrl} (status ${page.status}, ${page.rendered})\n\n` +
            truncate(content, chars_per_page),
        );

        if (depth < max_depth && visited.size < max_pages) {
          const links = extractLinks(page.html, page.finalUrl, {
            sameDomain: same_domain,
          });
          for (const l of links) {
            const c = l.href.split("#")[0];
            if (!visited.has(c)) queue.push({ url: c, depth: depth + 1 });
          }
        }
      } catch (err) {
        const msg = describeFetchError(err, canonical);
        if (!firstError) firstError = msg;
        results.push(`## [depth ${depth}] FAILED\n${canonical}\n\n${msg}`);
      }

      if (delay_ms > 0 && queue.length > 0 && visited.size < max_pages) {
        await sleep(delay_ms);
      }
    }

    if (results.length === 0) {
      return errorResult(firstError ?? `Nothing crawled from ${start}.`);
    }

    const header =
      `Crawled ${visited.size} page${visited.size === 1 ? "" : "s"} from ${start} ` +
      `(max depth ${max_depth}, ${same_domain ? "same-domain" : "cross-domain"}):\n\n`;

    return { content: [{ type: "text", text: header + results.join("\n\n---\n\n") }] };
  },
);

// =======================
// TOOL 4: EXTRACT BY SELECTOR
// =======================

server.registerTool(
  "extract_by_selector",
  {
    description:
      "Extract specific data from a page using a CSS selector. " +
      "Returns each matching element's text, or an attribute value when `attribute` is given " +
      "(e.g. selector='a.product', attribute='href').",
    inputSchema: {
      url: urlSchema,
      selector: z
        .string()
        .trim()
        .min(1)
        .max(500)
        .describe("CSS selector, e.g. 'h2', '.price', 'table tr td', 'a.result-link'."),
      attribute: z
        .string()
        .trim()
        .min(1)
        .max(100)
        .optional()
        .describe("Return this attribute instead of text (e.g. 'href', 'src', 'data-id')."),
      limit: z
        .number()
        .int()
        .positive()
        .max(1000)
        .default(100)
        .describe("Maximum number of matches to return."),
      render: renderSchema.optional(),
    },
  },
  async ({ url, selector, attribute, limit, render }) => {
    let target: string;
    try {
      target = normalizeUrl(url);
    } catch (err) {
      return errorResult(err instanceof Error ? err.message : String(err));
    }

    try {
      const page = await getPage(target, render ?? "auto");
      const matches = extractBySelector(page.html, selector, {
        attribute,
        limit,
        baseUrl: page.finalUrl,
      });

      if (matches.length === 0) {
        return {
          content: [
            {
              type: "text",
              text: `No elements matched selector \`${selector}\` on ${page.finalUrl}.`,
            },
          ],
        };
      }

      const what = attribute ? `[${attribute}]` : "text";
      const body = matches.map((m, i) => `${i + 1}. ${m}`).join("\n");

      return {
        content: [
          {
            type: "text",
            text: `Matched ${matches.length} element${matches.length === 1 ? "" : "s"} for \`${selector}\` ${what} on ${page.finalUrl}:\n\n${body}`,
          },
        ],
      };
    } catch (err) {
      return errorResult(describeFetchError(err, target));
    }
  },
);

// =======================
// START SERVER
// =======================

async function startStdio(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Crawler MCP Server running on stdio.");
}

async function startHttp(port: number): Promise<void> {
  const app = express();
  app.use(express.json());

  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });

  await server.connect(transport);

  app.post("/mcp", async (req, res) => {
    await transport.handleRequest(req, res, req.body);
  });
  app.get("/mcp", async (req, res) => {
    await transport.handleRequest(req, res);
  });
  app.delete("/mcp", async (req, res) => {
    await transport.handleRequest(req, res);
  });

  app.get("/health", (_req, res) => {
    res.json({ status: "ok", server: "crawler-mcp-server" });
  });

  app.listen(port, () => {
    console.error(`Crawler MCP Server running on http://localhost:${port}/mcp`);
  });
}

const mode = (process.env.MCP_TRANSPORT ?? "stdio").toLowerCase();

if (mode === "http") {
  const port = parseInt(process.env.MCP_HTTP_PORT ?? "3001", 10);
  await startHttp(port);
} else {
  await startStdio();
}
