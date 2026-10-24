import { AUDIO_RATE } from "../audio/constants";
import { describeClose, inputAudioAppend, truncateItem, type ClientEvent, type ServerEvent } from "../realtime/events";
import type { SessionEvent, TalkSession } from "./TalkController";

const WS_URL = "wss://api.boson.ai/v1/realtime?model=higgs-realtime";
const OPEN = 1; // WebSocket.OPEN, spelled out so tests need no global WebSocket

/** Short and behavioural: the role comes from per-response instructions (brief 3.4 prompting lesson). */
export const SESSION_INSTRUCTIONS =
  "You are a spoken reading companion. Follow the instructions given with each response. Speak naturally; no markdown.";

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
  onLog?: (dir: "send" | "recv" | "info", type: string, raw: unknown) => void;
  cancelWaitMs?: number;
  voiceChangeWaitMs?: number;
  /** Waits between retries when Boson refuses a connection with close code 1013 (concurrency limit). */
  backoffMs?: number[];
}

/** Close code 1013 before the session opened: too many concurrent connections. Safe to retry after a pause. */
export class ConnectionLimitError extends Error {}

export function sessionConfig(voice: string): ClientEvent {
  return {
    type: "session.update",
    session: {
      type: "realtime",
      model: "higgs-realtime",
      instructions: SESSION_INSTRUCTIONS,
      output_modalities: ["audio"],
      audio: {
        input: {
          format: { type: "audio/pcm", rate: AUDIO_RATE },
          turn_detection: null, // push-to-talk: we commit each turn ourselves
          transcription: { model: "higgs-stt-3.1" },
          noise_reduction: { type: "near_field" },
        },
        output: { format: { type: "audio/pcm", rate: AUDIO_RATE }, voice },
      },
      temperature: 0.3,
    },
  };
}

export class TalkClient implements TalkSession {
  private d: TalkClientDeps & { cancelWaitMs: number; voiceChangeWaitMs: number; backoffMs: number[] };
  private ws: SocketLike | null = null;
  private opening: Promise<void> | null = null; // the idempotency guard (React StrictMode opens two sockets otherwise)
  private voice = "default";
  private player: PlayerLike;
  private mic: MicLike;
  private micOn = false;
  private pendingAudio: string[] = [];
  private commitWhenOpen = false;
  private listeners = new Set<(e: SessionEvent) => void>();
  private waiters: ((e: ServerEvent) => void)[] = [];

  private responseActive = false;
  private responseDone = false;
  private audioStarted = false;
  private assistantItemId: string | null = null;
  private caption = "";

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
        this.send(sessionConfig(voice)); // the first session.update starts the session; no need to wait for session.created
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
        if (!wasThis) return;
        this.resetResponse();
        if (ev.code === 1013 && !opened) { reject(new ConnectionLimitError(info.message)); return; }
        if (ev.code === 3000 || ev.code === 4429) this.emit({ type: "error", message: info.message });
        else if (ev.code === 1013) this.emit({ type: "error", message: "Boson is at its connection limit. Try again in a moment." });
        else this.emit({ type: "closed", message: info.message }); // idle timeout / max duration: reconnect on next press
        reject(new Error(info.message)); // no-op if already opened
      };
    });
  }

  close(): void {
    this.ws?.close(1000, "client disconnect");
    this.ws = null;
    void this.mic.stop();
    void this.player.close();
  }

  // ---- microphone / turns -------------------------------------------------------------------------------------

  async startMic(): Promise<void> {
    this.micOn = true;
    this.commitWhenOpen = false;
    await this.mic.start();
  }

  private onMicChunk(b64: string): void {
    if (!this.micOn) return;
    if (this.isOpen) this.send(inputAudioAppend(b64));
    else this.pendingAudio.push(b64); // still connecting: keep it and send in order once open
  }

  private flushPending(): void {
    for (const b64 of this.pendingAudio) this.send(inputAudioAppend(b64));
    this.pendingAudio = [];
    if (this.commitWhenOpen) { this.commitWhenOpen = false; this.send({ type: "input_audio_buffer.commit" }); }
  }

  async stopMic(discard = false): Promise<void> {
    this.micOn = false;
    await this.mic.stop();
    if (discard) {
      this.pendingAudio = [];
      this.commitWhenOpen = false;
      this.send({ type: "input_audio_buffer.clear" });
    }
  }

  commit(): void {
    if (this.isOpen) this.send({ type: "input_audio_buffer.commit" });
    else this.commitWhenOpen = true; // released before the socket opened: commit right after the buffered audio
  }

  respond(instructions: string): void {
    this.resetResponse();
    this.responseActive = true;
    this.send({ type: "response.create", response: { instructions, max_output_tokens: 600 } });
  }

  /** Barge-in: stop playback, truncate the item at what was actually heard, cancel generation, wait for the ack. */
  async interrupt(): Promise<void> {
    const playedMs = await this.player.flush();
    if (this.assistantItemId && playedMs > 0) this.send(truncateItem(this.assistantItemId, playedMs));
    if (this.responseActive) {
      const done = this.waitForServer((e) => e.type === "response.done", this.d.cancelWaitMs);
      this.send({ type: "response.cancel" });
      await done; // a commit while a response is active is rejected (voice_output_task_ongoing)
    }
    this.resetResponse();
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
    this.responseActive = false;
    this.responseDone = false;
    this.audioStarted = false;
    this.assistantItemId = null;
    this.caption = "";
  }

  private onMessage(raw: string): void {
    let evt: ServerEvent;
    try { evt = JSON.parse(raw) as ServerEvent; } catch { return; } // never crash on a bad frame
    this.d.onLog?.("recv", evt.type, evt);
    for (const w of [...this.waiters]) w(evt);

    switch (evt.type) {
      case "conversation.item.input_audio_transcription.completed":
        this.emit({ type: "transcript", text: String(evt.transcript ?? "") });
        break;
      case "response.created":
        this.player.reset(); // zero the played-sample counter so audio_end_ms measures this reply only
        this.responseActive = true;
        this.responseDone = false;
        this.audioStarted = false;
        this.caption = "";
        break;
      case "response.output_item.added": {
        const item = evt.item as { id?: string; type?: string } | undefined;
        if (item?.type === "message" && typeof item.id === "string") this.assistantItemId = item.id;
        break;
      }
      case "response.output_audio.delta":
        if (typeof evt.delta === "string" && evt.delta) {
          if (typeof evt.item_id === "string") this.assistantItemId = evt.item_id;
          this.player.enqueueBase64(evt.delta);
          this.d.onAudioChunk?.(evt.delta, !this.audioStarted);
          if (!this.audioStarted) { this.audioStarted = true; this.emit({ type: "audio-start" }); }
        }
        break;
      case "response.output_audio_transcript.delta":
        this.caption += String(evt.delta ?? "");
        this.emit({ type: "caption", text: this.caption, done: false });
        break;
      case "response.output_audio_transcript.done":
        this.emit({ type: "caption", text: String(evt.transcript ?? this.caption), done: true });
        break;
      case "response.done": {
        const status = (evt.response as { status?: string } | undefined)?.status;
        this.responseActive = false;
        this.responseDone = true;
        this.emit({ type: "response-done", status: status === "cancelled" ? "cancelled" : status === "failed" ? "failed" : "completed" });
        if (!this.audioStarted) this.emit({ type: "playback-drained" }); // nothing was spoken, nothing to wait for
        break;
      }
      case "error": {
        const err = (evt.error ?? evt) as { type?: string; code?: string | null; message?: string };
        if (err.code === "response_not_active") break; // cancelling an already-finished response is harmless
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
      this.emit({ type: "playback-drained" });
      this.responseDone = false;
    }
  }
}
