import { isAutoplayBlock } from "../audio/mediaErrors";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getAccessToken, setAccessToken } from "../accessToken";
import {
  avatarUrl, fetchAvatars, fetchBook, fetchCast, fetchFaces, fetchHealth, fetchTts, fetchVoices, speak, fetchScopedRecap,
  type Book, type Cast, type Face, type Health,
} from "../api";
import {
  PRESETS, PREVIEW_LINE, castVoice, choiceFor, dropTalkReferences, createCastSaver, emptyCast, isNovel, loadTalkChoices, migrateOldOverrides,
  narratorGender, readingFaceId, saveTalkChoices, talkFaceId, talkVoice, voiceMode, withTalkChoice, withVoiceMode, type TalkChoice,
  type TalkChoices,
} from "../reader/castLogic";
import { DEMO_SPEAKER, DEMO_START_PARA, firstChunkAtOrAfter, isDemo } from "../demo";
import BookText from "../reader/BookText";
import PlayerBar from "../reader/PlayerBar";
import RecapPanel, { nothingYet, recapHeading } from "../reader/RecapPanel";
import type { RecapChoice } from "../reader/RecapMenu";
import { ReaderEngine, type AudioLike, type ClipSource } from "../reader/ReaderEngine";
import type { FollowAudio } from "../reader/clipFollow";
import { AudioLevelMeter } from "../reader/audioLevel";
import { SPEECH_IDLE, stepSpeech } from "../reader/speechLevel";
import { loopsToPreload, showReadingFace } from "../reader/readFace";
import { lipSyncTargetFor, readClipsOn, readFaceFor, speechSeconds, type ReadFaces } from "../reader/readLipSync";
import Stage from "../reader/Stage";
import { toggleMode, validSpeaker } from "../reader/talkbackLogic";
import TalkbackView, { ModeToggle } from "../reader/TalkbackView";
import { loadNaturalSpeed, saveNaturalSpeed, speedTagFor, type SpeedTag } from "../reader/naturalSpeed";
import { loadSpoilers, saveSpoilers } from "../reader/spoilers";
import VoiceLab from "../reader/VoiceLab";
import { spend, type SpendSnapshot } from "../spend";
import { openClipVideo } from "../talk/clipVideo";
import { FaceSync } from "../talk/faceSync";
import { runTool, type ToolActions, type ToolContext } from "../talk/tools";
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
  const [naturalSpeed, setNaturalSpeed] = useState(loadNaturalSpeed); // #78: opt-in, remembered
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
  const [recapOpen, setRecapOpen] = useState<RecapChoice | null>(null); // text-only recap (no Talkback without a voice)
  const [cast, setCast] = useState<Cast>(emptyCast);
  const [castLoaded, setCastLoaded] = useState(false); // false until the saved cast arrived: then the server picks voices
  const castEdited = useRef(false); // a local edit happened: late server loads must not overwrite it
  const [animating, setAnimating] = useState<Set<string>>(new Set()); // lives here so closing the panel keeps the spinner
  const [animErrors, setAnimErrors] = useState<Record<string, string>>({});
  const [castError, setCastError] = useState<string | null>(null);
  const [faces, setFaces] = useState<Face[]>([]);
  const [cloned, setCloned] = useState<Record<string, string>>({});
  // Talkback's Voice / Avatar picks, one per "Talk to" character (#106), kept per book in this browser
  const [talkChoices, setTalkChoices] = useState<TalkChoices>(() => loadTalkChoices(bookId));
  useEffect(() => { setTalkChoices(loadTalkChoices(bookId)); }, [bookId]);
  // read-mode lip-sync (#80, always on since #92): the face's clip video is driven by its own sync
  const [readClipShown, setReadClipShown] = useState(false);
  const readSync = useRef<FaceSync | null>(null);
  if (!readSync.current) readSync.current = new FaceSync(setReadClipShown);
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
  const speedRef = useRef<{ speed: number; tag: SpeedTag | null }>({ speed: 1, tag: null }); // what a rebuilt engine starts with
  const playingRef = useRef(false);
  indexRef.current = index;
  playingRef.current = playing;
  const modeRef = useRef(mode);
  modeRef.current = mode;

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
      fetchTts: async (chunk, voice, speedTag) => {
        const r = await fetchTts(book.id, chunk.id, voice, demo, speedTag); // demo: pre-rendered only
        if (!r.cached) spend.addTts(chunk.tts_text.length); // only freshly synthesised speech costs money
        return r;
      },
      createAudio: (url) => new Audio(url) as unknown as AudioLike, // the engine only assigns a plain onended callback
      sync: readSync.current as FaceSync,
    }, indexRef.current);
    engineRef.current = engine;
    engine.setSpeed(speedRef.current.speed, speedRef.current.tag); // a rebuilt engine keeps the speed (and tag) the UI shows
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
        speech = sounding ? stepSpeech(speech, lvl, now - last) : { ...SPEECH_IDLE, peak: speech.peak }; // keep the voice's peak across pauses
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


  const readingVoiceFor = (id: string) => (book ? castVoice(book, cast, id) : "marcus");
  const talkVoiceFor = (id: string) => (book ? talkVoice(book, cast, choiceFor(talkChoices, id), id) : "marcus");
  const faceById = (id: string | null) => (id ? faces.find((f) => f.id === id) : undefined);
  const talkFaceFor = (id: string) =>
    book ? faceById(talkFaceId(book, cast, choiceFor(talkChoices, id), id, talkVoiceFor(id), faces)) : undefined;
  const narratorVoice = book ? castVoice(book, cast, "narrator") : null;
  const current = chunks[index];
  // read mode: who reads a chunk, by the reader's own voice -> face resolution (one or two voices); a speaker
  // without a face is read by the narrator's face (reader/readLipSync)
  const readFaces: ReadFaces = {
    faceFor: (s) => (book ? faceById(readingFaceId(book, cast, s)) ?? null : null),
    voiceFor: (s) => (book ? castVoice(book, cast, isNovel(book) ? s : "narrator") : "marcus"),
  };
  const latestFaces = useRef(readFaces);
  latestFaces.current = readFaces;
  // the face of whoever reads aloud right now (read mode)
  // mounted while the first chunk still loads, so its talking loop is ready on the first word (see reader/readFace)
  const readingFace = book && current && showReadingFace({ playing, loading }) ? readFaceFor(readFaces, current.speaker) : null;
  const nextChunk = chunks[index + 1];
  const nextReadingFace = book && nextChunk && playing ? readFaceFor(readFaces, nextChunk.speaker) : null;
  const preloadLoops = loopsToPreload(readingFace, nextReadingFace);
  // the engine's clip source: the chunk's clip on the face on screen for it, whatever voice reads it (#113; null: no
  // face shown, audio only). Always on, at every speed: the clip plays at the voice's rate (#105).
  const clipsOn = readClipsOn(audioAvailable);
  const clipSource = useMemo<ClipSource | null>(() => {
    if (!clipsOn) return null;
    return (chunk, audioUrl, voice) => {
      const target = lipSyncTargetFor(latestFaces.current, chunk.speaker, voice);
      if (!target) return null;
      return openClipVideo(audioUrl, target.voice, target.faceId, () => spend.addAvatar(speechSeconds(chunk.tts_text)));
    };
  }, [clipsOn]);
  useEffect(() => { engineRef.current?.setClipSource(clipSource); }, [clipSource, book, chunks, audioAvailable, demo]);
  const voiceOptions = useMemo(
    () => [...PRESETS.map((v) => ({ value: v, label: v })), ...Object.entries(cloned).map(([n, v]) => ({ value: v, label: `${n} (cloned)` }))],
    [cloned]);

  // The reader tools a character can call (#72): the snapshot and the actions read the latest state through refs, so
  // the runner handed to the talk loop never goes stale. The spoiler guard lives in runTool.
  const toolDeps = useRef<{ ctx: () => ToolContext; act: ToolActions } | null>(null);
  toolDeps.current = book ? {
    ctx: () => ({
      bookId: book.id, kind: book.kind, sections: book.sections, paragraphs: book.paragraphs, characters: book.characters, chunks,
      index: indexRef.current, readerPara: readerPara(chunks, indexRef.current), spoilers: spoilers && book.kind === "novel",
      playing: engineRef.current ? ["playing", "loading"].includes(engineRef.current.snapshot().state) : playingRef.current,
      speed, speeds: SPEEDS, voiceMode: voiceMode(cast), narrator: narratorGender(cast), inSeries: !!book.series,
      textbook: book.kind === "textbook",
    }),
    act: {
      seek: (i) => { lastManualScroll.current = 0; const next = clampIndex(i, chunks.length); if (engineRef.current) engineRef.current.seek(next); else setIndex(next); },
      pause: () => (engineRef.current ? engineRef.current.pause() : setPlaying(false)),
      setVoiceMode: (m) => changeCast(withVoiceMode(cast, m)),
      setNarrator: (g) => changeCast({ ...cast, narrator_gender: g }),
      setSpeed,
      openRecap: (scope, section) => recap(section ? { scope: "chapter", label: "Chapter recap", section: section.id }
        : { scope, label: scope === "chapter" ? (book.kind === "textbook" ? "This chapter" : "Last chapter") : scope === "book" ? "This book so far" : "The series so far" }),
      showCast: () => setLabOpen(true),
    },
  } : null;
  const toolRunner = useCallback((name: string, args: Record<string, unknown>) => {
    const d = toolDeps.current;
    if (!d) return { ok: false, output: "The book is still loading.", chip: null };
    return runTool(name, args, d.ctx(), d.act);
  }, []);

  // Talk mode: push-to-talk questions, answered from what has been read so far (needs the Boson key).
  const talk = useTalk({
    bookId,
    enabled: audioAvailable && !!book,
    speakerId,
    autoResume,
    health,
    faceIdFor: (id) => talkFaceFor(id)?.id ?? null,
    narratorFaceId: () => talkFaceFor("narrator")?.id ?? null,
    spoilers: spoilers && book?.kind === "novel",
    talkback: mode === "talk",
    voiceFor: talkVoiceFor,
    narratorVoice: audioAvailable ? narratorVoice : null,
    tools: toolRunner,
    bookKind: book?.kind,
    reader: {
      isPlaying: () => (engineRef.current ? ["playing", "loading"].includes(engineRef.current.snapshot().state) : playingRef.current),
      currentIndex: () => indexRef.current,
      currentPara: () => readerPara(chunks, indexRef.current),
      pause: () => (engineRef.current ? engineRef.current.pause() : setPlaying(false)),
      play: () => (engineRef.current ? engineRef.current.play() : setPlaying(true)),
      resumeAt: (i) => {
        if (modeRef.current === "talk") setMode("read"); // a tool asked for reading to go on (#72): show the page, keep the chat
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

  // natural speed (#78) needs new audio, which a demo (pre-rendered only) cannot have
  const speedTag = speedTagFor(speed, naturalSpeed && !demo);
  speedRef.current = { speed, tag: speedTag };
  useEffect(() => { engineRef.current?.setSpeed(speed, speedTag); }, [speed, speedTag, audioAvailable, book, chunks, demo]);
  useEffect(() => { saveNaturalSpeed(naturalSpeed); }, [naturalSpeed]);
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

  /** Recap menu: the narrator posts the chosen recap into the Talkback chat and reads it aloud (text panel without a voice). */
  const recap = (c: RecapChoice) => {
    if (!book) return;
    if (!audioAvailable) { setRecapOpen(c); return; }
    if (mode !== "talk") changeMode("talk");
    const at = para;
    void talk.narrate(c.label, async () => {
      const r = await fetchScopedRecap(book.id, at, c.scope, c.section);
      return { text: r.text, heading: recapHeading(c, r), empty: nothingYet(at) };
    });
  };

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
  const changeTalkChoices = (next: TalkChoices) => { setTalkChoices(next); saveTalkChoices(bookId, next); };
  /** A Voice / Avatar pick in Talkback belongs to the character being talked to. */
  const changeTalk = (next: TalkChoice) => changeTalkChoices(withTalkChoice(talkChoices, speakerId, next));
  const reloadCast = () => { void saver.flush().then(() => fetchCast(bookId)).then((c) => { setCast(c); }).catch(() => {}); };
  const reloadFaces = () => { fetchFaces().then(setFaces).catch(() => {}); fetchVoices().then(setCloned).catch(() => {}); };
  // one short fixed line per voice through the TTS cache: only the first preview of a voice costs anything
  const preview = async (voice: string) => {
    try {
      const r = await speak(PREVIEW_LINE, voice);
      if (!r.cached) spend.addTts(PREVIEW_LINE.length);
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
                          choice={choiceFor(talkChoices, speakerId)} onChoice={changeTalk} voices={voiceOptions}
                          spoilers={spoilers} onSpoilers={(on) => { setSpoilers(on); saveSpoilers(bookId, on); }}
                          portrait={book.characters.find((c) => c.id === speakerId)?.portrait ?? book.narrator?.portrait ?? null}
                          onBack={() => changeMode("read")} onRecap={recap} onVoices={() => setLabOpen(true)} />
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
                 readingFace={readingFace} preloadLoops={preloadLoops}
                 readSpeaking={readSpeaking} levelRef={levelRef}
                 readLipSync={{ sync: readSync.current as FaceSync, shown: readClipShown }} />
        )}
      </div>
      {labOpen && (
        <VoiceLab book={book} cast={cast} onCast={changeCast} castError={castError}
                  cloned={cloned} onCloned={setCloned} animating={animating} setAnimating={setAnimating} animErrors={animErrors} setAnimErrors={setAnimErrors} faces={faces} onFaces={setFaces}
                  onFacesChanged={() => { reloadFaces(); reloadCast(); }}
                  onRemoved={(gone) => {
                    changeTalkChoices(dropTalkReferences(talkChoices, gone));
                  }}
                  onPreview={preview} audioAvailable={audioAvailable} onClose={() => setLabOpen(false)} />
      )}
      {recapOpen && (
        <RecapPanel bookId={book.id} para={para} choice={recapOpen} onClose={() => setRecapOpen(null)} />
      )}
      {debugEnabled() && <DebugDrawer entries={talk.logs} spend={spendNow} />}
      {mode === "read" && <PlayerBar
        playing={playing} loading={loading} onToggle={toggle} onRecap={recap} inSeries={!!book.series} textbook={book.kind === "textbook"}
        onPrev={() => { lastManualScroll.current = 0; step(-1); }} onNext={() => { lastManualScroll.current = 0; step(1); }}
        speed={speed} speeds={SPEEDS} onSpeed={setSpeed}
        naturalSpeed={naturalSpeed && !demo} onNaturalSpeed={demo ? undefined : setNaturalSpeed}
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
