import { AUDIO_RATE } from "../audio/constants";
import { describeClose, functionCallOutput, historyItem, inputAudioAppend, truncateItem, userInput, type ClientEvent, type ServerEvent } from "../realtime/events";
import type { SessionEvent, TalkSession, ToolCall, ToolResult } from "./TalkController";
import type { AnswerMode } from "./answerMode";
import { splitMarker } from "./emotionMarker";
import type { SentenceSpeaker } from "./sentencePlayer";
import { takeSentences } from "./sentences";
import { TOOL_DEFS } from "./tools";
import { sourcesFromSearchOutput } from "./webSearch";

/** The Realtime model (A6): the 1.5 preview by default (live-checked with our exact session config). `?rt=higgs-realtime`
 * on the page URL picks another for an A/B. If the preview is refused, the page drops to the stable model for good. */
export const REALTIME_MODEL = "higgs-realtime-1.5-preview";
export const REALTIME_MODEL_STABLE = "higgs-realtime";
let fellBackToStable = false;
export function resetRealtimeFallback(): void { fellBackToStable = false; }
export function realtimeModel(search: string = typeof location === "undefined" ? "" : location.search): string {
  if (fellBackToStable) return REALTIME_MODEL_STABLE;
  const rt = new URLSearchParams(search).get("rt");
  return rt && /^higgs-realtime[\w.-]*$/.test(rt) ? rt : REALTIME_MODEL;
}
export const realtimeUrl = (model: string): string => `wss://api.boson.ai/v1/realtime?model=${encodeURIComponent(model)}`;
const OPEN = 1; // WebSocket.OPEN, spelled out so tests need no global WebSocket
/**
 * Hands-free pre-roll (#114): how much mic audio from before the voice gate opened is sent first. The gate reports
 * speech only after `minSpeechMs` of continuous level over its threshold (400 ms while the character talks, and a dip
 * restarts the count), and a soft first syllable is under the threshold anyway, so the first second or so of the
 * question was gone before the turn began. Short enough that little of the character's echo comes along with it.
 */
export const PREROLL_MS = 1500;
/** A voice-started turn while the character is speaking (a barge-in) keeps only about the over-speech gate's window
 * (HANDS_FREE_OVER_SPEECH.minSpeechMs): before that the mic hears the character, and any echo leak would be transcribed. */
export const BARGE_PREROLL_MS = 500;
/** A turn started by the mic button while hands-free is on keeps only this much (the reader presses, then speaks). */
export const PRESS_PREROLL_MS = 300;

/** Server-mode hands-free (#135): mic audio held while the socket is not open (or a cancel is out), newest kept. */
export const STREAM_HOLD_MS = 3000;
/** Caption reveal tick (#135): how often the played position is read while an answer plays. */
export const CAPTION_TICK_MS = 100;
/** Busy server (upstream_error before anything was heard): how many times the answer is asked again, and after how long. */
export const BUSY_RETRIES = 1;
export const BUSY_RETRY_MS = 800;
/** Text mode (B1): how many answer sentences are synthesised ahead of the one being spoken. */
export const TEXT_PREFETCH_DEPTH = 3;
/** Boson's turn detection types, best first (#135); the next one is tried when the server refuses one. */
export const VAD_TYPES = ["semantic_vad", "server_vad"] as const;
export type VadType = (typeof VAD_TYPES)[number];

/**
 * Hands-free turn detection (#135). `create_response: false`: the server detects, commits and transcribes the turn but
 * never answers by itself (verified live; the migration guide still says it has no effect), so every answer is our
 * response.create with the retrieval instructions. `interrupt_response: false`: we decide barge-in (echo guard), and
 * live a speech_started during an active response did not cancel it. threshold 0.6 (default 0.85 misses soft speech;
 * NOTES suggested 0.6-0.7), 500 ms of silence (semantic's default 100 ms split one live question into fragments).
 */
export function turnDetection(type: VadType): Record<string, unknown> {
  return { type, create_response: false, interrupt_response: false, threshold: 0.6, silence_duration_ms: 500 };
}

/** One spoken fragment of the answer and when its audio plays, in ms into the reply (from `.length` events). */
export type CaptionSegment = { text: string; start: number; end: number };

/**
 * The caption heard by `ms` into the reply (#135): every fragment whose audio has started, the current one word by
 * word in proportion to its duration (Boson's `response.output_audio_transcript.length` gives a duration per
 * fragment, live about a sentence, not per word).
 */
export function captionAt(segs: CaptionSegment[], ms: number): string {
  let out = "";
  for (const g of segs) {
    if (ms <= g.start) break;
    if (ms >= g.end) { out += g.text; continue; }
    const parts = g.text.match(/\s*\S+/g) ?? [];
    const n = Math.min(parts.length, Math.ceil(((ms - g.start) / Math.max(1, g.end - g.start)) * parts.length));
    out += parts.slice(0, n).join("");
    break;
  }
  return out.trim();
}

/** Duration of a base64 PCM16 mono chunk at AUDIO_RATE. */
function chunkMs(b64: string): number {
  const pad = b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0;
  return ((b64.length * 3) / 4 - pad) / 2 / AUDIO_RATE * 1000;
}

/** Short and behavioural: the role comes from per-response instructions (brief 3.4 prompting lesson). */
export const SESSION_INSTRUCTIONS =
  "You are a spoken reading companion. Follow the instructions given with each response. Speak naturally; no markdown. " +
  "When the reader asks you to act on the book (move, read, repeat, find a topic, pause, speed, recap, bookmark), call the tool, then confirm in one short sentence.";

export interface SocketLike {
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: ((ev: { code: number; reason: string }) => void) | null;
  onerror: (() => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}
export interface PlayerLike {
  resume(): Promise<void>;
  enqueueBase64(b64: string): void;
  /** Stop playback now; resolves with milliseconds actually handed to the audio output (audio_end_ms). */
  flush(): Promise<number>;
  /** Milliseconds of this reply played so far (since reset), without stopping it. Optional: without it captions show
   * the text as it arrives instead of in step with the voice (#135). */
  playedMs?(): number;
  /** Hold playback where it is (queued audio is kept) and continue it. Optional. */
  pausePlayback?(): Promise<void>;
  resumePlayback?(): Promise<void>;
  /** False while playback cannot be held (a live face, #140). Absent = it can. */
  readonly canPause?: boolean;
  /** The reply's audio is all in (response.done). Optional: a live face (#140) ends its audio segment here. */
  endTurn?(): void;
  reset(): void;
  close(): Promise<void>;
}
export interface MicLike {
  start(): Promise<void>;
  stop(): Promise<void>;
}

export interface TalkClientDeps {
  tokenProvider: () => Promise<string>;
  createSocket?: (url: string, protocols: string[]) => SocketLike;
  /** Receives (speaking) changes so the client can tell when an answer has finished playing. */
  createPlayer: (onSpeaking: (speaking: boolean) => void) => PlayerLike;
  /** Receives base64 PCM16 chunks (~100 ms) and a level for the meter. */
  createMic: (onChunk: (b64: string) => void, onLevel?: (rms: number) => void) => MicLike;
  onLevel?: (rms: number) => void;
  /** Every base64 PCM chunk of the reply; `first` is true for the first chunk of a response (reply replay keeps them). */
  onAudioChunk?: (b64: string, first: boolean) => void;
  /** The finished turns of the chat with the current speaker. A fresh session starts empty, so they are put back as text items. */
  historyProvider?: () => { role: "user" | "character"; text: string }[];
  onLog?: (dir: "send" | "recv" | "info", type: string, raw: unknown) => void;
  cancelWaitMs?: number;
  voiceChangeWaitMs?: number;
  /** Waits between retries when Boson refuses a connection with close code 1013 (concurrency limit). */
  backoffMs?: number[];
  /** The tools declared to the session (#72): the client function tools, plus Boson's server-side web_search when the
   * book allows it (webSearch.sessionTools); `[]` declares none. `setTools` changes them for the next turn. */
  tools?: readonly unknown[];
  /**
   * B1: "text" (default): the model writes the answer (output_modalities ["text"]) and `createSpeaker`'s sentence
   * player voices it sentence by sentence with Higgs TTS (emotion tag, the speaker's voice, lip-sync), like a Gemini
   * answer. "audio": Boson speaks the answer itself through `createPlayer` (the old path). Without `createSpeaker`
   * the client is in audio mode whatever this says.
   */
  answerMode?: AnswerMode;
  /** The sentence player for text mode; `onPlaying` must report when a sentence's sound starts and stops. */
  createSpeaker?: (onPlaying: (on: boolean) => void) => SentenceSpeaker;
  /** Text mode: each clean answer sentence with its feeling marker (null = neutral), `first` for the first of an answer. */
  onSentence?: (text: string, emotion: string | null, first: boolean) => void;
}

/** Close code 1013 before the session opened: too many concurrent connections. Safe to retry after a pause. */
export class ConnectionLimitError extends Error {}
/** The socket was dropped before it opened (a turn-mode change): openWithBackoff opens the next one at once. */
class SupersededError extends Error {}

export type SessionOptions = { model?: string; turnDetection?: Record<string, unknown> | null; language?: string | null; textOutput?: boolean };

export function sessionConfig(voice: string, tools: readonly unknown[] = TOOL_DEFS, opts: SessionOptions = {}): ClientEvent {
  return {
    type: "session.update",
    session: {
      type: "realtime",
      model: opts.model ?? realtimeModel(),
      instructions: SESSION_INSTRUCTIONS,
      output_modalities: opts.textOutput ? ["text"] : ["audio"], // B1: text mode, voiced by Higgs TTS in the browser
      tools, // client function tools (the model asks, the browser acts: tools.ts) and, when allowed, the server-side web_search
      tool_choice: "auto",
      audio: {
        input: {
          format: { type: "audio/pcm", rate: AUDIO_RATE },
          turn_detection: opts.turnDetection ?? null, // push-to-talk: we commit each turn ourselves; hands-free: turnDetection()
          transcription: transcription(opts.language ?? null),
          // near_field for push-to-talk only: live (#135) it made the server VAD start a turn late and lose the first
          // words; the browser's own noise suppression and echo cancellation still apply in the capture
          ...(opts.turnDetection ? {} : { noise_reduction: { type: "near_field" } }),
        },
        // text mode keeps the voice (so a switch back to audio needs no reconnect) but asks for no audio format
        output: opts.textOutput ? { voice } : { format: { type: "audio/pcm", rate: AUDIO_RATE }, voice },
      },
      temperature: 0.3,
    },
  };
}

/** The transcription block; `language` (ISO 639-1) is Boson's hint, set only for clearly non-English speech (#135). */
function transcription(language: string | null): Record<string, unknown> {
  return language ? { model: "higgs-stt-3.1", language } : { model: "higgs-stt-3.1" };
}

export class TalkClient implements TalkSession {
  private d: TalkClientDeps & { cancelWaitMs: number; voiceChangeWaitMs: number; backoffMs: number[] };
  private ws: SocketLike | null = null;
  private opening: Promise<void> | null = null; // the idempotency guard (React StrictMode opens two sockets otherwise)
  private voice = "default";
  private player: PlayerLike;
  private mic: MicLike;
  private tools: readonly unknown[];
  // B1 text mode: the answer arrives as text, is split into sentences and spoken by the sentence player
  private readonly speaker: SentenceSpeaker | null;
  private textBuf = ""; // text received, not yet a whole sentence
  private textTotal = ""; // all text of the current content part (output_text.done repeats it: a lost delta is made up)
  private textQueue: string[] = []; // clean sentences waiting to be spoken
  private textPumping = false;
  private textEpoch = 0; // bumped by stopText(): a pump loop from before exits
  private textCurrent: string | null = null; // the sentence being spoken
  private doneHeld: "completed" | "cancelled" | "failed" | null = null; // text mode: response-done waiting for the first sound
  private spoken: string[] = []; // sentences whose sound has started (the caption, and "heard" on a cut)
  private sentenceCount = 0;
  // B2 server-side web_search: the server runs the call, adds its result and starts the answer itself
  private serverToolPending = false;
  private searchCalls = new Set<string>(); // call ids of web_search calls in this turn (their outputs carry the links)
  private searching = false;
  private lastCallNames = new Map<string, string>(); // call id -> tool name, for the continuation's instructions
  private micOn = false;
  private monitor = false; // hands-free (#10): the mic stays open between turns; chunks are kept briefly, not sent
  private recent: { b64: string; ms: number }[] = []; // rolling pre-roll while monitoring (#114): sent first when a turn starts
  private recentMs = 0;
  private pendingAudio: string[] = [];
  private commitWhenOpen = false;
  private interrupting = false; // a cancel is out and not yet acknowledged (#100): new audio and the commit wait
  private dropOutput = false; // the cut-off answer's late output is discarded until the next response.created (#100)
  private listeners = new Set<(e: SessionEvent) => void>();
  private waiters: ((e: ServerEvent) => void)[] = [];

  private responseActive = false;
  private responseDone = false;
  private audioStarted = false;
  private assistantItemId: string | null = null;
  private caption = "";
  private lastInstructions = "";
  private lastQuestion = ""; // repeated to the model in a tool continuation (it does not see the earlier input)
  private pendingCalls: ToolCall[] = []; // tool calls announced by function_call_arguments.done, until response.done

  // #135 server turn detection: wanted (hands-free), and the VAD type the open (or next) session uses
  private wantServer = false;
  private modeChangeDue = false; // #141: the session's turn mode is wrong, but an answer is playing: switch when it ends
  private sessionRefused = false; // #141: the open session's turn detection was refused; it is replaced once idle
  private toolsOut = false; // tool calls handed to the reader, returnTools not called yet
  private vadIndex = 0;
  private sessionServer = false; // the open session was configured with server turn detection
  private sessionAcked = false; // its session.created was seen
  private sessionModel = REALTIME_MODEL_STABLE; // the Realtime model the open session asked for
  private expectResponse = 0; // response.create calls not yet matched by a response.created
  private language: string | null = null;
  // the character must never hear itself: while held (it speaks, and a short tail after) mic audio is kept, not sent
  private micHeld = false;
  // answers bound to their response id: a cancelled answer's late output is never played or captioned
  private currentResponseId: string | null = null;
  private cancelledIds = new Set<string>();
  private cancelUncreated = 0; // cancelled after response.create but before its response.created arrived
  // 1.5 preview capacity (Boson feedback #12): about 2 generations at once per key; the next fails with upstream_error
  private lastCreate: ClientEvent | null = null; // the last response.create, sent again once when the server was busy
  private busyRetries = 0;
  private busyFailed = false; // this response failed with upstream_error before anything was heard
  // #135 voice-timed captions
  private segs: CaptionSegment[] = [];
  private segMs = 0;
  private finalCaption: string | null = null;
  private shownCaption = "";
  private captionTimer: ReturnType<typeof setInterval> | null = null;

  constructor(deps: TalkClientDeps) {
    this.d = { cancelWaitMs: 2000, voiceChangeWaitMs: 3000, backoffMs: [1000, 2000, 4000], ...deps };
    this.tools = deps.tools ?? TOOL_DEFS;
    this.player = deps.createPlayer((speaking) => this.onSpeaking(speaking));
    this.mic = deps.createMic((b64) => this.onMicChunk(b64), deps.onLevel);
    this.speaker = deps.answerMode !== "audio" && deps.createSpeaker ? deps.createSpeaker((on) => this.onSentencePlaying(on)) : null;
  }

  /** B1: the answer is written by the model and voiced by Higgs TTS here (else Boson speaks it as PCM). */
  get textMode(): boolean { return this.speaker !== null; }

  /** The tools for the next turn (B2: web_search comes and goes with the spoilers switch). Sent to an open session at once. */
  setTools(tools: readonly unknown[]): void {
    if (JSON.stringify(tools) === JSON.stringify(this.tools)) return;
    this.tools = tools;
    this.send({ type: "session.update", session: { tools } });
  }

  get isOpen(): boolean { return this.ws?.readyState === OPEN; }

  on(fn: (e: SessionEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(e: SessionEvent): void { this.listeners.forEach((fn) => fn(e)); }

  private send(evt: ClientEvent): void {
    if (this.ws?.readyState !== OPEN) return;
    this.ws.send(JSON.stringify(evt));
    this.d.onLog?.("send", evt.type, evt);
  }

  // ---- connection --------------------------------------------------------------------------------------------

  ensureOpen(voice: string): Promise<void> {
    this.applyModeChange(false); // a deferred mode change not made yet (#141): the session opened next has the right config
    if (this.isOpen) return voice === this.voice ? Promise.resolve() : this.setVoice(voice);
    if (this.opening) return this.opening;
    this.voice = voice;
    this.opening = this.openWithBackoff(voice).finally(() => { this.opening = null; });
    return this.opening;
  }

  private async openWithBackoff(voice: string): Promise<void> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.open(voice);
      } catch (e) {
        if (e instanceof SupersededError) { attempt--; continue; } // dropped while connecting (a mode change): open the right one
        const wait = this.d.backoffMs[attempt];
        if (!(e instanceof ConnectionLimitError) || wait === undefined) throw e;
        this.d.onLog?.("info", "backoff", `Boson is at its connection limit; retrying in ${wait} ms`);
        await new Promise((r) => setTimeout(r, wait));
      }
    }
  }

  private async open(voice: string): Promise<void> {
    await this.player.resume(); // must run inside the user gesture or the AudioContext stays suspended
    const key = await this.d.tokenProvider(); // a fresh ephemeral key for every connection, never cached
    await new Promise<void>((resolve, reject) => {
      let opened = false;
      const make = this.d.createSocket ?? ((url, protocols) => new WebSocket(url, protocols) as unknown as SocketLike);
      const model = realtimeModel();
      const ws = make(realtimeUrl(model), ["realtime", `bai-client-secret.${key}`]);
      this.ws = ws;
      ws.onopen = () => {
        opened = true;
        this.sessionServer = this.wantServer;
        this.sessionAcked = false;
        const turn = this.sessionServer ? turnDetection(VAD_TYPES[this.vadIndex]) : null;
        // the first session.update starts the session; no need to wait for session.created
        this.sessionModel = model;
        this.send(sessionConfig(voice, this.tools, { model, turnDetection: turn, language: this.language, textOutput: this.textMode }));
        this.seedHistory();
        this.flushPending();
        resolve();
      };
      ws.onmessage = (ev) => this.onMessage(ev.data);
      ws.onerror = () => this.d.onLog?.("info", "socket-error", "WebSocket error; the close code has the detail.");
      ws.onclose = (ev) => {
        const wasThis = this.ws === ws;
        if (wasThis) this.ws = null;
        const info = describeClose(ev.code, ev.reason);
        this.d.onLog?.("info", `close:${ev.code}`, info);
        if (!wasThis) { if (!opened) reject(new SupersededError()); return; } // never leave `opening` pending for ever
        this.resetResponse();
        this.dropOutput = false;
        this.interrupting = false;
        if (opened && !this.sessionAcked && this.sessionModel !== REALTIME_MODEL_STABLE && ![1000, 1013, 3000, 4429].includes(ev.code)) {
          fellBackToStable = true; // the preview closed the session before it started: the next one uses the stable model
          this.d.onLog?.("info", "realtime-model", `${this.sessionModel} closed the session (${ev.code}); using ${REALTIME_MODEL_STABLE} from now on`);
        }
        if (ev.code === 1013 && !opened) { reject(new ConnectionLimitError(info.message)); return; }
        if (ev.code === 3000 || ev.code === 4429) this.emit({ type: "error", message: info.message });
        else if (ev.code === 1013) this.emit({ type: "error", message: "Boson is at its connection limit. Try again in a moment." });
        else this.emit({ type: "closed", message: info.message }); // idle timeout / max duration: reconnect on next press
        reject(new Error(info.message)); // no-op if already opened
      };
    });
  }

  /** A new session knows nothing of the earlier chat (reconnect, reload, idle timeout): give it the last few turns as text. */
  private seedHistory(): void {
    let turns: { role: "user" | "character"; text: string }[] = [];
    try { turns = this.d.historyProvider?.() ?? []; } catch { /* no history is fine */ }
    turns.forEach((t, i) => { if (t.text.trim()) this.send(historyItem(t.role, t.text, `seed-${i}`)); });
  }

  /** Forget the conversation the server holds (Clear chat): drop the socket; the next press opens a fresh, empty session. */
  resetConversation(): void {
    const ws = this.ws;
    this.ws = null;
    this.resetResponse();
    this.dropOutput = false;
    this.interrupting = false;
    ws?.close(1000, "chat cleared");
  }

  /** Server turns (#135): Boson's turn detection is wanted for the next session (hands-free) or not (push-to-talk).
   * turn_detection cannot change mid-session (live: 400 "turn_detection cannot be changed after session start"), so an
   * open session in the other mode is dropped; the next ensureOpen opens one with the right config. */
  setServerTurns(on: boolean): void {
    if (on === this.wantServer) return;
    this.wantServer = on;
    this.vadIndex = 0;
    this.micHeld = false;
    if (!on) this.pendingAudio = []; // held for a server session that will not come: never the start of the next press
    if (!this.ws || (this.sessionServer === on && !this.sessionRefused)) { this.modeChangeDue = false; return; }
    if (this.turnBusy) this.modeChangeDue = true; // #141: never drop the socket under an answer; switch once it drained
    else this.dropSocket("turn mode change");
  }

  /** A turn is in progress (audio going out, an answer generated, played or waiting for its tools): a socket dropped
   * now would never report the answer's end, and the controller would stay "answering" (#141). */
  private get turnBusy(): boolean {
    return this.micOn || this.responseActive || (this.responseDone && this.audioStarted) || this.expectResponse > 0 || this.toolsOut
      || this.textPumping || this.textQueue.length > 0 || this.serverToolPending;
  }

  /** The turn is idle again: make the deferred mode change (drop the session, open one with the right config). */
  private applyModeChange(reopen = true): void {
    if (!this.modeChangeDue || this.turnBusy) return;
    this.modeChangeDue = false;
    if (!this.ws || (this.sessionServer === this.wantServer && !this.sessionRefused)) return;
    this.dropSocket(this.sessionRefused ? "turn mode fallback" : "turn mode change");
    if (reopen) void this.ensureOpen(this.voice).catch((e: unknown) => this.emit({ type: "error", message: e instanceof Error ? e.message : String(e) }));
  }

  /** Boson's turn detection is in use for hands-free (wanted, and not refused). */
  get serverTurnsActive(): boolean { return this.wantServer; }

  /** Remove an item from the server's conversation (#135: a turn that was only the character's echo). */
  deleteItem(itemId: string): void { this.send({ type: "conversation.item.delete", item_id: itemId }); }

  /** Empty the server's input audio buffer: a server-detected turn is over (live, the server never clears it itself,
   * and transcribes every turn with all the audio since the last clear). */
  clearInput(): void { this.send({ type: "input_audio_buffer.clear" }); }

  /** The transcription language hint (ISO 639-1, or null for none) (#135); sent at once when a session is open. */
  setLanguageHint(code: string | null): void {
    if (code === this.language) return;
    this.language = code;
    this.send({ type: "session.update", session: { audio: { input: { transcription: { model: "higgs-stt-3.1", language: code } } } } });
  }

  private dropSocket(reason: string): void {
    const ws = this.ws;
    this.ws = null;
    this.modeChangeDue = false;
    this.sessionRefused = false;
    this.pendingAudio = []; // held for the dropped session (#141)
    this.commitWhenOpen = false;
    this.resetResponse();
    this.dropOutput = false;
    this.interrupting = false;
    this.expectResponse = 0;
    ws?.close(1000, reason);
  }

  /** The server refused this turn detection (#135): try the next type, or go back to manual turns and say so. */
  private refuseServerMode(why: string, toManual = false): void {
    if (!this.sessionServer || this.sessionRefused) return;
    this.d.onLog?.("info", "turn-mode", why);
    if (!toManual && this.vadIndex + 1 < VAD_TYPES.length) this.vadIndex++;
    else { this.wantServer = false; this.emit({ type: "turn-mode", server: false }); }
    if (this.turnBusy) { this.sessionRefused = true; this.modeChangeDue = true; return; } // #141: once the answer drained
    this.sessionServer = false;
    this.dropSocket("turn mode fallback");
    void this.ensureOpen(this.voice).catch((e: unknown) => this.emit({ type: "error", message: e instanceof Error ? e.message : String(e) }));
  }

  /** The 1.5 preview refused the session (A6): remember it for the page and reopen on the stable model, keeping any audio held for the first turn. */
  private fallBackToStable(why: string): void {
    fellBackToStable = true;
    this.d.onLog?.("info", "realtime-model", `${this.sessionModel} refused (${why}); using ${REALTIME_MODEL_STABLE} from now on`);
    const held = this.pendingAudio, commit = this.commitWhenOpen;
    this.dropSocket("model fallback");
    this.pendingAudio = held;
    this.commitWhenOpen = commit;
    void this.ensureOpen(this.voice).catch((e: unknown) => this.emit({ type: "error", message: e instanceof Error ? e.message : String(e) }));
  }

  close(): void {
    this.stopCaptionTimer();
    this.stopText();
    this.ws?.close(1000, "client disconnect");
    this.ws = null;
    this.monitor = false;
    void this.mic.stop();
    void this.player.close();
  }

  // ---- microphone / turns -------------------------------------------------------------------------------------

  /**
   * Start streaming the turn. `voiceStarted` (hands-free, the voice gate opened it): the rolling pre-roll goes out
   * first, so the words spoken before the gate decided it was speech are kept (#114). A press keeps only ~300 ms.
   * Without hands-free nothing was captured before, so push-to-talk starts from the press as always.
   */
  async startMic(voiceStarted = false, overSpeech = false): Promise<void> {
    this.micOn = true;
    this.commitWhenOpen = false;
    const keepMs = voiceStarted ? (overSpeech ? BARGE_PREROLL_MS : PREROLL_MS) : PRESS_PREROLL_MS;
    const pre = this.recent.splice(0);
    this.recentMs = 0;
    let from = pre.length;
    for (let ms = 0; from > 0 && ms < keepMs; ) ms += pre[--from].ms;
    for (const c of pre.slice(from)) this.onMicChunk(c.b64); // in order, ahead of the live audio (held with it if not ready)
    await this.mic.start();
  }

  /** Hands-free (#10): keep the microphone open between turns so speech can be detected; nothing is sent until startMic. */
  async setMonitor(on: boolean): Promise<void> {
    this.monitor = on;
    this.recent = [];
    this.recentMs = 0;
    if (on) await this.mic.start();
    else if (!this.micOn) await this.mic.stop();
  }

  /**
   * Server turns (hands-free): hold the mic so the server never hears our own playback (the character speaking, and a
   * short tail after). Held audio is kept briefly; releasing with `preRollMs` (a barge-in the local gate heard) sends
   * the last `preRollMs` of it first, so the reader's first words are kept; without, the held audio (echo) is dropped.
   */
  setMicHold(hold: boolean, preRollMs = 0): void {
    if (hold === this.micHeld) return;
    this.micHeld = hold;
    const pre = this.recent.splice(0);
    this.recentMs = 0;
    if (hold || preRollMs <= 0) return;
    let from = pre.length;
    for (let ms = 0; from > 0 && ms < preRollMs; ) ms += pre[--from].ms;
    for (const c of pre.slice(from)) this.onMicChunk(c.b64);
  }

  /** Audio and commits may go out: the socket is open and no cancel is waiting for its acknowledgement. */
  private get ready(): boolean { return this.isOpen && !this.interrupting; }

  private onMicChunk(b64: string): void {
    if (this.monitor && this.wantServer) { // #135: the server listens all the time and decides the turns
      if (this.micHeld) { // the character is speaking: keep just enough for a barge-in's pre-roll, send nothing
        const ms = chunkMs(b64);
        this.recent.push({ b64, ms });
        this.recentMs += ms;
        while (this.recent.length > 1 && this.recentMs - this.recent[0].ms >= PREROLL_MS) this.recentMs -= this.recent.shift()!.ms;
        return;
      }
      if (this.ready) { this.flushPending(); this.send(inputAudioAppend(b64)); return; }
      this.pendingAudio.push(b64);
      let ms = 0;
      for (const c of this.pendingAudio) ms += chunkMs(c);
      while (this.pendingAudio.length > 1 && ms > STREAM_HOLD_MS) ms -= chunkMs(this.pendingAudio.shift()!);
      return;
    }
    if (!this.micOn) {
      if (this.monitor) { // keep just enough to cover PREROLL_MS
        const ms = chunkMs(b64);
        this.recent.push({ b64, ms });
        this.recentMs += ms;
        while (this.recent.length > 1 && this.recentMs - this.recent[0].ms >= PREROLL_MS) this.recentMs -= this.recent.shift()!.ms;
      }
      return;
    }
    if (this.ready) this.send(inputAudioAppend(b64));
    else this.pendingAudio.push(b64); // still connecting, or the old answer is still being cancelled: send in order later
  }

  private flushPending(): void {
    if (!this.ready) return;
    for (const b64 of this.pendingAudio) this.send(inputAudioAppend(b64));
    this.pendingAudio = [];
    if (this.commitWhenOpen) { this.commitWhenOpen = false; this.send({ type: "input_audio_buffer.commit" }); }
  }

  async stopMic(discard = false): Promise<void> {
    this.micOn = false;
    if (!this.monitor) await this.mic.stop();
    if (discard) {
      this.pendingAudio = [];
      this.commitWhenOpen = false;
      this.send({ type: "input_audio_buffer.clear" });
    }
  }

  commit(): void {
    if (this.ready) this.send({ type: "input_audio_buffer.commit" });
    else this.commitWhenOpen = true; // released before the socket opened (or the cancel was acknowledged): commit right after the held audio
  }

  /**
   * Ask for the answer. The question (typed, or the transcript of what was said) goes as `response.input` (#97 and the
   * 2026-10-24 probe, see events.userInput): with per-response instructions the model sees nothing else of the
   * conversation, not even the committed audio turn, so the instructions carry the passages and `input` the question.
   */
  respond(instructions: string, question?: string): void {
    this.resetResponse();
    this.lastInstructions = instructions;
    this.responseActive = true;
    this.currentResponseId = null;
    this.expectResponse++;
    const q = question?.trim() ?? "";
    this.lastQuestion = q;
    this.sendCreate({ type: "response.create", response: { instructions, max_output_tokens: 600, ...(q ? { input: [userInput(q)] } : {}) } });
  }

  /**
   * Tool calls done (#72): hand every result back as function_call_output (the conversation stays well-formed), then
   * ask for the spoken continuation. The results are repeated as the continuation's `input` message: live (probes 5
   * and 6, 2026-10-24) a function_call_output item is not seen under per-response instructions, a note inside the
   * instructions is not acted on either, and a `system` input role is refused; a `user` input message works.
   */
  returnTools(results: ToolResult[]): void {
    this.toolsOut = false;
    for (const r of results) this.send(functionCallOutput(r.callId, r.output));
    this.responseActive = true;
    this.currentResponseId = null;
    this.expectResponse++;
    const note = toolResultsNote(results, this.lastCallNames, this.lastQuestion);
    this.sendCreate({ type: "response.create", response: { instructions: this.lastInstructions, max_output_tokens: 600, ...(note ? { input: [userInput(note)] } : {}) } });
  }

  /** A new response.create: remembered so a busy server (upstream_error) can be asked once more. */
  private sendCreate(evt: ClientEvent): void {
    this.lastCreate = evt;
    this.busyRetries = 0;
    this.busyFailed = false;
    this.send(evt);
  }

  /** Pause / resume the reply being played (#90): the rest of the reply keeps arriving and waits in the ring buffer. */
  canPause(): boolean { return this.speaker ? this.speaker.canPause !== false : this.player.canPause !== false; }
  async pausePlayback(): Promise<void> { if (this.speaker) this.speaker.pause?.(); else await this.player.pausePlayback?.(); }
  async resumePlayback(): Promise<void> { if (this.speaker) this.speaker.resume?.(); else await this.player.resumePlayback?.(); }

  /**
   * Barge-in: stop playback, truncate the item at what was actually heard, cancel generation, wait for the ack.
   * From here until the next response.created the old answer's late output is dropped (#100): the server generates
   * faster than real time, so its audio and caption deltas keep arriving after the local flush. Mic audio and the
   * commit of the new question are held until the cancel is acknowledged (a commit during an active response is
   * rejected with voice_output_task_ongoing); a cancel that is never acknowledged fails the turn rather than
   * pretending the old answer stopped.
   */
  async interrupt(): Promise<void> {
    const active = this.responseActive;
    if (active) {
      this.interrupting = true;
      this.dropOutput = true;
      // bind the cancel to the response: its late output (or, if it was not created yet, the whole of it) is dropped
      if (this.currentResponseId) this.markCancelled(this.currentResponseId);
      else if (this.expectResponse > 0) { this.expectResponse--; this.cancelUncreated++; }
      this.currentResponseId = null;
    }
    this.stopCaptionTimer();
    const segs = this.segs;
    const heardAudio = this.audioStarted;
    const heardText = this.spoken.join(" ");
    this.stopText(); // text mode: the sentence being spoken stops, the queued ones are dropped
    const playedMs = await this.player.flush();
    if (this.speaker) { if (heardText) this.emit({ type: "heard", text: heardText }); } // text mode: the sentences whose sound had started
    else if (heardAudio && segs.length && this.timed) this.emit({ type: "heard", text: captionAt(segs, playedMs) }); // #135: the words the reader heard
    this.endSearch(false); // B2: a search in progress no longer has a turn to answer
    if (this.assistantItemId && playedMs > 0) this.send(truncateItem(this.assistantItemId, playedMs));
    if (!active) { this.resetResponse(); return; }
    const ack = this.waitForServer((e) => e.type === "response.done" || (e.type === "error" && errorCode(e) === "response_not_active"), this.d.cancelWaitMs);
    this.send({ type: "response.cancel" });
    const got = await ack;
    this.interrupting = false;
    this.resetResponse();
    if (!got) {
      this.pendingAudio = [];
      this.commitWhenOpen = false;
      throw new Error("The character could not be stopped. Try again.");
    }
    this.flushPending(); // the words spoken over the old answer, then the commit if the key was already released
  }

  async setVoice(voice: string): Promise<void> {
    this.voice = voice;
    if (!this.isOpen) return; // the next ensureOpen uses it
    const ack = this.waitForServer((e) => e.type === "session.updated" || e.type === "error", this.d.voiceChangeWaitMs);
    this.send({ type: "session.update", session: { audio: { output: { voice } } } });
    const res = await ack;
    if (!res || res.type === "error") { // not accepted mid-session: reconnect with the new voice
      this.ws?.close(1000, "voice change");
      this.ws = null;
      await this.ensureOpen(voice);
    }
  }

  // ---- server events ---------------------------------------------------------------------------------------------

  private waitForServer(pred: (e: ServerEvent) => boolean, ms: number): Promise<ServerEvent | null> {
    return new Promise((resolve) => {
      const t = setTimeout(() => { this.waiters = this.waiters.filter((w) => w !== h); resolve(null); }, ms);
      const h = (e: ServerEvent) => { if (pred(e)) { clearTimeout(t); this.waiters = this.waiters.filter((w) => w !== h); resolve(e); } };
      this.waiters.push(h);
    });
  }

  private markCancelled(id: string): void {
    if (this.cancelledIds.size > 50) this.cancelledIds.clear();
    this.cancelledIds.add(id);
  }

  /** Output of a response that is not the current one (cancelled, or an older one): never played or captioned. */
  private stale(evt: ServerEvent): boolean {
    if (this.dropOutput) return true;
    const rid = typeof evt.response_id === "string" ? evt.response_id : null;
    if (rid === null) return false;
    return this.cancelledIds.has(rid) || (this.currentResponseId !== null && rid !== this.currentResponseId);
  }

  private resetResponse(): void {
    this.toolsOut = false;
    this.responseActive = false;
    this.responseDone = false;
    this.audioStarted = false;
    this.assistantItemId = null;
    this.caption = "";
    this.pendingCalls = [];
    this.lastCallNames.clear();
    this.clearCaptions();
    this.stopText();
    this.spoken = [];
    this.sentenceCount = 0;
    this.serverToolPending = false;
    this.endSearch(false);
    this.searchCalls.clear();
  }

  // ---- text mode (B1): sentences -> Higgs TTS -> the sentence player -------------------------------------------

  /** Text received: whole sentences go to the speaker now, the tail waits for more (or for `final`). */
  private takeText(delta: string, final = false): void {
    this.textBuf += delta;
    const { done, rest } = takeSentences(this.textBuf, final);
    this.textBuf = rest;
    for (const raw of done) this.enqueueSentence(raw);
  }

  private enqueueSentence(raw: string): void {
    const { text, emotion } = splitMarker(raw);
    if (!text || !this.speaker) return;
    this.d.onSentence?.(text, emotion, this.sentenceCount === 0);
    this.sentenceCount++;
    this.textQueue.push(text);
    this.prefetchText();
    void this.pumpText();
  }

  /** Synthesise the next few queued sentences while the current one plays; rank 0 opens its lip-sync stream too. */
  private prefetchText(): void {
    this.textQueue.slice(0, TEXT_PREFETCH_DEPTH).forEach((t, i) => this.speaker?.prefetch(t, i));
  }

  private async pumpText(): Promise<void> {
    if (this.textPumping || !this.speaker) return;
    this.textPumping = true;
    const epoch = this.textEpoch;
    while (this.textQueue.length && epoch === this.textEpoch) {
      const text = this.textQueue.shift() as string;
      this.textCurrent = text;
      this.prefetchText();
      try {
        await this.speaker.play(text);
      } catch (e) {
        if (epoch !== this.textEpoch) return;
        this.textPumping = false;
        this.failText(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    if (epoch !== this.textEpoch) return;
    this.textCurrent = null;
    this.textPumping = false;
    this.finishTextIfDone();
  }

  /** The sentence player reports sound starting or stopping (text mode). */
  private onSentencePlaying(on: boolean): void {
    if (!on || !this.textCurrent) return;
    if (!this.audioStarted) {
      this.audioStarted = true;
      this.emit({ type: "audio-start" });
      if (this.doneHeld) { const s = this.doneHeld; this.doneHeld = null; this.emit({ type: "response-done", status: s }); }
    }
    if (this.spoken.at(-1) !== this.textCurrent) {
      this.spoken.push(this.textCurrent);
      this.emit({ type: "caption", text: this.spoken.join(" "), done: false });
    }
  }

  /** The model has finished writing and every sentence has been spoken: the turn's playback is over. */
  private finishTextIfDone(): void {
    if (!this.speaker || !this.responseDone || this.textPumping || this.textQueue.length || this.serverToolPending || this.textBuf.trim()) return;
    this.responseDone = false;
    if (this.doneHeld) { const s = this.doneHeld; this.doneHeld = null; this.emit({ type: "response-done", status: s }); } // nothing was heard
    const text = this.spoken.join(" ");
    if (text) this.emit({ type: "caption", text, done: true });
    this.emit({ type: "playback-drained" });
    this.applyModeChange();
  }

  /** Drop what is queued and stop the sentence being spoken (a cut, a new question, a closed socket). */
  private stopText(): void {
    this.doneHeld = null;
    this.textEpoch++;
    this.textBuf = "";
    this.textTotal = "";
    this.textQueue = [];
    this.textPumping = false;
    this.textCurrent = null;
    this.speaker?.stop();
  }

  /** TTS or playback of a sentence failed: the answer ends here, with the message, and the model is told to stop. */
  private failText(message: string): void {
    this.stopText();
    const active = this.responseActive;
    this.responseActive = false;
    this.responseDone = false;
    this.dropOutput = true;
    if (this.currentResponseId) this.markCancelled(this.currentResponseId);
    this.currentResponseId = null;
    this.endSearch(false);
    this.serverToolPending = false;
    if (active) this.send({ type: "response.cancel" });
    this.emit({ type: "error", message });
  }

  // ---- server-side web_search (B2) --------------------------------------------------------------------------------

  private endSearch(completed: boolean): void {
    if (!this.searching) return;
    this.searching = false;
    this.emit({ type: "searching", active: false, completed });
  }

  // ---- voice-timed captions (#135) -----------------------------------------------------------------------------

  /** Captions follow the voice when the player can say how much it has played. */
  private get timed(): boolean { return typeof this.player.playedMs === "function"; }

  private clearCaptions(): void {
    this.stopCaptionTimer();
    this.segs = [];
    this.segMs = 0;
    this.finalCaption = null;
    this.shownCaption = "";
  }

  private stopCaptionTimer(): void {
    if (this.captionTimer !== null) clearInterval(this.captionTimer);
    this.captionTimer = null;
  }

  private tickCaption(): void {
    const text = captionAt(this.segs, this.player.playedMs?.() ?? 0);
    if (text === this.shownCaption) return;
    this.shownCaption = text;
    this.emit({ type: "caption", text, done: false });
  }

  /** The reply finished playing: the whole caption, final. */
  private finishCaption(): void {
    this.stopCaptionTimer();
    const text = this.finalCaption ?? this.segs.map((g) => g.text).join("").trim();
    this.segs = [];
    this.finalCaption = null;
    if (text) this.emit({ type: "caption", text, done: true });
  }

  /**
   * The tool calls of a finished response: `client` are ours to run (`executor` "client", or none); `server` the server
   * runs itself (web_search): it adds the result and starts the answer response, so such a response is not a finished
   * turn (latent bug #4, fixed with B2).
   */
  private splitCalls(response: unknown): { client: ToolCall[]; server: ToolCall[] } {
    const output = (response as { output?: unknown[] } | undefined)?.output;
    const client: ToolCall[] = [];
    const server: ToolCall[] = [];
    let listed = false;
    for (const raw of Array.isArray(output) ? output : []) {
      const item = raw as { type?: string; call_id?: string; name?: string; arguments?: string; executor?: string };
      if (item.type !== "function_call" || typeof item.call_id !== "string" || typeof item.name !== "string") continue;
      listed = true;
      const call = { callId: item.call_id, name: item.name, args: parseArgs(item.arguments) };
      if (item.executor === "server" || (item.executor === undefined && item.name === "web_search")) server.push(call);
      else client.push(call);
    }
    if (listed) return { client, server };
    // a response.done without its items: only the *.arguments.done events named them
    return { client: this.pendingCalls.filter((c) => !this.searchCalls.has(c.callId)), server: this.pendingCalls.filter((c) => this.searchCalls.has(c.callId)) };
  }

  private onMessage(raw: string): void {
    let evt: ServerEvent;
    try { evt = JSON.parse(raw) as ServerEvent; } catch { return; } // never crash on a bad frame
    this.d.onLog?.("recv", evt.type, evt);
    for (const w of [...this.waiters]) w(evt);

    switch (evt.type) {
      case "conversation.item.input_audio_transcription.completed":
        this.emit({ type: "transcript", text: String(evt.transcript ?? ""), ...(typeof evt.item_id === "string" ? { itemId: evt.item_id } : {}) });
        break;
      case "input_audio_buffer.speech_started":
        this.emit({ type: "speech-started", ...(typeof evt.item_id === "string" ? { itemId: evt.item_id } : {}) });
        break;
      case "input_audio_buffer.speech_stopped":
        this.emit({ type: "speech-stopped" });
        break;
      case "input_audio_buffer.committed":
        this.emit({ type: "committed", ...(typeof evt.item_id === "string" ? { itemId: evt.item_id } : {}) });
        break;
      case "session.created":
      case "session.updated": {
        this.sessionAcked = true;
        if (!this.sessionServer) break;
        const t = (evt.session as { audio?: { input?: { turn_detection?: { type?: string; create_response?: boolean } | null } } } | undefined)?.audio?.input?.turn_detection;
        if (t === undefined) break; // an ack without the audio block says nothing about turns
        if (t && t.create_response !== false) this.refuseServerMode("the server would answer every turn by itself", true);
        else if (!t || t.type !== VAD_TYPES[this.vadIndex]) this.refuseServerMode(`asked for ${VAD_TYPES[this.vadIndex]}, got ${t?.type ?? "none"}`);
        break;
      }
      case "conversation.item.input_audio_transcription.failed":
        this.emit({ type: "transcript", text: "" }); // nothing usable was heard: the turn ends now, not after the timeout
        break;
      case "response.created": {
        const rid = (evt.response as { id?: unknown } | undefined)?.id;
        const id = typeof rid === "string" ? rid : null;
        if (id !== null && this.cancelledIds.has(id)) break; // already cancelled
        if (this.cancelUncreated > 0) { // the answer the reader cut off before it started: cancel it, never play it
          this.cancelUncreated--;
          if (id !== null) this.markCancelled(id);
          this.dropOutput = true;
          this.send({ type: "response.cancel" });
          break;
        }
        if (this.expectResponse === 0 && this.sessionServer) { // #135: the server answered a turn by itself; never play it
          if (id !== null) this.markCancelled(id);
          this.dropOutput = true;
          this.send({ type: "response.cancel" });
          break;
        }
        this.currentResponseId = id;
        this.expectResponse = Math.max(0, this.expectResponse - 1);
        this.clearCaptions();
        this.player.reset(); // zero the played-sample counter so audio_end_ms measures this reply only
        this.dropOutput = false; // a new answer: its output is wanted again
        this.responseActive = true;
        this.responseDone = false;
        if (!this.textMode) this.audioStarted = false; // text mode keeps counting across a tool or search continuation
        this.caption = "";
        if (this.serverToolPending) { this.serverToolPending = false; this.endSearch(true); } // B2: the answer after the search
        break;
      }
      case "conversation.item.added": { // B2: the web_search result (the server adds it); its links go under the reply
        const item = evt.item as { type?: string; call_id?: string; output?: unknown } | undefined;
        if (item?.type !== "function_call_output" || typeof item.call_id !== "string" || !this.searchCalls.has(item.call_id)) break;
        const sources = sourcesFromSearchOutput(item.output);
        if (sources.length) this.emit({ type: "sources", sources });
        break;
      }
      case "response.output_item.added": {
        if (this.stale(evt)) break;
        const item = evt.item as { id?: string; type?: string; name?: string; call_id?: string; executor?: string } | undefined;
        if (item?.type === "message" && typeof item.id === "string") this.assistantItemId = item.id;
        if (item?.type === "function_call" && (item.executor === "server" || item.name === "web_search") && typeof item.call_id === "string") {
          this.searchCalls.add(item.call_id); // B2: the server is searching; a "Searching…" chip until its answer starts
          if (!this.searching) { this.searching = true; this.emit({ type: "searching", active: true }); }
        }
        break;
      }
      case "response.output_text.delta": // B1 text mode: the answer as text, voiced sentence by sentence
        if (this.stale(evt) || !this.textMode) break;
        if (typeof evt.item_id === "string") this.assistantItemId = evt.item_id;
        if (typeof evt.delta === "string") { this.textTotal += evt.delta; this.takeText(evt.delta); }
        break;
      case "response.output_text.done": {
        if (this.stale(evt) || !this.textMode) break;
        const full = typeof evt.text === "string" ? evt.text : "";
        if (full.length > this.textTotal.length && full.startsWith(this.textTotal)) this.takeText(full.slice(this.textTotal.length)); // a delta went missing
        this.textTotal = "";
        this.takeText("", true); // the tail is a sentence too
        break;
      }
      case "response.output_audio.delta":
        if (this.stale(evt)) break; // the cut-off answer, generated ahead of what was heard
        if (typeof evt.delta === "string" && evt.delta) {
          if (typeof evt.item_id === "string") this.assistantItemId = evt.item_id;
          this.player.enqueueBase64(evt.delta);
          this.d.onAudioChunk?.(evt.delta, !this.audioStarted);
          if (!this.audioStarted) { this.audioStarted = true; this.emit({ type: "audio-start" }); }
        }
        break;
      case "response.output_audio_transcript.delta":
        if (this.stale(evt)) break;
        this.caption += String(evt.delta ?? "");
        if (!this.timed) this.emit({ type: "caption", text: this.caption, done: false });
        break;
      case "response.output_audio_transcript.length": { // Boson: the fragment again, with its audio duration (#135)
        if (this.stale(evt) || !this.timed) break;
        const len = Number(evt.length_ms);
        if (!Number.isFinite(len) || len < 0) break;
        this.segs.push({ text: String(evt.delta ?? ""), start: this.segMs, end: this.segMs + len });
        this.segMs += len;
        if (this.captionTimer === null) this.captionTimer = setInterval(() => this.tickCaption(), CAPTION_TICK_MS);
        break;
      }
      case "response.output_audio_transcript.done":
        if (this.stale(evt)) break;
        if (this.timed && this.segs.length) { this.finalCaption = String(evt.transcript ?? this.caption); break; } // shown as it is heard
        this.emit({ type: "caption", text: String(evt.transcript ?? this.caption), done: true });
        break;
      case "response.function_call_arguments.done":
        if (this.stale(evt)) break;
        if (typeof evt.call_id === "string" && typeof evt.name === "string") {
          this.pendingCalls.push({ callId: evt.call_id, name: evt.name, args: parseArgs(evt.arguments) });
        }
        break;
      case "response.done": {
        const status = (evt.response as { status?: string } | undefined)?.status;
        const doneId = (evt.response as { id?: unknown } | undefined)?.id;
        if (typeof doneId === "string" && (this.cancelledIds.has(doneId) || (this.currentResponseId !== null && doneId !== this.currentResponseId))) break; // not the current answer
        this.responseActive = false;
        if (this.dropOutput) { this.pendingCalls = []; break; } // the cancelled answer's end is not the new turn's end (the interrupt waiter saw it)
        if (this.busyFailed) {
          this.busyFailed = false;
          if (this.busyRetries < BUSY_RETRIES && this.lastCreate) { // the 1.5 preview was at capacity: ask again shortly
            this.busyRetries++;
            this.pendingCalls = [];
            this.responseActive = true;
            this.currentResponseId = null;
            this.expectResponse++;
            const create = this.lastCreate;
            this.d.onLog?.("info", "busy-retry", `Boson was busy (upstream_error); asking again in ${BUSY_RETRY_MS} ms`);
            setTimeout(() => { if (this.responseActive && this.lastCreate === create) this.send(create); }, BUSY_RETRY_MS);
            break;
          }
          if (this.sessionModel !== REALTIME_MODEL_STABLE && !fellBackToStable) {
            fellBackToStable = true; // the preview keeps refusing: the next session uses the stable model
            this.d.onLog?.("info", "realtime-model", `${this.sessionModel} stayed busy; using ${REALTIME_MODEL_STABLE} from the next session`);
          }
          this.emit({ type: "error", message: "The voice service is busy right now. Try again in a moment." });
        }
        const { client: calls, server } = status === "cancelled" ? { client: [], server: [] } : this.splitCalls(evt.response);
        this.pendingCalls = [];
        if (calls.length) { // the model stopped to act: run the tools, then returnTools() continues the answer
          for (const c of calls) this.lastCallNames.set(c.callId, c.name);
          this.toolsOut = true;
          this.emit({ type: "tool-call", calls });
          break;
        }
        if (server.length) { // B2: the server runs the search, adds its result and starts the answer itself: wait for it
          for (const c of server) this.searchCalls.add(c.callId);
          if (!this.searching) { this.searching = true; this.emit({ type: "searching", active: true }); }
          this.serverToolPending = true;
          this.responseActive = true; // a barge-in now cancels the answer the server is about to start
          this.currentResponseId = null;
          this.expectResponse++; // its response.created is expected, never cancelled as a stray one (#135)
          break;
        }
        this.responseDone = true;
        if (!this.textMode && this.audioStarted) this.player.endTurn?.();
        const doneStatus = status === "cancelled" ? "cancelled" : status === "failed" ? "failed" : "completed";
        // text mode: the model may finish writing before the first sentence is heard (TTS takes ~2.5 s). A response-done
        // before audio-start reads as "an answer with no sound" to the controller, so it waits for that sound
        if (this.textMode && !this.audioStarted && (this.textQueue.length > 0 || this.textPumping || this.textBuf.trim() !== "")) {
          this.doneHeld = doneStatus;
        } else {
          this.emit({ type: "response-done", status: doneStatus });
        }
        if (this.textMode) this.finishTextIfDone(); // drained once the last sentence has been spoken (or at once, nothing to say)
        else if (!this.audioStarted) { this.emit({ type: "playback-drained" }); this.applyModeChange(); } // nothing was spoken, nothing to wait for
        break;
      }
      case "error": {
        const err = (evt.error ?? evt) as { type?: string; code?: string | null; message?: string; event_id?: string; param?: string | null };
        if (this.sessionServer && !this.sessionAcked && /turn_detection|vad/i.test(`${err.message ?? ""} ${err.param ?? ""}`)) {
          this.refuseServerMode(err.message ?? "turn detection refused"); // the config was refused (the session closes next)
          break;
        }
        // before the first ack, an error that is not about quota/auth/rate means the preview rejected our session.update
        if (!this.sessionAcked && this.sessionModel !== REALTIME_MODEL_STABLE && !(typeof err.event_id === "string" && err.event_id.startsWith("seed-"))
            && !/quota|auth|rate_limit/i.test(`${err.type ?? ""} ${err.code ?? ""}`) && errorCode(evt) !== "response_not_active") {
          this.fallBackToStable(err.message ?? "session refused"); // the preview refused our session.update
          break;
        }
        if (typeof err.event_id === "string" && err.event_id.startsWith("seed-")) break; // a history item was refused: the chat just starts fresh
        if (errorCode(evt) === "response_not_active") break; // cancelling an already-finished response is harmless
        if (errorCode(evt) === "upstream_error" && this.responseActive && !this.audioStarted && !this.spoken.length) {
          this.busyFailed = true; // its response.done follows: asked again there, or reported once out of retries
          break;
        }
        this.emit({ type: "error", message: err.message ?? "The voice service reported an error." });
        break;
      }
      case "session.idle_timeout":
      case "session.max_duration_reached":
        break; // normal: the socket closes next and the following press reconnects, no toast
      default:
        break; // unknown event types (e.g. conversation.context.summarized) are expected
    }
  }

  private onSpeaking(speaking: boolean): void {
    if (this.textMode) return; // text mode answers drain through the sentence player (finishTextIfDone)
    if (!speaking && this.responseDone && this.audioStarted) {
      if (this.timed && (this.segs.length || this.finalCaption !== null)) this.finishCaption();
      this.emit({ type: "playback-drained" });
      this.responseDone = false;
      this.applyModeChange();
    }
  }
}

/**
 * The input message of a tool continuation: what the reader asked and what each tool did. Live, a function_call_output
 * item is not visible to a response that carries `instructions`, so this is how the model learns the result it must
 * confirm ("Moved to Chapter Two") or the refusal it must relay ("spoilers are off"). Empty when there is nothing to tell.
 */
export function toolResultsNote(results: ToolResult[], names: Map<string, string>, question = ""): string {
  if (!results.length) return "";
  const lines = results.map((r) => `- ${names.get(r.callId) ?? "tool"}: ${r.output.trim().slice(0, 500)}`);
  const asked = question.trim() ? `, who said: "${question.trim().slice(0, 300)}"` : "";
  return `You just acted for the reader${asked}. What each tool did:\n${lines.join("\n")}\nTell the reader what happened in one short spoken sentence, in character. Do not call the tool again.`;
}

/** The `code` of a server error event (nested under `error`, or flat). */
function errorCode(evt: ServerEvent): string | null {
  const err = (evt.error ?? evt) as { code?: unknown };
  return typeof err.code === "string" ? err.code : null;
}

/** A tool call's JSON arguments, or {} when they are missing or malformed (the tool reports the problem, never throws). */
function parseArgs(raw: unknown): Record<string, unknown> {
  if (typeof raw !== "string" || !raw.trim()) return {};
  try { const v = JSON.parse(raw); return v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {}; } catch { return {}; }
}
