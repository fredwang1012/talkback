import type { LiveFace } from "./liveFace";
import { RoutedPlayer, createLiveSpeaker, createRoutedSpeaker } from "./livePlayers";
import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { fetchRealtimeKey, fetchRetrieve, speak, streamAnswer, type Health } from "../api";
import { MicCapture } from "../audio/capture";
import { RingPlayer } from "../audio/playback";
import { spend } from "../spend";
import type { LogEntry } from "../ui/DebugDrawer";
import { addNarration, addQuestion, addToolChip, appendSentenceTo, cutHeard, fillNarration, finishAnswer, loadChat, markInterrupted, priorTurns, saveChat, setSources, setTextOf, type ChatMessage } from "./chatLog";
import { createReadAloud, speechParts } from "../reader/readAloud";
import { answerLanguage, detectLanguage, type Detected } from "./language";
import { localGateForcedHere } from "./handsFree";
import { FaceSync } from "./faceSync";
import { createPlayingGate, type TimingEvent } from "./speechGate";
import { answerClip, lipSyncClip } from "./clipVideo";
import { createEmotionMemo } from "./answerEmotion";
import { createSentenceSpeaker, type Clip } from "./sentencePlayer";
import { createReplayPlayer, createReplayStore, runPcm, runSentences, type ReplayItem } from "./replay";
import { TalkClient, type MicLike, type PlayerLike } from "./TalkClient";
import { TalkController, type Latency, type ReaderControl, type TalkState } from "./TalkController";
import { type BookKind, type ToolRunner } from "./tools";
import { answerModeHere } from "./answerMode";
import { sessionTools, webSearchAllowed } from "./webSearch";

/** Gemini (via /api/answer) writes the answer and Boson TTS speaks it only when the server says so; else Realtime answers itself. */
export const answersViaGemini = (health: Health | null): boolean => health?.answer_backend === "gemini";

export type TalkUi = {
  state: TalkState;
  caption: string;
  toast: string | null;
  level: number;
  latency: Latency | null;
  speaking: boolean;
  question: string;
  language: Detected | null;
  faceSync: FaceSync; // owns the persistent lip-sync <video> and keeps it locked to the voice
  clipShown: boolean; // the lip-sync video has a decoded frame and is in step: show it over the talk loop
  narrating?: boolean; // a narrator recap is being read aloud now: the narrator's face shows (lip-synced like answers)
  handsFree?: boolean; // #10: the mic stays open; speaking interrupts the character and silence ends the question
  setHandsFree?: (on: boolean) => void;
  searching?: boolean; // B2: Boson's server-side web_search is running for the current answer ("Searching…" chip)
  logs: LogEntry[];
  pttDown: () => void;
  pttUp: () => void;
  setSpeaker: (id: string) => void;
  messages: ChatMessage[]; // the Talkback conversation for this book
  ask: (text: string) => void; // a typed question
  stop: () => void; // stop speaking
  clearMessages: () => void;
  replayable: ReadonlySet<number>; // reply (message) ids that can be played: voice still in memory, or a narrator recap
  replayingId: number | null; // the reply (or recap) whose voice is playing now, live answer included
  liveId: number | null; // the answer being spoken right now: its button pauses / resumes it in place (#90)
  replay: (id: number) => void; // play / pause one reply again (voice, and its avatar clip if there was one); costs nothing
  isPaused: (id: number) => boolean; // a reply paused part-way (Play resumes it)
  /** Post a narrator recap into the chat (shown at once, filled when `load` resolves) and read it aloud in the
   * narrator voice. Never sent to the model as a question, and left out of the chat memory (see priorTurns). */
  narrate: (heading: string, load: () => Promise<Narration>) => Promise<void>;
  dismissToast: () => void;
};

/** A loaded recap: its text, a better heading (e.g. with the chapter name), and what to say when the text is empty. */
export type Narration = { text: string; heading?: string; empty?: string };

type Args = {
  bookId: string;
  enabled: boolean; // only when the server has a Boson key
  reader: ReaderControl;
  speakerId: string;
  voiceFor: (speakerId: string) => string;
  autoResume: boolean;
  health: Health | null;
  faceIdFor?: (speakerId: string) => string | null; // the face shown for a speaker's answers (#113: any face lip-syncs any voice); null = none
  narratorFaceId?: () => string | null; // the same for the narrator's face, which reads recaps aloud
  spoilers?: boolean; // full-book mode: the reader allows spoilers (novels only)
  talkback?: boolean; // the dedicated conversation view is open: a mic tap never toggles reading
  narratorVoice?: string | null; // reads recaps aloud in the chat
  /** Picks the tools declared to the Realtime session: a textbook gets section / topic tools and no cast (tools.ts). */
  bookKind?: BookKind;
  /** The book's language (ISO 639-3, #117): answers and read-aloud recaps of a book in another language are voiced in it. */
  bookLanguage?: string;
  tools?: ToolRunner; // the reader tools the character may call (#72): jump, read chapter, pause/resume, voices, speed, recap, bookmark, cast
  /** #140: a live preset face in a LiveKit room. While set, answers are spoken through it (its own audio and video, in
   * sync) instead of local playback and per-sentence lip-sync clips. Null = today's clips. */
  liveFace?: LiveFace | null;
};

/** Owns the Talk session for the reader screen. The real mic, speaker and socket are created once per book. */
export function useTalk({ bookId, enabled, reader, speakerId, voiceFor, autoResume, health, faceIdFor = () => null, narratorFaceId = () => null, spoilers = false, talkback = false, narratorVoice = null, tools, bookKind, bookLanguage, liveFace = null }: Args): TalkUi {
  const [state, setState] = useState<TalkState>("reading");
  const [caption, setCaption] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const [latency, setLatency] = useState<Latency | null>(null);
  const [speaking, setSpeaking] = useState(false);
  const [question, setQuestion] = useState("");
  const [language, setLanguage] = useState<Detected | null>(null);
  const [clipShown, setClipShown] = useState(false);
  const faceSync = useRef<FaceSync | null>(null);
  if (!faceSync.current) faceSync.current = new FaceSync(setClipShown);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logSeq = useRef(0);
  const [messages, setMessagesState] = useState<ChatMessage[]>(() => loadChat(bookId));
  const msgs = useRef(messages);
  const via = useRef<"voice" | "typed">("voice");
  const setMessages = useCallback((fn: (m: ChatMessage[]) => ChatMessage[]) => {
    const next = fn(msgs.current);
    if (next === msgs.current) return;
    msgs.current = next;
    saveChat(bookIdRef.current, next);
    setMessagesState(next);
  }, []);
  const bookIdRef = useRef(bookId);
  useEffect(() => { replayer.current?.stop(); narration.current?.reset(); replayStore.current.clear(); bookIdRef.current = bookId; msgs.current = loadChat(bookId); setMessagesState(msgs.current); }, [bookId]);
  const controller = useRef<TalkController | null>(null);
  const [replayable, setReplayable] = useState<ReadonlySet<number>>(new Set());
  const [replayingId, setReplayingId] = useState<number | null>(null);
  const replayStore = useRef(createReplayStore(undefined, () => setReplayable(new Set(replayStore.current.ids()))));
  const replayIdRef = useRef(0);
  const replayer = useRef<ReturnType<typeof createReplayPlayer> | null>(null);
  const pendingId = () => msgs.current.findLast((x) => x.pending)?.id;
  // narrator recaps: one read-aloud for all of them, keyed by message id, in the narrator voice
  const narratorVoiceRef = useRef(narratorVoice);
  narratorVoiceRef.current = narratorVoice;
  const bookLanguageRef = useRef(bookLanguage);
  bookLanguageRef.current = bookLanguage;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const [narratingKey, setNarratingKey] = useState<string | null>(null);
  const [livePaused, setLivePaused] = useState(false);
  const [handsFree, setHandsFreeState] = useState(false);
  const [searching, setSearching] = useState(false);
  /** The message of the answer being given now (retrieving or speaking), if any. */
  const liveMsgId = () => {
    const c = controller.current;
    return c && ["retrieving", "answering"].includes(c.state) ? pendingId() : undefined;
  };
  const narration = useRef<ReturnType<typeof createReadAloud> | null>(null);
  if (!narration.current) {
    // lip-synced on the narrator's face like an answer (same clip path, start gate and face sync); the talk loop or
    // the still face stands in when no face is shown or a render fails
    narration.current = createReadAloud(() => createSentenceSpeaker(async (text) => {
      const voice = narratorVoiceRef.current ?? "";
      const lang = bookLanguageRef.current; // a recap of a book in another language is in that language (#117)
      const r = lang && lang !== "eng" ? await speak(text, voice, undefined, lang) : await speak(text, voice);
      if (!r.cached) spend.addTts(text.length);
      return lipSyncClip(r.audio_url, voice, latest.current.narratorFaceId());
    }, undefined, { sync: faceSync.current as FaceSync, onPlaying: setSpeaking }), setNarratingKey, (e) => setToast(e instanceof Error ? e.message : String(e)));
  }
  const clientRef = useRef<TalkClient | null>(null);
  const viaGemini = answersViaGemini(health);
  // Each character has their own memory (priorTurns is per speaker). A Realtime session holds every turn said in it,
  // so after a speaker switch it is dropped once no answer is in progress: the next press opens a session seeded with
  // that character's own turns (historyProvider), instead of one carrying the other character's chat (or none of theirs).
  const viaGeminiRef = useRef(viaGemini);
  viaGeminiRef.current = viaGemini;
  const sessionSpeaker = useRef(speakerId);
  const resetIfSpeakerChanged = useRef(() => {});
  resetIfSpeakerChanged.current = () => {
    const c = controller.current;
    if (!c || viaGeminiRef.current || sessionSpeaker.current === latest.current.speakerId) return;
    // an answer in progress plays on and the reset waits until it ends, or until the reader cuts in: a new question
    // (listening, also a hands-free barge-in straight from answering) always goes to the new character's session
    if (!["reading", "talk-idle", "resuming", "listening"].includes(c.state)) return;
    sessionSpeaker.current = latest.current.speakerId;
    clientRef.current?.resetConversation();
  };
  const latest = useRef({ reader, voiceFor, speakerId, faceIdFor, narratorFaceId, spoilers, tools });
  // #140: read per reply, never a dependency of the session effect (a rebuild would drop a question asked meanwhile)
  const healthRef = useRef(health);
  healthRef.current = health;
  const liveRef = useRef<LiveFace | null>(liveFace);
  liveRef.current = liveFace;
  latest.current = { reader, voiceFor, speakerId, faceIdFor, narratorFaceId, spoilers, tools };

  useEffect(() => {
    if (!enabled) return;
    let answerVoice = "";
    let answerFaceId: string | null = null;
    let answerMsgId: number | undefined;
    let answerLang: string | null = null; // #135: ISO 639-3 when the reader's questions are clearly not English
    let lastQuestion = "";
    const answerEmotion = createEmotionMemo();
    let rtMsgId: number | undefined;
    // the answer bubble of the current question (made by onTranscript): text only ever goes into it, never into an
    // older (interrupted, finished) one, so answers cannot stack in one bubble
    let liveAnswerId: number | undefined;
    let talk: TalkController | null = null;
    const gate = createPlayingGate((on) => talk?.notePlaying(on));
    const logTiming = (e: TimingEvent) => {
      console.info("[talk] timing", e);
      setLogs((l) => [...l.slice(-299), { id: ++logSeq.current, dir: "info", type: `timing:${e.stage}`, at: Date.now(), raw: e }]);
    };
    // B1: on the Realtime answer path Boson speaks the answer itself by default; `?answers=text` has the model write it
    // and Higgs TTS speak each sentence with its emotion tag, lip-synced, like a Gemini answer (slower to start)
    const answerMode = viaGemini ? "audio" : answerModeHere();
    /** B2: the tool list for this book right now; web_search only for a textbook or a novel with spoilers on. */
    const searchOn = () => healthRef.current?.web_search !== false; // the server's WEB_SEARCH switch
    const toolsNow = () => sessionTools(bookKind, latest.current.spoilers, undefined, searchOn());
    /** The voice, face, language and bubble of an answer are fixed when it starts, so a mid-answer speaker switch cannot change them. */
    const startAnswer = (speaker: string) => {
      answerVoice = latest.current.voiceFor(speaker);
      answerFaceId = latest.current.faceIdFor(speaker);
      answerMsgId = liveAnswerId ?? pendingId();
      answerEmotion.clear();
      answerLang = answerLanguage([...priorTurns(msgs.current, speaker).filter((t) => t.role === "user").map((t) => t.text), lastQuestion], bookLanguageRef.current);
    };
    const client = new TalkClient({
      tokenProvider: fetchRealtimeKey,
      tools: toolsNow(),
      answerMode,
      createPlayer: (onSpeaking) => new RoutedPlayer(() => liveRef.current, new RingPlayer(onSpeaking) as unknown as PlayerLike, onSpeaking),
      createMic: (onChunk, onLevel) => new MicCapture(onChunk, onLevel) as unknown as MicLike,
      ...(viaGemini ? {} : {
        // text mode (B1): each sentence through /api/speak (emotion tag, the speaker's voice, the answer's language) and
        // its lip-sync clip on the face shown; the same sentence player, start gate and face sync as the Gemini path
        createSpeaker: (onPlaying: (on: boolean) => void) => createRoutedSpeaker(() => liveRef.current, (face) => createLiveSpeaker(async (text) => {
          const r = await speak(text, answerVoice, answerEmotion.get(text), answerLang);
          if (!r.cached) spend.addTts(text.length);
          if (answerMsgId !== undefined) replayStore.current.addSentence(answerMsgId, text, { audio_url: r.audio_url });
          return r.audio_url;
        }, face, { onPlaying: (on: boolean) => { gate.playing(on); onPlaying(on); } }), createSentenceSpeaker(async (text) => {
          const voice = answerVoice;
          const r = await speak(text, voice, answerEmotion.get(text), answerLang);
          if (!r.cached) spend.addTts(text.length);
          const clip = lipSyncClip(r.audio_url, voice, answerFaceId);
          if (answerMsgId !== undefined) replayStore.current.addSentence(answerMsgId, text, clip);
          return clip;
        }, undefined, { sync: faceSync.current as FaceSync, onPlaying: (on: boolean) => { gate.playing(on); onPlaying(on); }, onTiming: logTiming })),
        onSentence: (text: string, emotion: string | null, first: boolean) => {
          if (first) startAnswer(latest.current.speakerId);
          answerEmotion.note(text, emotion ?? undefined);
        },
      }),
      onLevel: (rms) => { setLevel(rms); talk?.noteLevel(rms); },
      onAudioChunk: (b64, first) => { if (first) rtMsgId = pendingId(); if (rtMsgId !== undefined) replayStore.current.addPcm(rtMsgId, b64); },
      historyProvider: () => priorTurns(msgs.current, latest.current.speakerId),
      onLog: (dir, type, raw) => {
        const r = raw as { audio?: string; delta?: string };
        if (dir === "send" && type === "input_audio_buffer.append" && r.audio) spend.addAudioIn(r.audio.length);
        if (dir === "recv" && type === "response.output_audio.delta" && r.delta) spend.addAudioOut(r.delta.length);
        setLogs((l) => [...l.slice(-299), { id: ++logSeq.current, dir, type, at: Date.now(), raw }]);
      },
    });
    talk = new TalkController({
      session: client,
      reader: {
        isPlaying: () => latest.current.reader.isPlaying(),
        currentIndex: () => latest.current.reader.currentIndex(),
        currentPara: () => latest.current.reader.currentPara(),
        pause: () => latest.current.reader.pause(),
        play: () => latest.current.reader.play(),
        resumeAt: (i) => latest.current.reader.resumeAt(i),
      },
      retrieve: (query, para, speaker) => {
        const spoilers = latest.current.spoilers;
        client.setTools(toolsNow()); // B2: rebuilt every turn, so the spoilers switch takes web_search away (or gives it) at once
        return fetchRetrieve(bookId, query, para, speaker, spoilers, {
          textAnswer: answerMode === "text", webSearch: webSearchAllowed(bookKind, spoilers, searchOn()), history: priorTurns(msgs.current, speaker) });
      },
      ...(viaGemini ? {
        answer: (query, para, speaker, onSentence, signal) => {
          lastQuestion = query;
          startAnswer(speaker);
          return streamAnswer(bookId, query, para, speaker, (t, emotion) => { answerEmotion.note(t, emotion); const id = answerMsgId; setMessages((m) => appendSentenceTo(m, id, t)); onSentence(t); }, signal, latest.current.spoilers, priorTurns(msgs.current, speaker), (s) => setMessages((m) => setSources(m, s)));
        },
        speaker: createRoutedSpeaker(() => liveRef.current, (face) => createLiveSpeaker(async (text) => {
          const r = await speak(text, answerVoice, answerEmotion.get(text), answerLang);
          if (answerMsgId !== undefined) replayStore.current.addSentence(answerMsgId, text, { audio_url: r.audio_url });
          return r.audio_url;
        }, face, { onPlaying: gate.playing }), createSentenceSpeaker(async (text) => {
          const voice = answerVoice;
          // always lip-synced when a face is shown (#92, any face since #113): the first word waits (bounded) until
          // this clip can start with it from 0 (start gate: sentencePlayer, faceSync). A non-English answer is voiced
          // in its language (#135; its TTS has no word timestamps, which answers never needed)
          const clip = await answerClip(speak, text, voice, { emotion: answerEmotion.get(text), language: answerLang, faceId: answerFaceId });
          if (answerMsgId !== undefined) replayStore.current.addSentence(answerMsgId, text, clip);
          return clip;
        }, undefined, { sync: faceSync.current as FaceSync, onPlaying: gate.playing, onTiming: logTiming })),
      } : {}),
      voiceFor: (id) => latest.current.voiceFor(id),
      tools: (name, args) => {
        const run = latest.current.tools;
        if (!run) return { ok: false, output: "Tools are not available here.", chip: null };
        return run(name, args);
      },
      onToolRun: (chip) => setMessages((m) => addToolChip(m, chip)),
      onInterrupted: () => setMessages(markInterrupted),
      onHeard: (text) => setMessages((m) => cutHeard(m, text)), // #135: the cut-off reply keeps the words heard
      // B2: the "Searching…" chip while Boson searches; a lasting chip once its answer followed; links under the reply
      onSearching: (active, completed) => { setSearching(active); if (!active && completed) setMessages((m) => addToolChip(m, "Looked it up on the web")); },
      onSources: (sources) => setMessages((m) => setSources(m, { sources })),
      serverTurns: !localGateForcedHere(), // #135: hands-free on Boson's turn detection unless ?gate=local
      onState: (st) => { setState(st); if (st !== "answering") setLivePaused(false); if (["listening", "retrieving", "answering"].includes(st)) { replayer.current?.stop(); narration.current?.stop(); } if (["reading", "talk-idle", "resuming"].includes(st)) { setMessages(finishAnswer); resetIfSpeakerChanged.current(); } if (st === "listening") resetIfSpeakerChanged.current(); },
      onToast: setToast,
      onCaption: (text) => { setCaption(text); if (!viaGemini && text) setMessages((m) => setTextOf(m, liveAnswerId, text)); },
      onSpeaking: setSpeaking,
      onTiming: logTiming,
      onTranscript: (text) => {
        lastQuestion = text;
        setQuestion(text); setLanguage(detectLanguage(text));
        setMessages((m) => addQuestion(m, text, latest.current.speakerId, via.current));
        liveAnswerId = pendingId(); // the new empty answer bubble of this question
        via.current = "voice";
      },
      onLatency: (l) => { setLatency(l); console.info("[talk] latency", l); },
    });
    controller.current = talk;
    clientRef.current = client;
    const sync = faceSync.current;
    const replaySpeaker = createSentenceSpeaker(async (text) => {
      const part = (replayStore.current.get(replayIdRef.current) as Extract<ReplayItem, { kind: "sentences" }> | undefined)?.parts.find((p) => p.text === text);
      if (!part) throw new Error("That reply is no longer available.");
      return part.clip as Clip;
    }, undefined, { sync: sync as FaceSync, onPlaying: setSpeaking });
    replayer.current = createReplayPlayer(replayStore.current, (item) => {
      if (item.kind === "pcm") return runPcm(item.chunks);
      return runSentences(item.parts, replaySpeaker);
    }, setReplayingId, (e) => setToast(e instanceof Error ? e.message : String(e)));
    return () => {
      replayer.current?.stop(); replayer.current = null; narration.current?.reset(); gate.reset(); sync?.leave(); talk?.dispose(); client.close(); controller.current = null; clientRef.current = null; setState("reading"); setSpeaking(false); setHandsFreeState(false); setSearching(false); };
  }, [bookId, enabled, viaGemini, bookKind]);

  useEffect(() => { controller.current?.setAutoResume(autoResume); }, [autoResume, enabled]);
  useEffect(() => { controller.current?.setTalkback(talkback); }, [talkback, enabled]);
  useEffect(() => { resetIfSpeakerChanged.current(); void controller.current?.setSpeaker(speakerId); }, [speakerId, enabled]);
  // the same speaker with a different resolved voice must reach an open realtime session too
  const voiceKey = voiceFor(speakerId);
  useEffect(() => { void controller.current?.refreshVoice(); }, [voiceKey, enabled]);

  const liveId = ["retrieving", "answering"].includes(state) ? messages.findLast((m) => m.pending)?.id ?? null : null;
  const playable = useMemo(() => {
    const ids = new Set(replayable);
    if (narratorVoice && enabled) for (const m of messages) if (m.role === "narrator" && m.text && !m.loading && !m.quiet) ids.add(m.id);
    return ids;
  }, [replayable, messages, narratorVoice, enabled]);

  return {
    state, caption, toast, level, latency, speaking, question, language, faceSync: faceSync.current, clipShown, logs,
    narrating: narratingKey !== null,
    handsFree,
    searching,
    setHandsFree: useCallback((on: boolean) => {
      const c = controller.current;
      if (!c) return;
      setHandsFreeState(on);
      void c.setHandsFree(on).then(() => { if (!c.isHandsFree) setHandsFreeState(false); }); // the mic was refused: back off
    }, []),
    pttDown: useCallback(() => { void controller.current?.pttDown(); }, []),
    pttUp: useCallback(() => { void controller.current?.pttUp(); }, []),
    messages,
    ask: useCallback((text: string) => {
      via.current = "typed";
      void controller.current?.ask(text); // onTranscript fires synchronously inside ask, before its first await
      via.current = "voice"; // an ignored (empty) ask must not label the next spoken question as typed
    }, []),
    stop: useCallback(() => { void controller.current?.stop(); narration.current?.stop(); }, []),
    clearMessages: useCallback(() => { controller.current?.endQuiz(); void controller.current?.stop(); replayer.current?.stop(); narration.current?.reset(); replayStore.current.clear(); clientRef.current?.resetConversation(); setMessages(() => []); }, [setMessages]),
    replayable: playable, liveId,
    replayingId: narratingKey !== null ? Number(narratingKey) : replayingId ?? (liveId !== null && !livePaused ? liveId : null),
    isPaused: (id: number) => {
      if (id === liveId) return livePaused;
      if (replayer.current?.paused() === id) return true;
      const m = msgs.current.find((x) => x.id === id);
      return m?.role === "narrator" && narratingKey !== String(id) && !!narration.current?.paused(String(id), speechParts(m.text));
    },
    narrate: useCallback(async (heading: string, load: () => Promise<Narration>) => {
      const live = controller.current && !["reading", "talk-idle"].includes(controller.current.state);
      if (live) void controller.current?.stop(); // a recap is not a question: the live answer stops, nothing is sent to the model
      replayer.current?.stop();
      narration.current?.stop();
      const book = bookIdRef.current;
      let id = 0;
      setMessages((m) => { const next = addNarration(m, heading); id = next[next.length - 1].id; return next; });
      let r: Narration;
      try { r = await load(); } catch (e) {
        if (bookIdRef.current === book) setMessages((m) => fillNarration(m, id, null));
        setToast(`Could not load the recap: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
      if (bookIdRef.current !== book) return; // another book is open now: this chat is not on screen
      const text = r.text.trim();
      setMessages((m) => fillNarration(m, id, text || (r.empty ?? "Nothing to recap yet — keep reading."), r.heading, !text));
      if (text && narratorVoiceRef.current && enabledRef.current) narration.current?.restart(String(id), speechParts(text));
    }, [setMessages]),
    replay: useCallback((id: number) => {
      const msg = msgs.current.find((x) => x.id === id);
      const c = controller.current;
      if (c && id === liveMsgId()) { // the answer being spoken: pause / resume it in place
        if (c.answerPaused) {
          narration.current?.stop(); replayer.current?.stop();
          if (c.resumeAnswer()) setLivePaused(false);
        } else if (c.pauseAnswer()) setLivePaused(true);
        else if (c.state === "answering") void c.stop(); // cannot be held (no playback control): Stop, replay later
        return;
      }
      const live = c && !["reading", "talk-idle"].includes(c.state);
      if (msg?.role === "narrator") {
        if (!msg.text || msg.loading || msg.quiet) return;
        replayer.current?.stop();
        if (live) void controller.current?.stop();
        narration.current?.toggle(String(id), speechParts(msg.text));
        return;
      }
      narration.current?.stop(); // pauses the recap: one voice at a time
      const item = replayStore.current.get(id);
      if (!item) return;
      if (live && replayer.current?.playing() !== id) void controller.current?.stop(); // live answer stops cleanly first
      replayIdRef.current = id;
      replayer.current?.toggle(id);
    }, []),
    setSpeaker: useCallback((id: string) => { void controller.current?.setSpeaker(id); }, []),
    dismissToast: useCallback(() => setToast(null), []),
  };
}
