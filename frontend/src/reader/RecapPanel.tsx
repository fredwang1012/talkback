import { useEffect, useRef, useState } from "react";
import { fetchRecap, speak, type Recap } from "../api";
import { spend } from "../spend";
import { createSentenceSpeaker } from "../talk/sentencePlayer";
import { createReadAloud } from "./readAloud";

/** /api/speak takes at most 400 characters, so the recap is spoken a few sentences at a time. */
export function speechParts(text: string, max = 380): string[] {
  const parts: string[] = [];
  let cur = "";
  for (const s of text.replace(/\s+/g, " ").trim().match(/[^.!?]+[.!?]*\s*/g) ?? []) {
    if (cur && cur.length + s.length > max) { parts.push(cur.trim()); cur = ""; }
    cur += s;
    while (cur.length > max) { parts.push(cur.slice(0, max).trim()); cur = cur.slice(max); }
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts;
}

/** Reader positions this close to the top count as "the start of the book". */
const START_PARA = 2;

type Props = { bookId: string; para: number; voice: string | null; onClose: () => void };

/** "Previously...": what the reader has read so far, from summaries written only from text up to their place. */
export default function RecapPanel({ bookId, para, voice, onClose }: Props) {
  const [recap, setRecap] = useState<Recap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const readAloud = useRef<ReturnType<typeof createReadAloud> | null>(null);
  const voiceRef = useRef(voice);
  voiceRef.current = voice;
  if (!readAloud.current) {
    readAloud.current = createReadAloud(() => createSentenceSpeaker(async (text) => {
      const r = await speak(text, voiceRef.current ?? "");
      spend.addTts(text.length);
      return r;
    }), setPlaying, (e) => setError(e instanceof Error ? e.message : String(e)));
  }

  useEffect(() => { closeRef.current?.focus(); }, []);
  useEffect(() => {
    let live = true;
    fetchRecap(bookId, para).then((r) => { if (live) setRecap(r); })
      .catch((e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [bookId, para]);
  useEffect(() => () => readAloud.current?.stop(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function toggle(key: string, text: string) {
    if (!voice || !text) return;
    setError(null);
    readAloud.current?.toggle(key, speechParts(text));
  }
  function restart(key: string, text: string) {
    if (!voice || !text) return;
    setError(null);
    readAloud.current?.restart(key, speechParts(text));
  }
  const isPaused = (key: string, text: string) => playing !== key && !!readAloud.current?.paused(key, speechParts(text));
  /** Read aloud -> Pause -> Resume (from the same part), with Start over while paused. */
  const controls = (key: string, text: string, className: string) => (
    <>
      <button className={className} onClick={() => toggle(key, text)} aria-pressed={playing === key}>
        {playing === key ? "Pause" : isPaused(key, text) ? "Resume" : "Read aloud"}
      </button>
      {isPaused(key, text) && <button className="recap-more" onClick={() => restart(key, text)}>Start over</button>}
    </>
  );

  const earlierText = (more && recap?.earlier_full ? recap.earlier_full : recap?.earlier) ?? "";

  return (
    <section className="lab recap" role="dialog" aria-modal="false" aria-labelledby="recap-title">
      <div className="lab-head">
        <h2 id="recap-title">Previously{recap?.chapter ? `: ${recap.chapter}` : ""}</h2>
        <button ref={closeRef} onClick={onClose} aria-label="Close recap">Close</button>
      </div>
      {error && <p className="lab-error" role="alert">{error}</p>}
      {!recap && !error && <p>Looking back…</p>}
      {recap && (recap.text
        ? <p className="recap-text">{recap.text}</p>
        : <p className="recap-empty">{para <= START_PARA
          ? "You're at the start of this book."
          : "Nothing to recap yet — keep reading."}</p>)}
      {recap?.text && voice && (
        <div className="lab-row">
          {controls("recap", recap.text, "primary")}
        </div>
      )}
      {recap?.earlier && (
        <details className="recap-earlier">
          <summary>Earlier books</summary>
          <p className="recap-text">{earlierText}</p>
          {voice && controls("earlier", earlierText, "recap-more")}
          {recap.earlier_full && recap.earlier_full !== recap.earlier && (
            <button className="recap-more" aria-expanded={more} onClick={() => setMore(!more)}>
              {more ? "Less" : "More"}
            </button>
          )}
        </details>
      )}
    </section>
  );
}
