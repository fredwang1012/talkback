import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchBook, fetchHealth, type Book, type Health } from "../api";
import BookText from "../reader/BookText";
import PlayerBar from "../reader/PlayerBar";
import Stage from "../reader/Stage";
import {
  clampIndex, flattenChunks, loadPosition, readerPara, savePosition, silentDurationMs,
} from "../reader/position";

const SPEEDS = [0.75, 1, 1.25, 1.5];

export default function Reader({ bookId }: { bookId: string }) {
  const [book, setBook] = useState<Book | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [fullCast, setFullCast] = useState(true);
  const [autoResume, setAutoResume] = useState(true);
  const [speakerId, setSpeakerId] = useState("narrator");
  const lastManualScroll = useRef(0);

  useEffect(() => {
    fetchBook(bookId).then((b) => {
      setBook(b);
      setIndex(loadPosition(b.id, b.paragraphs.flatMap((p) => p.tts_chunks).length));
    }).catch((e: Error) => setError(e.message));
    fetchHealth().then(setHealth).catch(() => setHealth(null));
  }, [bookId]);

  const chunks = useMemo(() => (book ? flattenChunks(book) : []), [book]);
  const para = readerPara(chunks, index);
  const last = chunks.length - 1;

  const go = useCallback((i: number) => setIndex(clampIndex(i, chunks.length)), [chunks.length]);
  const step = useCallback((d: number) => setIndex((cur) => clampIndex(cur + d, chunks.length)), [chunks.length]);

  useEffect(() => { if (book) savePosition(book.id, index); }, [book, index]);

  // Silent preview: advance at an estimated reading pace until real audio exists.
  useEffect(() => {
    if (!playing || chunks.length === 0) return;
    if (index >= last) { setPlaying(false); return; }
    const t = window.setTimeout(() => step(1), silentDurationMs(chunks[index], speed));
    return () => window.clearTimeout(t);
  }, [playing, index, speed, chunks, last, step]);

  // Keep the active sentence in view, unless the reader scrolled by hand in the last 5 seconds.
  useEffect(() => {
    const mark = () => { lastManualScroll.current = Date.now(); };
    window.addEventListener("wheel", mark, { passive: true });
    window.addEventListener("touchmove", mark, { passive: true });
    return () => { window.removeEventListener("wheel", mark); window.removeEventListener("touchmove", mark); };
  }, []);
  useEffect(() => {
    if (Date.now() - lastManualScroll.current < 5000) return;
    const el = document.querySelector<HTMLElement>("[data-active='true']");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [index]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["SELECT", "INPUT", "TEXTAREA"].includes(t.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "ArrowRight") { lastManualScroll.current = 0; step(1); }
      else if (e.key === "ArrowLeft") { lastManualScroll.current = 0; step(-1); }
      else if (e.key === "k") setPlaying((p) => !p);
      else if (e.key === "]") setSpeed((s) => SPEEDS[Math.min(SPEEDS.indexOf(s) + 1, SPEEDS.length - 1)]);
      else if (e.key === "[") setSpeed((s) => SPEEDS[Math.max(SPEEDS.indexOf(s) - 1, 0)]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  if (error) return <p className="state-line" role="alert">Could not load this book ({error}). <a href="#/">Back to the library</a></p>;
  if (!book) return <p className="state-line">Turning to your page…</p>;

  return (
    <>
      <div className="reader">
        <div className="pages">
          <nav className="topbar">
            <a href="#/">All books</a>
            <span>{book.author}</span>
          </nav>
          <BookText book={book} chunks={chunks} index={index} onPick={(i) => { lastManualScroll.current = Date.now(); go(i); }} />
        </div>
        <Stage book={book} para={para} speakerId={speakerId} onSpeaker={setSpeakerId} health={health} />
      </div>
      <PlayerBar
        playing={playing} onToggle={() => setPlaying((p) => !p)}
        onPrev={() => { lastManualScroll.current = 0; step(-1); }} onNext={() => { lastManualScroll.current = 0; step(1); }}
        speed={speed} speeds={SPEEDS} onSpeed={setSpeed}
        fullCast={fullCast} onFullCast={setFullCast}
        autoResume={autoResume} onAutoResume={setAutoResume}
        para={para} totalParas={book.paragraphs.length}
        atStart={index === 0} atEnd={index >= last}
        hasCast={book.characters.length > 0}
      />
    </>
  );
}
