import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRealtimeKey, fetchRetrieve, lipSync, speak, streamAnswer, type Health } from "../api";
import { MicCapture } from "../audio/capture";
import { RingPlayer } from "../audio/playback";
import { spend } from "../spend";
import type { LogEntry } from "../ui/DebugDrawer";
import { detectLanguage, type Detected } from "./language";
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
  clip: string | null; // lip-synced video of the sentence being spoken, when lip-sync is on
  logs: LogEntry[];
  pttDown: () => void;
  pttUp: () => void;
  setSpeaker: (id: string) => void;
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
  lipSync?: boolean; // render each answer sentence as a lip-synced clip of the voice's face (~10 s per sentence)
};

/** Owns the Talk session for the reader screen. The real mic, speaker and socket are created once per book. */
export function useTalk({ bookId, enabled, reader, speakerId, voiceFor, autoResume, health, canLipSync = () => false, lipSync: wantLipSync = false, spoilers = false }: Args): TalkUi {
  const [state, setState] = useState<TalkState>("reading");
  const [caption, setCaption] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const [latency, setLatency] = useState<Latency | null>(null);
  const [speaking, setSpeaking] = useState(false);
  const [question, setQuestion] = useState("");
  const [language, setLanguage] = useState<Detected | null>(null);
  const [clip, setClip] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logSeq = useRef(0);
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
    const talk = new TalkController({
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
          return streamAnswer(bookId, query, para, speaker, onSentence, signal, latest.current.spoilers);
        },
        speaker: createSentenceSpeaker(async (text) => {
          const voice = answerVoice;
          const r = await speak(text, voice);
          if (latest.current.wantLipSync && latest.current.canLipSync(voice)) {
            try { return { ...r, video_url: await lipSync(r.audio_url, voice) }; }
            catch { /* render failed: the speaking loop stands in */ }
          }
          return r;
        }, undefined, setClip),
      } : {}),
      voiceFor: (id) => latest.current.voiceFor(id),
      onState: setState,
      onToast: setToast,
      onCaption: (text) => setCaption(text),
      onSpeaking: setSpeaking,
      onTranscript: (text) => { setQuestion(text); setLanguage(detectLanguage(text)); },
      onLatency: (l) => { setLatency(l); console.info("[talk] latency", l); },
    });
    controller.current = talk;
    return () => { talk.dispose(); client.close(); controller.current = null; setState("reading"); setSpeaking(false); };
  }, [bookId, enabled, viaGemini]);

  useEffect(() => { controller.current?.setAutoResume(autoResume); }, [autoResume, enabled]);
  useEffect(() => { void controller.current?.setSpeaker(speakerId); }, [speakerId, enabled]);
  // the same speaker with a different resolved voice must reach an open realtime session too
  const voiceKey = voiceFor(speakerId);
  useEffect(() => { void controller.current?.refreshVoice(); }, [voiceKey, enabled]);

  return {
    state, caption, toast, level, latency, speaking, question, language, clip, logs,
    pttDown: useCallback(() => { void controller.current?.pttDown(); }, []),
    pttUp: useCallback(() => { void controller.current?.pttUp(); }, []),
    setSpeaker: useCallback((id: string) => { void controller.current?.setSpeaker(id); }, []),
    dismissToast: useCallback(() => setToast(null), []),
  };
}
