/**
 * The core loop as an explicit state machine (brief 7.3):
 *   reading -> listening -> transcribing -> retrieving -> answering -> resuming -> reading   (+ talk-idle)
 * Push-to-talk only. It talks to the network through `TalkSession` and to the reader through `ReaderControl`,
 * so the whole loop can be tested with fakes.
 */

export type TalkState =
  | "reading" | "listening" | "transcribing" | "retrieving" | "answering" | "resuming" | "talk-idle";

export type SessionEvent =
  | { type: "transcript"; text: string }
  | { type: "audio-start" }
  | { type: "caption"; text: string; done: boolean }
  | { type: "response-done"; status: "completed" | "cancelled" | "failed" }
  | { type: "playback-drained" }
  | { type: "error"; message: string }
  | { type: "closed"; message?: string };

export interface TalkSession {
  /** Open the socket (fresh key) with this voice if it is not open; no-op when already open. Idempotent. */
  ensureOpen(voice: string): Promise<void>;
  /** Switch the voice of an open session (reconnects if the server rejects mid-session changes). */
  setVoice(voice: string): Promise<void>;
  /** Start streaming mic audio; audio captured while the socket is still connecting is buffered then sent. */
  startMic(): Promise<void>;
  /** Stop the mic. With `discard`, drop what was captured instead of keeping it for a commit. */
  stopMic(discard?: boolean): Promise<void>;
  commit(): void;
  /** Ask for a spoken answer using per-response instructions. */
  respond(instructions: string): void;
  /** Flush local playback, truncate the interrupted item at the played position and cancel the response. */
  interrupt(): Promise<void>;
  on(fn: (e: SessionEvent) => void): () => void;
}

export interface ReaderControl {
  isPlaying(): boolean;
  currentIndex(): number;
  currentPara(): number;
  pause(): void;
  play(): void;
  resumeAt(index: number): void;
}

export type RetrieveFn = (query: string, readerPara: number, speakerId: string) => Promise<{ instructions: string }>;

export type Latency = { commitToTranscriptMs?: number; retrieveMs?: number; firstAudioMs?: number };

export interface TalkDeps {
  session: TalkSession;
  reader: ReaderControl;
  retrieve: RetrieveFn;
  voiceFor: (speakerId: string) => string;
  onState?: (s: TalkState) => void;
  onToast?: (message: string) => void;
  onCaption?: (text: string, done: boolean) => void;
  onLatency?: (l: Latency) => void;
  now?: () => number;
  autoResumeDelayMs?: number;
  transcriptTimeoutMs?: number;
  retrieveTimeoutMs?: number;
  tapMs?: number;
}

export class TalkController {
  private d: Required<Pick<TalkDeps, "now" | "autoResumeDelayMs" | "transcriptTimeoutMs" | "retrieveTimeoutMs" | "tapMs">> & TalkDeps;
  private _state: TalkState = "reading";
  private speakerId = "narrator";
  private autoResume = true;
  private resumeIndex: number | null = null;
  private wasPlayingBeforePress = false;
  private pressedAt = 0;
  private turn = 0; // bumped whenever a turn is abandoned, so stale async work is ignored
  private resumeTimer: ReturnType<typeof setTimeout> | null = null;
  private waiters: { predicate: (e: SessionEvent) => boolean; resolve: (e: SessionEvent | null) => void }[] = [];
  private latency: Latency = {};
  private off: () => void;

  constructor(deps: TalkDeps) {
    this.d = { now: () => Date.now(), autoResumeDelayMs: 1500, transcriptTimeoutMs: 2500, retrieveTimeoutMs: 8000, tapMs: 300, ...deps };
    this.off = deps.session.on((e) => this.onSession(e));
  }

  get state(): TalkState { return this._state; }

  private set(s: TalkState): void {
    if (s === this._state) return;
    this._state = s;
    this.d.onState?.(s);
  }

  setAutoResume(v: boolean): void { this.autoResume = v; }

  /** Picking someone else to talk to; applied to the session voice before the next question. */
  async setSpeaker(id: string): Promise<void> {
    this.speakerId = id;
    try { await this.d.session.setVoice(this.d.voiceFor(id)); } catch { /* the next ensureOpen reconnects with the right voice */ }
  }

  dispose(): void {
    this.off();
    this.clearResumeTimer();
    this.turn++;
    this.waiters.forEach((w) => w.resolve(null));
    this.waiters = [];
  }

  // ---- push to talk ---------------------------------------------------------------------------------------

  async pttDown(): Promise<void> {
    this.clearResumeTimer(); // pressing during the 1.5 s window starts a follow-up question
    const turn = ++this.turn;
    this.pressedAt = this.d.now();
    const wasAnswering = this._state === "answering";
    const wasBusy = ["transcribing", "retrieving"].includes(this._state);

    if (this.resumeIndex === null) {
      this.wasPlayingBeforePress = this.d.reader.isPlaying();
      this.resumeIndex = this.d.reader.currentIndex(); // start of the current chunk = start of the sentence
    }
    if (this.d.reader.isPlaying()) this.d.reader.pause();
    this.d.onCaption?.("", false);
    this.set("listening");

    if (wasAnswering || wasBusy) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    if (turn !== this.turn) return;
    // connect and capture in parallel: the mic is buffered until the socket is open
    this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)).catch((e: unknown) => this.fail(turn, e));
    try { await this.d.session.startMic(); } catch (e) { this.fail(turn, e, "Microphone unavailable. Check browser permission."); }
  }

  async pttUp(): Promise<void> {
    if (this._state !== "listening") return;
    const turn = this.turn;
    const held = this.d.now() - this.pressedAt;

    if (held < this.d.tapMs) { // a tap toggles play/pause instead of asking a question
      await this.d.session.stopMic(true);
      const wasPlaying = this.wasPlayingBeforePress;
      const index = this.resumeIndex;
      this.resumeIndex = null;
      this.set("reading");
      if (!wasPlaying && index !== null) this.d.reader.play();
      return;
    }

    await this.d.session.stopMic();
    if (turn !== this.turn) return;
    this.set("transcribing");
    const t0 = this.d.now();
    this.d.session.commit();
    const transcript = await this.waitFor((e) => e.type === "transcript", this.d.transcriptTimeoutMs);
    if (turn !== this.turn) return;
    const query = transcript && transcript.type === "transcript" ? transcript.text.trim() : "";
    this.latency = { commitToTranscriptMs: this.d.now() - t0 };

    this.set("retrieving");
    const r0 = this.d.now();
    let instructions: string;
    try {
      const res = await this.withTimeout(this.d.retrieve(query, this.d.reader.currentPara(), this.speakerId), this.d.retrieveTimeoutMs);
      instructions = res.instructions;
    } catch (e) {
      if (turn !== this.turn) return;
      this.d.onToast?.(`Could not look that up: ${e instanceof Error ? e.message : String(e)}. Continuing the story.`);
      this.backToReading();
      return;
    }
    if (turn !== this.turn) return;
    this.latency.retrieveMs = this.d.now() - r0;

    this.set("answering");
    const a0 = this.d.now();
    this.d.session.respond(instructions);
    void this.awaitAnswer(turn, a0); // key release returns now; the answer is handled in the background
  }

  private async awaitAnswer(turn: number, a0: number): Promise<void> {
    const first = await this.waitFor((e) => e.type === "audio-start" || e.type === "response-done" || e.type === "error", 15000);
    if (turn !== this.turn) return;
    if (first?.type === "audio-start") {
      this.latency.firstAudioMs = this.d.now() - a0;
      this.d.onLatency?.({ ...this.latency });
    } else if (first?.type === "response-done") {
      this.afterAnswer(); // finished without any audio: nothing to wait for
    } else if (!first) {
      this.d.onToast?.("The answer took too long. Continuing the story.");
      void this.d.session.interrupt().catch(() => {});
      this.backToReading();
    }
    // audio-start: the answer is playing; the rest is handled by session events (response-done, playback-drained)
  }

  // ---- session events ---------------------------------------------------------------------------------------

  private onSession(e: SessionEvent): void {
    for (const w of [...this.waiters]) {
      if (w.predicate(e)) { this.waiters = this.waiters.filter((x) => x !== w); w.resolve(e); }
    }
    if (e.type === "caption") this.d.onCaption?.(e.text, e.done);
    if (e.type === "error") {
      this.d.onToast?.(e.message);
      if (["listening", "transcribing", "retrieving", "answering"].includes(this._state)) { this.turn++; this.backToReading(); }
    }
    if (e.type === "closed" && this._state === "answering") { this.turn++; this.backToReading(); }
    if (e.type === "playback-drained" && this._state === "answering") this.afterAnswer();
  }

  private afterAnswer(): void {
    if (this.autoResume && this.resumeIndex !== null && this.wasPlayingBeforePress) {
      const turn = this.turn;
      this.set("resuming");
      this.resumeTimer = setTimeout(() => {
        if (turn !== this.turn || this._state !== "resuming") return;
        this.backToReading();
      }, this.d.autoResumeDelayMs);
    } else {
      this.resumeIndex = null;
      this.set("talk-idle");
    }
  }

  /** Back to where we were: resume reading only if the story was playing when the question started. */
  private backToReading(): void {
    const index = this.resumeIndex;
    const wasPlaying = this.wasPlayingBeforePress;
    this.resumeIndex = null;
    this.clearResumeTimer();
    if (index !== null && wasPlaying) {
      this.set("reading");
      this.d.reader.resumeAt(index);
    } else {
      this.set(index !== null ? "talk-idle" : "reading"); // Talk mode without reading: stay idle, never start playing
    }
  }

  private fail(turn: number, e: unknown, message?: string): void {
    if (turn !== this.turn) return;
    this.d.onToast?.(message ?? (e instanceof Error ? e.message : String(e)));
    this.turn++;
    void this.d.session.stopMic(true);
    this.backToReading();
  }

  private clearResumeTimer(): void {
    if (this.resumeTimer) clearTimeout(this.resumeTimer);
    this.resumeTimer = null;
  }

  private waitFor(predicate: (e: SessionEvent) => boolean, ms: number): Promise<SessionEvent | null> {
    return new Promise((resolve) => {
      const w = { predicate, resolve: (e: SessionEvent | null) => { clearTimeout(timer); resolve(e); } };
      const timer = setTimeout(() => { this.waiters = this.waiters.filter((x) => x !== w); resolve(null); }, ms);
      this.waiters.push(w);
    });
  }

  private withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("timed out")), ms);
      p.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
    });
  }
}
