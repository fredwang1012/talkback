import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRealtimeKey, fetchRetrieve, lipSync, lipSyncStream, speak, streamAnswer, type Health } from "../api";
import { MicCapture } from "../audio/capture";
import { RingPlayer } from "../audio/playback";
import { spend } from "../spend";
import type { LogEntry } from "../ui/DebugDrawer";
import { addQuestion, appendSentence, finishAnswer, loadChat, saveChat, setPendingText, type ChatMessage } from "./chatLog";
import { detectLanguage, type Detected } from "./language";
import { FaceSync } from "./faceSync";
import { createPlayingGate, type TimingEvent } from "./speechGate";
import { chooseLipSyncMode, openAvatarStream } from "./avatarStream";
import { createSentenceSpeaker } from "./sentencePlayer";
import { TalkClient, type MicLike, type PlayerLike } from "./TalkClient";
import { TalkController, type Latency, type ReaderControl, type TalkState } from "./TalkController";

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
  logs: LogEntry[];
  pttDown: () => void;
  pttUp: () => void;
  setSpeaker: (id: string) => void;
  messages: ChatMessage[]; // the Talkback conversation for this book
  ask: (text: string) => void; // a typed question
  stop: () => void; // stop speaking
  clearMessages: () => void;
  dismissToast: () => void;
};

type Args = {
  bookId: string;
  enabled: boolean; // only when the server has a Boson key
  reader: ReaderControl;
  speakerId: string;
  voiceFor: (speakerId: string) => string;
  autoResume: boolean;
  health: Health | null;
  canLipSync?: (voice: string) => boolean; // the chosen face is the one bound to this voice (the server lip-syncs the voice's own face)
  spoilers?: boolean; // full-book mode: the reader allows spoilers (novels only)
  talkback?: boolean; // the dedicated conversation view is open: a mic tap never toggles reading
  lipSync?: boolean; // render each answer sentence as a lip-synced clip of the voice's face (~10 s per sentence)
};

/** Owns the Talk session for the reader screen. The real mic, speaker and socket are created once per book. */
export function useTalk({ bookId, enabled, reader, speakerId, voiceFor, autoResume, health, canLipSync = () => false, lipSync: wantLipSync = false, spoilers = false, talkback = false }: Args): TalkUi {
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
  useEffect(() => { bookIdRef.current = bookId; msgs.current = loadChat(bookId); setMessagesState(msgs.current); }, [bookId]);
  const controller = useRef<TalkController | null>(null);
  const viaGemini = answersViaGemini(health);
  const latest = useRef({ reader, voiceFor, speakerId, canLipSync, wantLipSync, spoilers });
  latest.current = { reader, voiceFor, speakerId, canLipSync, wantLipSync, spoilers };

  useEffect(() => {
    if (!enabled) return;
    const client = new TalkClient({
      tokenProvider: fetchRealtimeKey,
      createPlayer: (onSpeaking) => new RingPlayer(onSpeaking) as unknown as PlayerLike,
      createMic: (onChunk, onLevel) => new MicCapture(onChunk, onLevel) as unknown as MicLike,
      onLevel: setLevel,
      onLog: (dir, type, raw) => {
        const r = raw as { audio?: string; delta?: string };
        if (dir === "send" && type === "input_audio_buffer.append" && r.audio) spend.addAudioIn(r.audio.length);
        if (dir === "recv" && type === "response.output_audio.delta" && r.delta) spend.addAudioOut(r.delta.length);
        setLogs((l) => [...l.slice(-299), { id: ++logSeq.current, dir, type, at: Date.now(), raw }]);
      },
    });
    let answerVoice = "";
    let talk: TalkController | null = null;
    const gate = createPlayingGate((on) => talk?.notePlaying(on));
    const logTiming = (e: TimingEvent) => {
      console.info("[talk] timing", e);
      setLogs((l) => [...l.slice(-299), { id: ++logSeq.current, dir: "info", type: `timing:${e.stage}`, at: Date.now(), raw: e }]);
    };
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
      retrieve: (query, para, speaker) => fetchRetrieve(bookId, query, para, speaker, latest.current.spoilers),
      ...(viaGemini ? {
        // the voice is fixed when the answer starts, so a mid-answer speaker switch cannot change it
        answer: (query, para, speaker, onSentence, signal) => {
          answerVoice = latest.current.voiceFor(speaker);
          return streamAnswer(bookId, query, para, speaker, (t) => { setMessages((m) => appendSentence(m, t)); onSentence(t); }, signal, latest.current.spoilers);
        },
        speaker: createSentenceSpeaker(async (text) => {
          const voice = answerVoice;
          const r = await speak(text, voice);
          if (latest.current.wantLipSync && latest.current.canLipSync(voice)) {
            // the voice does not wait for this: the video joins when its first fragment is in (see faceSync)
            const openVideo = async () => {
              if (chooseLipSyncMode() === "stream") {
                const c = await openAvatarStream({
                  post: (signal) => lipSyncStream(r.audio_url, voice, signal),
                  MediaSourceCtor: MediaSource, createObjectURL: (o) => URL.createObjectURL(o), revokeObjectURL: (u) => URL.revokeObjectURL(u),
                });
                return { url: c.url, release: c.release };
              }
              return { url: await lipSync(r.audio_url, voice), release: () => {} };
            };
            return { audio_url: r.audio_url, openVideo };
          }
          return { audio_url: r.audio_url };
        }, undefined, { sync: faceSync.current as FaceSync, onPlaying: gate.playing, onTiming: logTiming }),
      } : {}),
      voiceFor: (id) => latest.current.voiceFor(id),
      onState: (st) => { setState(st); if (["reading", "talk-idle", "resuming"].includes(st)) setMessages(finishAnswer); },
      onToast: setToast,
      onCaption: (text) => { setCaption(text); if (!viaGemini && text) setMessages((m) => setPendingText(m, text)); },
      onSpeaking: setSpeaking,
      onTiming: logTiming,
      onTranscript: (text) => {
        setQuestion(text); setLanguage(detectLanguage(text));
        setMessages((m) => addQuestion(m, text, latest.current.speakerId, via.current));
        via.current = "voice";
      },
      onLatency: (l) => { setLatency(l); console.info("[talk] latency", l); },
    });
    controller.current = talk;
    const sync = faceSync.current;
    return () => { gate.reset(); sync?.leave(); talk?.dispose(); client.close(); controller.current = null; setState("reading"); setSpeaking(false); };
  }, [bookId, enabled, viaGemini]);

  useEffect(() => { controller.current?.setAutoResume(autoResume); }, [autoResume, enabled]);
  useEffect(() => { controller.current?.setTalkback(talkback); }, [talkback, enabled]);
  useEffect(() => { void controller.current?.setSpeaker(speakerId); }, [speakerId, enabled]);
  // the same speaker with a different resolved voice must reach an open realtime session too
  const voiceKey = voiceFor(speakerId);
  useEffect(() => { void controller.current?.refreshVoice(); }, [voiceKey, enabled]);

  return {
    state, caption, toast, level, latency, speaking, question, language, faceSync: faceSync.current, clipShown, logs,
    pttDown: useCallback(() => { void controller.current?.pttDown(); }, []),
    pttUp: useCallback(() => { void controller.current?.pttUp(); }, []),
    messages,
    ask: useCallback((text: string) => {
      via.current = "typed";
      void controller.current?.ask(text); // onTranscript fires synchronously inside ask, before its first await
      via.current = "voice"; // an ignored (empty) ask must not label the next spoken question as typed
    }, []),
    stop: useCallback(() => { void controller.current?.stop(); }, []),
    clearMessages: useCallback(() => { void controller.current?.stop(); setMessages(() => []); }, [setMessages]),
    setSpeaker: useCallback((id: string) => { void controller.current?.setSpeaker(id); }, []),
    dismissToast: useCallback(() => setToast(null), []),
  };
}
