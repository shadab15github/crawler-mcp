import * as cheerio from "cheerio";
import TurndownService from "turndown";

const turndown = new TurndownService({
  headingStyle: "atx",
  codeBlockStyle: "fenced",
});
// Drop noisy / non-content elements entirely (svg/iframe are stripped via
// cheerio before conversion; these are a safety net for the turndown pass).
turndown.remove(["script", "style", "noscript"]);

export interface PageLink {
  text: string;
  href: string;
}

/** Strip scripts/styles and grab the document title. */
export function extractTitle(html: string): string {
  const $ = cheerio.load(html);
  return (
    $("title").first().text().trim() ||
    $('meta[property="og:title"]').attr("content")?.trim() ||
    ""
  );
}

/** Convert a page's main content to Markdown. */
export function htmlToMarkdown(html: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg, nav, header, footer").remove();
  // Prefer a main/article container if present, else fall back to body.
  const main = $("main").first();
  const article = $("article").first();
  const container = main.length ? main : article.length ? article : $("body");
  const inner = container.html() ?? html;
  return turndown.turndown(inner).replace(/\n{3,}/g, "\n\n").trim();
}

/** Plain readable text of a page. */
export function htmlToText(html: string): string {
  const $ = cheerio.load(html);
  $("script, style, noscript, iframe, svg").remove();
  return $("body").text().replace(/[ \t]+/g, " ").replace(/\n\s*\n\s*/g, "\n\n").trim();
}

/**
 * Extract all hyperlinks from a page, resolved to absolute URLs.
 * `baseUrl` is the page's final URL (used to resolve relative links).
 */
export function extractLinks(
  html: string,
  baseUrl: string,
  opts: { sameDomain?: boolean; limit?: number } = {},
): PageLink[] {
  const $ = cheerio.load(html);
  const base = new URL(baseUrl);
  const seen = new Set<string>();
  const links: PageLink[] = [];

  $("a[href]").each((_, el) => {
    const rawHref = $(el).attr("href");
    if (!rawHref) return;
    if (/^(javascript:|mailto:|tel:|#)/i.test(rawHref)) return;

    let abs: URL;
    try {
      abs = new URL(rawHref, base);
    } catch {
      return;
    }
    if (abs.protocol !== "http:" && abs.protocol !== "https:") return;
    if (opts.sameDomain && abs.hostname !== base.hostname) return;

    const href = abs.toString();
    if (seen.has(href)) return;
    seen.add(href);

    links.push({ text: $(el).text().replace(/\s+/g, " ").trim(), href });
  });

  return opts.limit ? links.slice(0, opts.limit) : links;
}

/**
 * Extract values matching a CSS selector. By default returns each element's
 * text; pass `attribute` to return an attribute value (e.g. "href", "src").
 */
export function extractBySelector(
  html: string,
  selector: string,
  opts: { attribute?: string; limit?: number; baseUrl?: string } = {},
): string[] {
  const $ = cheerio.load(html);
  const out: string[] = [];

  $(selector).each((_, el) => {
    if (opts.limit && out.length >= opts.limit) return false;
    if (opts.attribute) {
      let val = $(el).attr(opts.attribute);
      if (val == null) return;
      // Resolve URL-ish attributes to absolute when a base is known.
      if (opts.baseUrl && /^(href|src)$/i.test(opts.attribute)) {
        try {
          val = new URL(val, opts.baseUrl).toString();
        } catch {
          /* leave as-is */
        }
      }
      out.push(val);
    } else {
      const text = $(el).text().replace(/\s+/g, " ").trim();
      if (text) out.push(text);
    }
  });

  return out;
}

/** Truncate long content and append a clear notice when cut. */
export function truncate(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  return (
    text.slice(0, maxChars) +
    `\n\n…[truncated ${text.length - maxChars} more characters]`
  );
}
