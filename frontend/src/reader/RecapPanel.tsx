import { useEffect, useRef, useState } from "react";
import { fetchScopedRecap, type ScopedRecap } from "../api";
import type { RecapChoice } from "./RecapMenu";

/** Reader positions this close to the top count as "the start of the book". */
const START_PARA = 2;

/** The heading of a recap message, e.g. "Last chapter: The Pool of Tears". */
export function recapHeading(choice: RecapChoice, r: Pick<ScopedRecap, "chapter">): string {
  return choice.scope === "chapter" && r.chapter ? `${choice.label}: ${r.chapter}` : choice.label;
}

/** What to say when a scope has nothing to recap yet. */
export const nothingYet = (para: number) =>
  para <= START_PARA ? "You're at the start of this book." : "Nothing to recap yet — keep reading.";

type Props = { bookId: string; para: number; choice: RecapChoice; onClose: () => void };

/**
 * Text-only recap, for when Talkback is not available (no voice on the server). Otherwise the Recap menu posts the
 * recap into the Talkback chat and the narrator reads it aloud.
 */
export default function RecapPanel({ bookId, para, choice, onClose }: Props) {
  const [recap, setRecap] = useState<ScopedRecap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => { closeRef.current?.focus(); }, []);
  useEffect(() => {
    let live = true;
    setRecap(null); setError(null);
    fetchScopedRecap(bookId, para, choice.scope, choice.section).then((r) => { if (live) setRecap(r); })
      .catch((e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [bookId, para, choice.scope, choice.section]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <section className="lab recap" role="dialog" aria-modal="false" aria-labelledby="recap-title">
      <div className="lab-head">
        <h2 id="recap-title">{recap ? recapHeading(choice, recap) : choice.label}</h2>
        <button ref={closeRef} onClick={onClose} aria-label="Close recap">Close</button>
      </div>
      {error && <p className="lab-error" role="alert">{error}</p>}
      {!recap && !error && <p>Looking back…</p>}
      {recap && (recap.text
        ? <p className="recap-text">{recap.text}</p>
        : <p className="recap-empty">{nothingYet(para)}</p>)}
    </section>
  );
}
