import axios from "axios";
import * as cheerio from "cheerio";

export type RenderMode = "auto" | "static" | "browser";

export interface FetchResult {
  html: string;
  /** URL after following redirects. */
  finalUrl: string;
  status: number;
  /** Which engine actually produced the HTML. */
  rendered: "static" | "browser";
}

const USER_AGENT =
  process.env.CRAWLER_USER_AGENT ??
  "crawler-mcp/1.0 (+https://github.com/your-org/crawler-mcp)";

const TIMEOUT_MS = parseInt(process.env.CRAWLER_TIMEOUT_MS ?? "15000", 10);

/**
 * Heuristic: decide whether a statically-fetched page probably needs a real
 * browser to render its content (single-page apps, client-side hydration).
 */
export function needsRendering(html: string): boolean {
  const $ = cheerio.load(html);
  $("script, style, noscript, template").remove();
  const visibleText = $("body").text().replace(/\s+/g, " ").trim();

  // Plenty of server-rendered text — no browser needed.
  if (visibleText.length >= 400) return false;

  // Common SPA hydration roots that are empty on first paint.
  const spaRoots = ["#root", "#app", "#__next", "[data-reactroot]", "#__nuxt"];
  const hasEmptySpaRoot = spaRoots.some((sel) => {
    const el = $(sel);
    return el.length > 0 && el.text().trim().length < 50;
  });

  // "Please enable JavaScript" style notices.
  const mentionsJs = /enable javascript|requires javascript/i.test(html);

  return hasEmptySpaRoot || mentionsJs || visibleText.length < 200;
}

async function fetchStatic(url: string): Promise<FetchResult> {
  const response = await axios.get<string>(url, {
    timeout: TIMEOUT_MS,
    maxRedirects: 5,
    responseType: "text",
    // Accept any 2xx/3xx/4xx so we can produce a clean error message instead of throwing.
    validateStatus: (s) => s < 500,
    headers: {
      "User-Agent": USER_AGENT,
      Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
    },
    // Follow redirects but capture the final URL.
    transformResponse: [(d) => d],
  });

  const finalUrl =
    (response.request?.res?.responseUrl as string | undefined) ?? url;

  return {
    html: typeof response.data === "string" ? response.data : String(response.data),
    finalUrl,
    status: response.status,
    rendered: "static",
  };
}

/**
 * Render with Playwright (Chromium). Imported lazily so the server runs fine
 * when Playwright/Chromium are not installed and no browser rendering is asked for.
 */
async function fetchWithBrowser(url: string): Promise<FetchResult> {
  let chromium: typeof import("playwright").chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch {
    throw new Error(
      "Browser rendering required, but Playwright is not installed. " +
        "Run `npm install playwright` then `npx playwright install chromium`.",
    );
  }

  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ userAgent: USER_AGENT });
    const page = await context.newPage();
    const response = await page.goto(url, {
      waitUntil: "networkidle",
      timeout: TIMEOUT_MS,
    });
    const html = await page.content();
    return {
      html,
      finalUrl: page.url(),
      status: response?.status() ?? 200,
      rendered: "browser",
    };
  } finally {
    await browser.close();
  }
}

/**
 * Fetch a URL's HTML. In "auto" mode, tries a fast static fetch first and
 * transparently falls back to a headless browser when the page looks like it
 * needs JavaScript to render.
 */
export async function fetchHtml(
  url: string,
  mode: RenderMode = "auto",
): Promise<FetchResult> {
  if (mode === "browser") {
    return fetchWithBrowser(url);
  }

  const staticResult = await fetchStatic(url);

  if (mode === "static") {
    return staticResult;
  }

  // mode === "auto"
  if (needsRendering(staticResult.html)) {
    try {
      return await fetchWithBrowser(staticResult.finalUrl);
    } catch {
      // Playwright unavailable or failed — return the static result rather than
      // failing the whole request. Caller still gets whatever was server-rendered.
      return staticResult;
    }
  }

  return staticResult;
}
