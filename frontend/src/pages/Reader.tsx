import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getAccessToken, setAccessToken } from "../accessToken";
import { fetchBook, fetchHealth, fetchTts, type Book, type Health } from "../api";
import { DEMO_SPEAKER, DEMO_START_PARA, firstChunkAtOrAfter, isDemo } from "../demo";
import BookText from "../reader/BookText";
import PlayerBar from "../reader/PlayerBar";
import { ReaderEngine, type AudioLike } from "../reader/ReaderEngine";
import Stage from "../reader/Stage";
import VoiceLab from "../reader/VoiceLab";
import { spend, type SpendSnapshot } from "../spend";
import { useTalk } from "../talk/useTalk";
import { DebugDrawer } from "../ui/DebugDrawer";
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
  const [word, setWord] = useState<{ charStart: number; charEnd: number } | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const demo = useMemo(() => isDemo(), []);
  const [code, setCode] = useState(getAccessToken());
  const [labOpen, setLabOpen] = useState(false);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const overridesRef = useRef<Record<string, string>>({});
  overridesRef.current = overrides;
  const [spendNow, setSpendNow] = useState<SpendSnapshot>(spend.snapshot());
  useEffect(() => spend.subscribe(setSpendNow), []);
  const lastManualScroll = useRef(0);
  const engineRef = useRef<ReaderEngine | null>(null);
  const indexRef = useRef(0);
  const playingRef = useRef(false);
  indexRef.current = index;
  playingRef.current = playing;

  useEffect(() => {
    fetchBook(bookId).then((b) => {
      setBook(b);
      const flat = flattenChunks(b);
      setIndex(demo ? firstChunkAtOrAfter(flat, DEMO_START_PARA) : loadPosition(b.id, flat.length));
      if (demo && b.characters.some((c) => c.id === DEMO_SPEAKER)) setSpeakerId(DEMO_SPEAKER); // pinned to Holmes
      try { setOverrides(JSON.parse(localStorage.getItem(`bookson:voices:${b.id}`) ?? "{}")); } catch { /* none saved */ }
    }).catch((e: Error) => setError(e.message));
    fetchHealth().then(setHealth).catch(() => setHealth(null));
  }, [bookId, demo]);

  const chunks = useMemo(() => (book ? flattenChunks(book) : []), [book]);
  const para = readerPara(chunks, index);
  const last = chunks.length - 1;
  const needsCode = !!health?.access_required && !code;
  const audioAvailable = health?.boson_key_set === true && !needsCode;

  // Real audio engine, only when the server has a Boson key. Otherwise the silent preview below is used.
  useEffect(() => {
    if (!book || !audioAvailable || chunks.length === 0) return;
    const engine = new ReaderEngine(chunks, {
      fetchTts: async (chunk, voice) => {
        const r = await fetchTts(book.id, chunk.id, voice ?? overridesRef.current[chunk.speaker], demo); // demo: pre-rendered only
        if (!r.cached) spend.addTts(chunk.tts_text.length); // only freshly synthesised speech costs money
        return r;
      },
      createAudio: (url) => new Audio(url) as unknown as AudioLike, // the engine only assigns a plain onended callback
    }, indexRef.current);
    engineRef.current = engine;
    const off = engine.on((s) => {
      setIndex(s.index);
      setPlaying(s.state === "playing" || s.state === "loading");
      setLoading(s.state === "loading");
      setAudioError(s.state === "error" ? s.error : null);
    });
    let raf = 0;
    const tick = () => {
      const w = engine.activeWord();
      setWord((cur) => (cur?.charStart === w?.charStart && cur?.charEnd === w?.charEnd ? cur : w));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); off(); engine.dispose(); engineRef.current = null; setWord(null); };
  }, [book, chunks, audioAvailable, demo]);


  // Talk mode: push-to-talk questions, answered from what has been read so far (needs the Boson key).
  const talk = useTalk({
    bookId,
    enabled: audioAvailable && !!book,
    speakerId,
    autoResume,
    voiceFor: (id) => overrides[id] ?? (id === "narrator" ? book?.narrator?.voice : book?.characters.find((c) => c.id === id)?.voice) ?? "marcus",
    reader: {
      isPlaying: () => (engineRef.current ? ["playing", "loading"].includes(engineRef.current.snapshot().state) : playingRef.current),
      currentIndex: () => indexRef.current,
      currentPara: () => readerPara(chunks, indexRef.current),
      pause: () => (engineRef.current ? engineRef.current.pause() : setPlaying(false)),
      play: () => (engineRef.current ? engineRef.current.play() : setPlaying(true)),
      resumeAt: (i) => {
        if (engineRef.current) { engineRef.current.seek(i); engineRef.current.play(); }
        else { setIndex(clampIndex(i, chunks.length)); setPlaying(true); }
      },
    },
  });
  const { pttDown, pttUp } = talk;

  // Hold Space to talk (works while reading, to interrupt).
  useEffect(() => {
    if (!audioAvailable) return;
    let held = false;
    const typing = (e: KeyboardEvent) => ["SELECT", "INPUT", "TEXTAREA", "BUTTON"].includes((e.target as HTMLElement).tagName);
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat || typing(e)) return;
      e.preventDefault();
      if (!held) { held = true; pttDown(); }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space" || !held) return;
      e.preventDefault();
      held = false;
      pttUp();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => { window.removeEventListener("keydown", down); window.removeEventListener("keyup", up); };
  }, [audioAvailable, pttDown, pttUp]);

  useEffect(() => { engineRef.current?.setSpeed(speed); }, [speed, audioAvailable, book]);
  useEffect(() => {
    engineRef.current?.setNarratorOnly(fullCast ? null : (book?.narrator?.voice ?? null));
  }, [fullCast, book, audioAvailable]);

  const seek = useCallback((i: number) => {
    const next = clampIndex(i, chunks.length);
    if (engineRef.current) engineRef.current.seek(next);
    else setIndex(next);
  }, [chunks.length]);
  const step = useCallback((d: number) => seek(index + d), [seek, index]);
  const toggle = useCallback(() => {
    if (engineRef.current) engineRef.current.toggle();
    else setPlaying((p) => !p);
  }, []);

  useEffect(() => { if (book && !demo) savePosition(book.id, index); }, [book, index, demo]);

  // Silent preview: advance at an estimated reading pace until real audio exists.
  useEffect(() => {
    if (audioAvailable || !playing || chunks.length === 0) return;
    if (index >= last) { setPlaying(false); return; }
    const t = window.setTimeout(() => setIndex((i) => clampIndex(i + 1, chunks.length)), silentDurationMs(chunks[index], speed));
    return () => window.clearTimeout(t);
  }, [audioAvailable, playing, index, speed, chunks, last]);

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
      else if (e.key === "k") toggle();
      else if (e.key === "]") setSpeed((s) => SPEEDS[Math.min(SPEEDS.indexOf(s) + 1, SPEEDS.length - 1)]);
      else if (e.key === "[") setSpeed((s) => SPEEDS[Math.max(SPEEDS.indexOf(s) - 1, 0)]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, toggle]);

  const setOverride = (speaker: string, voice: string | null) => {
    const next = { ...overridesRef.current };
    if (voice) next[speaker] = voice; else delete next[speaker];
    setOverrides(next);
    overridesRef.current = next;
    try { localStorage.setItem(`bookson:voices:${bookId}`, JSON.stringify(next)); } catch { /* storage blocked */ }
    engineRef.current?.refreshAhead(); // chunks fetched ahead used the old voice
  };
  const preview = async (speaker: string, voice: string) => {
    const chunk = chunks.find((c) => c.speaker === speaker) ?? chunks[0];
    if (!chunk) return;
    try {
      const r = await fetchTts(bookId, chunk.id, voice);
      if (!r.cached) spend.addTts(chunk.tts_text.length);
      void new Audio(r.audio_url).play();
    } catch (e) { setAudioError(e instanceof Error ? e.message : String(e)); }
  };

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
          <BookText book={book} chunks={chunks} index={index} word={word}
                    onPick={(i) => { lastManualScroll.current = Date.now(); seek(i); }} />
        </div>
        <Stage book={book} para={para} speakerId={speakerId} onSpeaker={setSpeakerId} health={health}
               talk={talk} audioAvailable={audioAvailable} onTalkMode={() => engineRef.current?.pause()}
               pinned={demo && book.characters.some((c) => c.id === DEMO_SPEAKER) ? DEMO_SPEAKER : null}
               needsCode={needsCode} onCode={(c) => { setAccessToken(c); setCode(c); }} onOpenLab={() => setLabOpen(true)} />
      </div>
      {labOpen && (
        <VoiceLab book={book} overrides={overrides} onOverride={setOverride} onPreview={preview}
                  audioAvailable={audioAvailable} onClose={() => setLabOpen(false)} />
      )}
      <DebugDrawer entries={talk.logs} spend={spendNow} />
      <PlayerBar
        playing={playing} loading={loading} onToggle={toggle}
        onPrev={() => { lastManualScroll.current = 0; step(-1); }} onNext={() => { lastManualScroll.current = 0; step(1); }}
        speed={speed} speeds={SPEEDS} onSpeed={setSpeed}
        fullCast={fullCast} onFullCast={setFullCast}
        autoResume={autoResume} onAutoResume={setAutoResume}
        para={para} totalParas={book.paragraphs.length}
        atStart={index === 0} atEnd={index >= last}
        hasCast={book.characters.length > 0}
        audioAvailable={audioAvailable} audioError={audioError}
      />
    </>
  );
}
