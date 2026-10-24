import { cleanSources, cleanSuggestions, type Source } from "../api";

/** Height of the Search Suggestions frame: Google's chip carousel is one scrollable row; without scripts in the
 * sandbox the frame cannot measure itself, so it gets a fixed height that fits that row. */
export const SUGGESTIONS_HEIGHT = 64;

/**
 * Google's Search Suggestions document for the frame: the markup exactly as Google sent it, after a `<base>` that
 * makes its chips open the Google results in a new tab (a framed google.com page would refuse to load). Nothing in
 * the markup itself is changed.
 */
export function suggestionsDoc(html: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><base target="_blank"><meta name="color-scheme" content="light dark">`
    + `<style>html,body{margin:0;background:transparent}</style></head><body>${html}</body></html>`;
}

/**
 * The web pages a grounded answer used, as small links under the reply (text only, never HTML; new tab), and Google's
 * Search Suggestions, which Google requires to be shown unmodified with a grounded answer. That markup is isolated in
 * a sandboxed iframe: no scripts, no same-origin access to the app, only popups (the chips) that leave the sandbox.
 */
export function SourceLinks({ sources, suggestions }: { sources?: Source[]; suggestions?: string }) {
  const links = cleanSources(sources);
  const html = cleanSuggestions(suggestions);
  if (!links.length && !html) return null;
  return (
    <div className="grounding">
      {links.length > 0 && (
        <div className="sources" aria-label="Web sources">
          <span className="sources-label">From the web:</span>
          <ul>
            {links.map((s) => (
              <li key={s.uri}>
                <a href={s.uri} target="_blank" rel="noopener noreferrer" title={s.uri}>{s.title}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {html && (
        <iframe className="search-suggestions" title="Google Search suggestions" srcDoc={suggestionsDoc(html)}
                sandbox="allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" loading="lazy"
                height={SUGGESTIONS_HEIGHT} />
      )}
    </div>
  );
}
