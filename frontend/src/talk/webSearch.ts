/**
 * Boson's server-side `web_search` tool on the Realtime answer path (B2). It is declared only when a web search cannot
 * spoil the book: always for a textbook, for a novel only with spoilers on. A spoiler-safe novel never gets it, whatever
 * the page asks for: the tool list is rebuilt from the book kind and the spoilers switch before every turn
 * (`sessionTools`), and the backend applies the same rule to the prompt (`websearch.allowed`).
 *
 * The server runs the search itself: the call arrives as a `function_call` item with `executor: "server"`, the result
 * as a `function_call_output` item (`conversation.item.added`), and the server starts the answer response on its own.
 * `sourcesFromSearchOutput` reads the links out of that result for the chat (the docs say the format is unstable, so
 * it is best effort and never throws).
 */
import { cleanSources, type Source } from "../api";
import { toolDefsFor, type BookKind } from "./tools";

/** The open-web form. `allowed_domains` is left out on purpose: the docs warn a scoped search often returns little or nothing. */
export const WEB_SEARCH_TOOL = { type: "web_search" } as const;

/** Textbooks always; novels only with spoilers on (an unknown kind is a novel); never when the server's WEB_SEARCH is
 * off (`serverOn`, from /api/health). Same rule as `backend/app/websearch.py`. */
export function webSearchAllowed(kind: BookKind | undefined, spoilers: boolean, serverOn = true): boolean {
  return serverOn && (kind === "textbook" || spoilers === true);
}

/** The reader's country (ISO 3166-1 alpha-2) from a locale like "en-CA", for local results; null when the locale has none. */
export function localeCountry(locale: string | undefined): string | null {
  const m = /^[a-z]{2,3}(?:[-_][A-Za-z]{4})?[-_]([A-Za-z]{2})(?![A-Za-z0-9])/.exec(locale ?? "");
  return m ? m[1].toUpperCase() : null;
}

/** web_search for this reader: `location.country` when the browser locale names one (the API rejects full names). */
export function webSearchTool(locale: string | undefined = typeof navigator === "undefined" ? undefined : navigator.language): Record<string, unknown> {
  const country = localeCountry(locale);
  return country ? { ...WEB_SEARCH_TOOL, location: { country } } : { ...WEB_SEARCH_TOOL };
}

/** The tools to declare for this book right now: its function tools, plus web_search when allowed. */
export function sessionTools(kind: BookKind | undefined, spoilers: boolean, locale?: string, serverOn = true): readonly unknown[] {
  const fns: readonly unknown[] = toolDefsFor(kind);
  return webSearchAllowed(kind, spoilers, serverOn) ? [...fns, webSearchTool(locale)] : fns;
}

/**
 * Links in a web_search result. Live (captured 2026-10-24) each result is a numbered block:
 * "[1] Title", then the URL on its own line, then an excerpt. Any http(s) URL in the text counts; its title is the
 * "[n] ..." line just before it, else the host. At most `MAX_SOURCES`, deduplicated (cleanSources).
 */
export function sourcesFromSearchOutput(output: unknown): Source[] {
  if (typeof output !== "string" || !output.trim()) return [];
  const raw: { uri: string; title: string }[] = [];
  let title = "";
  for (const line of output.split(/\r?\n/)) {
    const t = line.trim();
    const numbered = /^\[\d+\]\s*(.+)$/.exec(t);
    if (numbered) { title = numbered[1].trim(); continue; }
    const md = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/.exec(t);
    if (md) { raw.push({ uri: md[2], title: md[1].trim() }); title = ""; continue; }
    const url = /https?:\/\/[^\s<>"'\])]+/.exec(t);
    if (!url) continue;
    const bare = url[0].replace(/[.,;:]+$/, "");
    raw.push({ uri: bare, title: t === bare ? title : title || "" });
    title = "";
  }
  return cleanSources(raw);
}
