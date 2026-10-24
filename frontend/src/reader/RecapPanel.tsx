import { useEffect, useRef, useState } from "react";
import { fetchRecap, speak, type Recap } from "../api";
import { spend } from "../spend";
import { createSentenceSpeaker, type SentenceSpeaker } from "../talk/sentencePlayer";

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
  const [speaking, setSpeaking] = useState(false);
  const [more, setMore] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const speaker = useRef<SentenceSpeaker | null>(null);

  useEffect(() => { closeRef.current?.focus(); }, []);
  useEffect(() => {
    let live = true;
    fetchRecap(bookId, para).then((r) => { if (live) setRecap(r); })
      .catch((e: Error) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [bookId, para]);
  useEffect(() => () => speaker.current?.stop(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function readAloud() {
    if (!recap?.text || !voice) return;
    if (speaking) { speaker.current?.stop(); setSpeaking(false); return; }
    const s = createSentenceSpeaker(async (text) => {
      const r = await speak(text, voice);
      spend.addTts(text.length);
      return r;
    });
    speaker.current = s;
    setSpeaking(true);
    setError(null);
    try {
      const parts = speechParts(recap.text);
      parts.slice(1).forEach((p) => s.prefetch(p)); // the later parts synthesise while the first plays
      for (const p of parts) await s.play(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (speaker.current === s) setSpeaking(false);
    }
  }

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
          <button className="primary" onClick={() => void readAloud()} aria-pressed={speaking}>
            {speaking ? "Stop" : "Read aloud"}
          </button>
        </div>
      )}
      {recap?.earlier && (
        <details className="recap-earlier">
          <summary>Earlier books</summary>
          <p className="recap-text">{more && recap.earlier_full ? recap.earlier_full : recap.earlier}</p>
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
