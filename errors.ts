import { AxiosError } from "axios";

/**
 * Convert axios/network errors into a user-readable string. `label` is the
 * subject of the failed request (e.g. the URL being fetched).
 */
export function describeFetchError(err: unknown, label: string): string {
  if (err instanceof AxiosError && err.response) {
    const status = err.response.status;
    if (status === 404) return `${label} returned 404 Not Found.`;
    if (status === 401 || status === 403)
      return `${label} refused access (${status}). The site may block crawlers.`;
    if (status === 429)
      return `${label} rate-limited the request (429). Slow down and retry.`;
    if (status >= 500)
      return `${label} returned a server error (${status}).`;
    return `${label} returned HTTP ${status}.`;
  }
  if (err instanceof AxiosError && err.code === "ECONNABORTED") {
    return `Request to ${label} timed out.`;
  }
  if (err instanceof AxiosError && err.code === "ENOTFOUND") {
    return `Could not resolve host for ${label}. Check the URL.`;
  }
  if (err instanceof Error) return `Error fetching ${label}: ${err.message}`;
  return `Unknown error fetching ${label}: ${String(err)}`;
}

export function errorResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
    isError: true,
  };
}

/** Validate and normalise a URL, returning an absolute http(s) URL string. */
export function normalizeUrl(input: string): string {
  let url: URL;
  try {
    url = new URL(input.trim());
  } catch {
    // Allow scheme-less input like "example.com/page"
    try {
      url = new URL(`https://${input.trim()}`);
    } catch {
      throw new Error(`"${input}" is not a valid URL.`);
    }
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Only http and https URLs are supported (got ${url.protocol}).`);
  }
  return url.toString();
}
