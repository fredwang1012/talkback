import { isAutoplayBlock } from "../audio/mediaErrors";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getAccessToken, setAccessToken } from "../accessToken";
import {
  avatarUrl, fetchAvatars, fetchBook, fetchCast, fetchFaces, fetchHealth, fetchTts, fetchVoices,
  type Book, type Cast, type Face, type Health,
} from "../api";
import {
  PRESETS, castVoice, dropReferences, createCastSaver, emptyCast, isNovel, loadStyle, loadTalkChoice, migrateOldOverrides,
  oneVoiceOverride, readingFaceId, saveStyle, saveTalkChoice, talkFaceId, talkVoice, type ReadingStyle, type TalkChoice,
} from "../reader/castLogic";
import { DEMO_SPEAKER, DEMO_START_PARA, firstChunkAtOrAfter, isDemo } from "../demo";
import BookText from "../reader/BookText";
import PlayerBar from "../reader/PlayerBar";
import RecapPanel from "../reader/RecapPanel";
import { ReaderEngine, type AudioLike } from "../reader/ReaderEngine";
import type { FollowAudio } from "../reader/clipFollow";
import { AudioLevelMeter } from "../reader/audioLevel";
import { SPEECH_IDLE, stepSpeech } from "../reader/speechLevel";
import Stage from "../reader/Stage";
import { toggleMode, validSpeaker } from "../reader/talkbackLogic";
import TalkbackView, { ModeToggle } from "../reader/TalkbackView";
import { loadSpoilers, saveSpoilers } from "../reader/spoilers";
import VoiceLab from "../reader/VoiceLab";
import { spend, type SpendSnapshot } from "../spend";
import { useTalk } from "../talk/useTalk";
import { DebugDrawer } from "../ui/DebugDrawer";
import { debugEnabled } from "../ui/debugFlag";
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
  const [autoResume, setAutoResume] = useState(true);
  const [speakerId, setSpeakerId] = useState("narrator");
  const [avatars, setAvatars] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"read" | "talk">("read");
  const stopTalkRef = useRef<() => void>(() => {});
  const [word, setWord] = useState<{ charStart: number; charEnd: number } | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [audioNotice, setAudioNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const demo = useMemo(() => isDemo(), []);
  const [code, setCode] = useState(getAccessToken());
  const [remember, setRemember] = useState(true);
  const [labOpen, setLabOpen] = useState(false);
  const [recapOpen, setRecapOpen] = useState(false);
  const [cast, setCast] = useState<Cast>(emptyCast);
  const [castLoaded, setCastLoaded] = useState(false); // false until the saved cast arrived: then the server picks voices
  const castEdited = useRef(false); // a local edit happened: late server loads must not overwrite it
  const [animating, setAnimating] = useState<Set<string>>(new Set()); // lives here so closing the panel keeps the spinner
  const [animErrors, setAnimErrors] = useState<Record<string, string>>({});
  const [castError, setCastError] = useState<string | null>(null);
  const [faces, setFaces] = useState<Face[]>([]);
  const [cloned, setCloned] = useState<Record<string, string>>({});
  const [style, setStyle] = useState<ReadingStyle>(() => loadStyle(bookId));
  const [talkChoice, setTalkChoice] = useState<TalkChoice>(() => loadTalkChoice(bookId));
  useEffect(() => { setStyle(loadStyle(bookId)); setTalkChoice(loadTalkChoice(bookId)); }, [bookId]);
  const [lipSync, setLipSync] = useState(() => { try { return localStorage.getItem("talkback:lipsync") === "1"; } catch { return false; } });
  const [spoilers, setSpoilers] = useState(() => loadSpoilers(bookId));
  useEffect(() => setSpoilers(loadSpoilers(bookId)), [bookId]);
  const [spendNow, setSpendNow] = useState<SpendSnapshot>(spend.snapshot());
  useEffect(() => spend.subscribe(setSpendNow), []);
  const lastManualScroll = useRef(0);
  const engineRef = useRef<ReaderEngine | null>(null);
  const meterRef = useRef<AudioLevelMeter | null>(null);
  const levelRef = useRef(0); // live loudness of the read-aloud audio (read-mode face)
  const [readSpeaking, setReadSpeaking] = useState(false);
  const getAudio = useCallback(() => (engineRef.current?.currentAudio() as unknown as FollowAudio | null) ?? null, []);
  const indexRef = useRef(0);
  const playingRef = useRef(false);
  indexRef.current = index;
  playingRef.current = playing;

  useEffect(() => {
    fetchBook(bookId).then((b) => {
      setBook(b);
      const flat = flattenChunks(b);
      setIndex(demo ? firstChunkAtOrAfter(flat, DEMO_START_PARA) : loadPosition(b.id, flat.length));
      if (demo && b.characters.some((c) => c.id === DEMO_SPEAKER)) setSpeakerId(DEMO_SPEAKER); // pinned to the demo character
      // the cast lives on the server; the old series-wide localStorage voices are moved into it once
      castEdited.current = false;
      fetchCast(b.id).then(async (c) => {
        if (castEdited.current) return; // the visitor already edited: their state is the newest
        const migrated = await migrateOldOverrides(b, c);
        if (!castEdited.current) { setCast(migrated ?? c); setCastLoaded(true); }
      }).catch(() => { /* no cast: books keep their own voices */ });
    }).catch((e: Error) => setError(e.message));
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchAvatars(bookId).then((ids) => setAvatars(new Set(ids)));
    fetchFaces().then(setFaces).catch(() => setFaces([]));
    fetchVoices().then(setCloned).catch(() => setCloned({}));
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
        const r = await fetchTts(book.id, chunk.id, voice, demo); // demo: pre-rendered only
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
      setAudioNotice(s.state === "paused" ? s.error : null);
    });
    const meter = new AudioLevelMeter();
    meterRef.current = meter;
    let speech = SPEECH_IDLE, last = performance.now();
    let raf = 0;
    const tick = () => {
      const now = performance.now();
      const snap = engine.snapshot();
      const cur = engine.currentAudio() as unknown as { paused?: boolean; ended?: boolean } | null;
      // start/stop exactly with the element's own play/pause/ended, not only with the engine's state
      const sounding = snap.state === "playing" && !(cur && (cur.paused || cur.ended));
      const lvl = meter.level(engine.currentAudio(), sounding);
      if (lvl === null) { // no Web Audio: fall back to "speaking while playing"
        levelRef.current = 0;
        speech = { ...SPEECH_IDLE, speaking: sounding };
      } else {
        levelRef.current = lvl;
        speech = sounding ? stepSpeech(speech, lvl, now - last) : SPEECH_IDLE;
      }
      last = now;
      setReadSpeaking((cur) => (cur === speech.speaking ? cur : speech.speaking));
      const w = engine.activeWord();
      setWord((cur) => (cur?.charStart === w?.charStart && cur?.charEnd === w?.charEnd ? cur : w));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf); off(); engine.dispose(); engineRef.current = null; setWord(null);
      meter.dispose(); meterRef.current = null; levelRef.current = 0; setReadSpeaking(false);
    };
  }, [book, chunks, audioAvailable, demo]);


  const oneVoice = book ? oneVoiceOverride(book, cast, style) : null;
  const readingVoiceFor = (id: string) => (book ? castVoice(book, cast, id) : "marcus");
  const talkVoiceFor = (id: string) => (book ? talkVoice(book, cast, talkChoice, id) : "marcus");
  const faceById = (id: string | null) => (id ? faces.find((f) => f.id === id) : undefined);
  const talkFaceFor = (id: string) =>
    book ? faceById(talkFaceId(cast, talkChoice, id, talkVoiceFor(id), faces)) : undefined;
  const narratorVoice = book ? oneVoice ?? castVoice(book, cast, "narrator") : null;
  const current = chunks[index];
  // the face of whoever reads aloud right now (read mode)
  const readingFace = book && playing && !loading && current
    ? faceById(readingFaceId(book, cast, style, current.speaker)) ?? null : null;
  const voiceOptions = useMemo(
    () => [...PRESETS.map((v) => ({ value: v, label: v })), ...Object.entries(cloned).map(([n, v]) => ({ value: v, label: `${n} (cloned)` }))],
    [cloned]);
  const lipSyncFace = faceById(book ? talkFaceId(cast, talkChoice, speakerId, talkVoiceFor(speakerId), faces) : null);
  const lipSyncable = !!lipSyncFace && lipSyncFace.voice === talkVoiceFor(speakerId);

  // Talk mode: push-to-talk questions, answered from what has been read so far (needs the Boson key).
  const talk = useTalk({
    bookId,
    enabled: audioAvailable && !!book,
    speakerId,
    autoResume,
    health,
    canLipSync: (voice) => { const f = talkFaceFor(speakerId); return !!f && f.voice === voice; },
    lipSync,
    spoilers: spoilers && book?.kind === "novel",
    talkback: mode === "talk",
    voiceFor: talkVoiceFor,
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
  stopTalkRef.current = talk.stop;

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
    engineRef.current?.setNarratorOnly(oneVoice);
  }, [oneVoice, audioAvailable]);
  // a changed cast applies at once, from the local copy (the save runs in the background)
  useEffect(() => {
    engineRef.current?.setVoiceResolver(book && castLoaded
      ? (chunk) => castVoice(book, cast, isNovel(book) ? chunk.speaker : "narrator") : null);
  }, [cast, castLoaded, book, audioAvailable]);

  const changeMode = useCallback((m: "read" | "talk") => {
    setMode(m);
    if (m === "talk") { if (engineRef.current) engineRef.current.pause(); else setPlaying(false); }
    else stopTalkRef.current(); // leaving Talkback silences the character; the reading position is untouched
  }, []);

  const seek = useCallback((i: number) => {
    const next = clampIndex(i, chunks.length);
    if (engineRef.current) engineRef.current.seek(next);
    else setIndex(next);
  }, [chunks.length]);
  const step = useCallback((d: number) => seek(index + d), [seek, index]);
  const toggle = useCallback(() => {
    meterRef.current?.unlock(); // the AudioContext may only start from a user gesture
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
      else if (e.key === "t" && audioAvailable) changeMode(toggleMode(mode));
      else if (e.key === "]") setSpeed((s) => SPEEDS[Math.min(SPEEDS.indexOf(s) + 1, SPEEDS.length - 1)]);
      else if (e.key === "[") setSpeed((s) => SPEEDS[Math.max(SPEEDS.indexOf(s) - 1, 0)]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, toggle, audioAvailable, mode, changeMode]);

  const saver = useMemo(
    () => createCastSaver(bookId, () => setCastError(null), setCastError),
    [bookId]);
  const changeCast = (next: Cast) => { castEdited.current = true; setCastLoaded(true); setCast(next); setCastError(null); void saver(next); };
  const changeStyle = (next: ReadingStyle) => { setStyle(next); saveStyle(bookId, next); };
  const changeTalk = (next: TalkChoice) => { setTalkChoice(next); saveTalkChoice(bookId, next); };
  const reloadCast = () => { void saver.flush().then(() => fetchCast(bookId)).then((c) => { setCast(c); }).catch(() => {}); };
  const reloadFaces = () => { fetchFaces().then(setFaces).catch(() => {}); fetchVoices().then(setCloned).catch(() => {}); };
  const preview = async (speaker: string, voice: string) => {
    const chunk = chunks.find((c) => c.speaker === speaker) ?? chunks[0];
    if (!chunk) return;
    try {
      const r = await fetchTts(bookId, chunk.id, voice);
      if (!r.cached) spend.addTts(chunk.tts_text.length);
      await new Audio(r.audio_url).play();
    } catch (e) {
      if (isAutoplayBlock(e)) setAudioNotice("Tap the preview button again to hear the voice. Your browser needs a fresh click.");
      else setAudioError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (!book) return;
    const ok = validSpeaker(book, para, spoilers, speakerId);
    if (ok !== speakerId) setSpeakerId(ok);
  }, [book, para, spoilers, speakerId]);

  if (error && error.includes(": 401")) {
    return (
      <form className="code-form state-line" onSubmit={(e) => {
        e.preventDefault();
        const v = new FormData(e.currentTarget).get("code");
        if (typeof v === "string" && v.trim()) { setAccessToken(v.trim(), remember); setCode(v.trim()); window.location.reload(); }
      }}>
        <label htmlFor="book-code">This server needs an access code to open books</label>
        <div>
          <input id="book-code" name="code" type="password" autoComplete="off" />
          <button type="submit">Open the book</button>
        </div>
        <label className="remember"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Remember on this device</label>
      </form>
    );
  }
  if (error) return <p className="state-line" role="alert">Could not load this book ({error}). <a href="#/">Back to the library</a></p>;
  if (!book) return <p className="state-line">Turning to your page…</p>;

  return (
    <>
      <div className={`reader${mode === "talk" ? " talkback-mode" : ""}`}>
        <div className="pages">
          <nav className="topbar">
            <a href="#/">All books</a>
            <span>{book.author}</span>
            <ModeToggle mode={mode} onMode={changeMode} talkAvailable={audioAvailable} />
          </nav>
          {mode === "talk" ? (
            <TalkbackView book={book} para={para} speakerId={speakerId} onSpeaker={setSpeakerId}
                          pinned={demo && book.characters.some((c) => c.id === DEMO_SPEAKER) ? DEMO_SPEAKER : null}
                          talk={talk} faces={faces} faceFor={talkFaceFor} autoVoiceFor={readingVoiceFor}
                          choice={talkChoice} onChoice={changeTalk} voices={voiceOptions}
                          spoilers={spoilers} onSpoilers={(on) => { setSpoilers(on); saveSpoilers(bookId, on); }}
                          lipSyncable={lipSyncable} lipSync={lipSync}
                          onLipSync={(on) => { setLipSync(on); try { localStorage.setItem("talkback:lipsync", on ? "1" : "0"); } catch { /* storage blocked */ } }}
                          portrait={book.characters.find((c) => c.id === speakerId)?.portrait ?? book.narrator?.portrait ?? null}
                          onBack={() => changeMode("read")} onRecap={() => setRecapOpen(true)} onVoices={() => setLabOpen(true)} />
          ) : (
            <BookText book={book} chunks={chunks} index={index} word={word}
                      onPick={(i) => { lastManualScroll.current = Date.now(); seek(i); }} />
          )}
        </div>
        {mode === "read" && (
          <Stage book={book} para={para} speakerId={speakerId} onSpeaker={setSpeakerId} health={health}
                 talk={talk} audioAvailable={audioAvailable}
                 avatarSrc={chunks[index] && avatars.has(chunks[index].id) ? avatarUrl(book.id, chunks[index].id) : null}
                 avatarPlaying={playing && !loading} getAudio={getAudio} onMode={changeMode}
                 pinned={demo && book.characters.some((c) => c.id === DEMO_SPEAKER) ? DEMO_SPEAKER : null}
                 needsCode={needsCode} onCode={(c, rem) => { setAccessToken(c, rem); setCode(c); }} onOpenLab={() => setLabOpen(true)}
                 faceFor={talkFaceFor}
                 readingFace={readingFace} spoilers={spoilers}
                 readSpeaking={readSpeaking} levelRef={levelRef} />
        )}
      </div>
      {labOpen && (
        <VoiceLab book={book} cast={cast} onCast={changeCast} castError={castError} style={style} onStyle={changeStyle}
                  cloned={cloned} onCloned={setCloned} animating={animating} setAnimating={setAnimating} animErrors={animErrors} setAnimErrors={setAnimErrors} faces={faces} onFaces={setFaces}
                  onFacesChanged={() => { reloadFaces(); reloadCast(); }}
                  onRemoved={(gone) => {
                    const next = dropReferences(style, talkChoice, gone);
                    changeStyle(next.style); changeTalk(next.talk);
                  }}
                  onPreview={preview} audioAvailable={audioAvailable} onClose={() => setLabOpen(false)} />
      )}
      {recapOpen && (
        <RecapPanel bookId={book.id} para={para} voice={audioAvailable ? narratorVoice : null}
                    onClose={() => setRecapOpen(false)} />
      )}
      {debugEnabled() && <DebugDrawer entries={talk.logs} spend={spendNow} />}
      {mode === "read" && <PlayerBar
        playing={playing} loading={loading} onToggle={toggle} onRecap={() => setRecapOpen(true)}
        onPrev={() => { lastManualScroll.current = 0; step(-1); }} onNext={() => { lastManualScroll.current = 0; step(1); }}
        speed={speed} speeds={SPEEDS} onSpeed={setSpeed}
        onVoices={() => setLabOpen(true)}
        autoResume={autoResume} onAutoResume={setAutoResume}
        para={para} totalParas={book.paragraphs.length}
        atStart={index === 0} atEnd={index >= last}
        hasCast={book.characters.length > 0}
        audioAvailable={audioAvailable} audioError={audioError} audioNotice={audioNotice}
      />}
    </>
  );
}
