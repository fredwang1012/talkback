import { AUDIO_RATE } from "../audio/constants";
import { describeClose, functionCallOutput, historyItem, inputAudioAppend, truncateItem, userTextItem, type ClientEvent, type ServerEvent } from "../realtime/events";
import type { SessionEvent, TalkSession, ToolCall, ToolResult } from "./TalkController";
import { TOOL_DEFS } from "./tools";

const WS_URL = "wss://api.boson.ai/v1/realtime?model=higgs-realtime";
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
const PRESS_PREROLL_MS = 300;

/** Server-mode hands-free (#135): mic audio held while the socket is not open (or a cancel is out), newest kept. */
export const STREAM_HOLD_MS = 3000;
/** Caption reveal tick (#135): how often the played position is read while an answer plays. */
export const CAPTION_TICK_MS = 100;
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
  /** The function tools declared to the session (#72); `[]` declares none. Never Boson's server-side web_search. */
  tools?: readonly unknown[];
}

/** Close code 1013 before the session opened: too many concurrent connections. Safe to retry after a pause. */
export class ConnectionLimitError extends Error {}
/** The socket was dropped before it opened (a turn-mode change): openWithBackoff opens the next one at once. */
class SupersededError extends Error {}

export type SessionOptions = { turnDetection?: Record<string, unknown> | null; language?: string | null };

export function sessionConfig(voice: string, tools: readonly unknown[] = TOOL_DEFS, opts: SessionOptions = {}): ClientEvent {
  return {
    type: "session.update",
    session: {
      type: "realtime",
      model: "higgs-realtime",
      instructions: SESSION_INSTRUCTIONS,
      output_modalities: ["audio"],
      tools, // client function tools only: the model asks, the browser acts (see tools.ts)
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
        output: { format: { type: "audio/pcm", rate: AUDIO_RATE }, voice },
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
  private pendingCalls: ToolCall[] = []; // tool calls announced by function_call_arguments.done, until response.done

  // #135 server turn detection: wanted (hands-free), and the VAD type the open (or next) session uses
  private wantServer = false;
  private modeChangeDue = false; // #141: the session's turn mode is wrong, but an answer is playing: switch when it ends
  private sessionRefused = false; // #141: the open session's turn detection was refused; it is replaced once idle
  private toolsOut = false; // tool calls handed to the reader, returnTools not called yet
  private vadIndex = 0;
  private sessionServer = false; // the open session was configured with server turn detection
  private sessionAcked = false; // its session.created was seen
  private expectResponse = 0; // response.create calls not yet matched by a response.created
  private language: string | null = null;
  // #135 voice-timed captions
  private segs: CaptionSegment[] = [];
  private segMs = 0;
  private finalCaption: string | null = null;
  private shownCaption = "";
  private captionTimer: ReturnType<typeof setInterval> | null = null;

  constructor(deps: TalkClientDeps) {
    this.d = { cancelWaitMs: 2000, voiceChangeWaitMs: 3000, backoffMs: [1000, 2000, 4000], ...deps };
    this.player = deps.createPlayer((speaking) => this.onSpeaking(speaking));
    this.mic = deps.createMic((b64) => this.onMicChunk(b64), deps.onLevel);
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
      const ws = make(WS_URL, ["realtime", `bai-client-secret.${key}`]);
      this.ws = ws;
      ws.onopen = () => {
        opened = true;
        this.sessionServer = this.wantServer;
        this.sessionAcked = false;
        const turn = this.sessionServer ? turnDetection(VAD_TYPES[this.vadIndex]) : null;
        // the first session.update starts the session; no need to wait for session.created
        this.send(sessionConfig(voice, this.d.tools, { turnDetection: turn, language: this.language }));
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
    if (!on) this.pendingAudio = []; // held for a server session that will not come: never the start of the next press
    if (!this.ws || (this.sessionServer === on && !this.sessionRefused)) { this.modeChangeDue = false; return; }
    if (this.turnBusy) this.modeChangeDue = true; // #141: never drop the socket under an answer; switch once it drained
    else this.dropSocket("turn mode change");
  }

  /** A turn is in progress (audio going out, an answer generated, played or waiting for its tools): a socket dropped
   * now would never report the answer's end, and the controller would stay "answering" (#141). */
  private get turnBusy(): boolean {
    return this.micOn || this.responseActive || (this.responseDone && this.audioStarted) || this.expectResponse > 0 || this.toolsOut;
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

  close(): void {
    this.stopCaptionTimer();
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

  /** Audio and commits may go out: the socket is open and no cancel is waiting for its acknowledgement. */
  private get ready(): boolean { return this.isOpen && !this.interrupting; }

  private onMicChunk(b64: string): void {
    if (this.monitor && this.wantServer) { // #135: the server listens all the time and decides the turns
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
   * Ask for the answer. A typed question (`question`) is first put into the conversation as a user text item (#97):
   * the instructions carry the passages, not the question, and a bare response.create answers an empty turn. A spoken
   * question needs none: the committed audio is the user item.
   */
  respond(instructions: string, question?: string): void {
    this.resetResponse();
    this.lastInstructions = instructions;
    this.responseActive = true;
    if (question?.trim()) this.send(userTextItem(question.trim()));
    this.expectResponse++;
    this.send({ type: "response.create", response: { instructions, max_output_tokens: 600 } });
  }

  /** Tool calls done (#72): hand every result back, then ask for the spoken continuation under the same instructions. */
  returnTools(results: ToolResult[]): void {
    this.toolsOut = false;
    for (const r of results) this.send(functionCallOutput(r.callId, r.output));
    this.responseActive = true;
    this.expectResponse++;
    this.send({ type: "response.create", response: { instructions: this.lastInstructions, max_output_tokens: 600 } });
  }

  /** Pause / resume the reply being played (#90): the rest of the reply keeps arriving and waits in the ring buffer. */
  async pausePlayback(): Promise<void> { await this.player.pausePlayback?.(); }
  async resumePlayback(): Promise<void> { await this.player.resumePlayback?.(); }

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
    if (active) { this.interrupting = true; this.dropOutput = true; }
    this.stopCaptionTimer();
    const segs = this.segs;
    const heardAudio = this.audioStarted;
    const playedMs = await this.player.flush();
    if (heardAudio && segs.length && this.timed) this.emit({ type: "heard", text: captionAt(segs, playedMs) }); // #135: the words the reader heard
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

  private resetResponse(): void {
    this.toolsOut = false;
    this.responseActive = false;
    this.responseDone = false;
    this.audioStarted = false;
    this.assistantItemId = null;
    this.caption = "";
    this.pendingCalls = [];
    this.clearCaptions();
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

  /** The tool calls of a finished response that are ours to run: `executor` "client" (the server runs its own, e.g. web_search). */
  private clientCalls(response: unknown): ToolCall[] {
    const output = (response as { output?: unknown[] } | undefined)?.output;
    const calls: ToolCall[] = [];
    let listed = false;
    for (const raw of Array.isArray(output) ? output : []) {
      const item = raw as { type?: string; call_id?: string; name?: string; arguments?: string; executor?: string };
      if (item.type !== "function_call" || typeof item.call_id !== "string" || typeof item.name !== "string") continue;
      listed = true;
      if (item.executor !== undefined && item.executor !== "client") continue;
      calls.push({ callId: item.call_id, name: item.name, args: parseArgs(item.arguments) });
    }
    return listed ? calls : this.pendingCalls; // a response.done without its items: only the *.arguments.done events named them
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
        if (!this.sessionServer) break;
        this.sessionAcked = true;
        const t = (evt.session as { audio?: { input?: { turn_detection?: { type?: string; create_response?: boolean } | null } } } | undefined)?.audio?.input?.turn_detection;
        if (t === undefined) break; // an ack without the audio block says nothing about turns
        if (t && t.create_response !== false) this.refuseServerMode("the server would answer every turn by itself", true);
        else if (!t || t.type !== VAD_TYPES[this.vadIndex]) this.refuseServerMode(`asked for ${VAD_TYPES[this.vadIndex]}, got ${t?.type ?? "none"}`);
        break;
      }
      case "conversation.item.input_audio_transcription.failed":
        this.emit({ type: "transcript", text: "" }); // nothing usable was heard: the turn ends now, not after the timeout
        break;
      case "response.created":
        if (this.expectResponse === 0 && this.sessionServer) { // #135: the server answered a turn by itself; never play it
          this.dropOutput = true;
          this.send({ type: "response.cancel" });
          break;
        }
        this.expectResponse = Math.max(0, this.expectResponse - 1);
        this.clearCaptions();
        this.player.reset(); // zero the played-sample counter so audio_end_ms measures this reply only
        this.dropOutput = false; // a new answer: its output is wanted again
        this.responseActive = true;
        this.responseDone = false;
        this.audioStarted = false;
        this.caption = "";
        break;
      case "response.output_item.added": {
        if (this.dropOutput) break;
        const item = evt.item as { id?: string; type?: string } | undefined;
        if (item?.type === "message" && typeof item.id === "string") this.assistantItemId = item.id;
        break;
      }
      case "response.output_audio.delta":
        if (this.dropOutput) break; // the cut-off answer, generated ahead of what was heard
        if (typeof evt.delta === "string" && evt.delta) {
          if (typeof evt.item_id === "string") this.assistantItemId = evt.item_id;
          this.player.enqueueBase64(evt.delta);
          this.d.onAudioChunk?.(evt.delta, !this.audioStarted);
          if (!this.audioStarted) { this.audioStarted = true; this.emit({ type: "audio-start" }); }
        }
        break;
      case "response.output_audio_transcript.delta":
        if (this.dropOutput) break;
        this.caption += String(evt.delta ?? "");
        if (!this.timed) this.emit({ type: "caption", text: this.caption, done: false });
        break;
      case "response.output_audio_transcript.length": { // Boson: the fragment again, with its audio duration (#135)
        if (this.dropOutput || !this.timed) break;
        const len = Number(evt.length_ms);
        if (!Number.isFinite(len) || len < 0) break;
        this.segs.push({ text: String(evt.delta ?? ""), start: this.segMs, end: this.segMs + len });
        this.segMs += len;
        if (this.captionTimer === null) this.captionTimer = setInterval(() => this.tickCaption(), CAPTION_TICK_MS);
        break;
      }
      case "response.output_audio_transcript.done":
        if (this.dropOutput) break;
        if (this.timed && this.segs.length) { this.finalCaption = String(evt.transcript ?? this.caption); break; } // shown as it is heard
        this.emit({ type: "caption", text: String(evt.transcript ?? this.caption), done: true });
        break;
      case "response.function_call_arguments.done":
        if (this.dropOutput) break;
        if (typeof evt.call_id === "string" && typeof evt.name === "string") {
          this.pendingCalls.push({ callId: evt.call_id, name: evt.name, args: parseArgs(evt.arguments) });
        }
        break;
      case "response.done": {
        const status = (evt.response as { status?: string } | undefined)?.status;
        this.responseActive = false;
        if (this.dropOutput) { this.pendingCalls = []; break; } // the cancelled answer's end is not the new turn's end (the interrupt waiter saw it)
        const calls = status === "cancelled" ? [] : this.clientCalls(evt.response);
        this.pendingCalls = [];
        if (calls.length) { // the model stopped to act: run the tools, then returnTools() continues the answer
          this.toolsOut = true;
          this.emit({ type: "tool-call", calls });
          break;
        }
        this.responseDone = true;
        this.emit({ type: "response-done", status: status === "cancelled" ? "cancelled" : status === "failed" ? "failed" : "completed" });
        if (!this.audioStarted) { this.emit({ type: "playback-drained" }); this.applyModeChange(); } // nothing was spoken, nothing to wait for
        break;
      }
      case "error": {
        const err = (evt.error ?? evt) as { type?: string; code?: string | null; message?: string; event_id?: string; param?: string | null };
        if (this.sessionServer && !this.sessionAcked && /turn_detection|vad/i.test(`${err.message ?? ""} ${err.param ?? ""}`)) {
          this.refuseServerMode(err.message ?? "turn detection refused"); // the config was refused (the session closes next)
          break;
        }
        if (typeof err.event_id === "string" && err.event_id.startsWith("seed-")) break; // a history item was refused: the chat just starts fresh
        if (errorCode(evt) === "response_not_active") break; // cancelling an already-finished response is harmless
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
    if (!speaking && this.responseDone && this.audioStarted) {
      if (this.timed && (this.segs.length || this.finalCaption !== null)) this.finishCaption();
      this.emit({ type: "playback-drained" });
      this.responseDone = false;
      this.applyModeChange();
    }
  }
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
