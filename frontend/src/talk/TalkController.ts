import { micErrorMessage } from "../audio/mediaErrors";
/**
 * The core loop as an explicit state machine (brief 7.3):
 *   reading -> listening -> transcribing -> retrieving -> answering -> resuming -> reading   (+ talk-idle)
 * Push-to-talk only. It talks to the network through `TalkSession` and to the reader through `ReaderControl`,
 * so the whole loop can be tested with fakes.
 */

import type { SentenceSpeaker } from "./sentencePlayer";
import type { TimingEvent } from "./speechGate";
import { parseIntent, type ToolOutcome, type ToolRunner } from "./tools";

/** How many queued sentences are synthesised ahead of the one being spoken. */
export const PREFETCH_DEPTH = 3;

export type TalkState =
  | "reading" | "listening" | "transcribing" | "retrieving" | "answering" | "resuming" | "talk-idle";

/** A function call the model made (#72): ours to run, then answer with `returnTools`. */
export type ToolCall = { callId: string; name: string; args: Record<string, unknown> };
export type ToolResult = { callId: string; output: string };

export type SessionEvent =
  | { type: "transcript"; text: string }
  | { type: "tool-call"; calls: ToolCall[] }
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
  /** Hand the results of a `tool-call` back and let the model go on speaking. Optional: a session without tools never asks. */
  returnTools?(results: ToolResult[]): void;
  /** Hold local playback where it is (the reply keeps arriving and waits), and continue it. Optional. */
  pausePlayback?(): Promise<void>;
  resumePlayback?(): Promise<void>;
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

/** Streams the answer sentence by sentence; rejects on failure; `signal` aborts it (barge-in). */
export type AnswerFn = (
  query: string, readerPara: number, speakerId: string, onSentence: (text: string) => void, signal?: AbortSignal,
) => Promise<void>;

export interface TalkDeps {
  session: TalkSession;
  reader: ReaderControl;
  retrieve: RetrieveFn;
  /** With `speaker`, replaces `retrieve` + `session.respond`: the backend writes the answer, we speak each sentence. */
  answer?: AnswerFn;
  speaker?: SentenceSpeaker;
  /** How long to wait for the first sentence before giving up. */
  firstSentenceTimeoutMs?: number;
  voiceFor: (speakerId: string) => string;
  onState?: (s: TalkState) => void;
  onToast?: (message: string) => void;
  onCaption?: (text: string, done: boolean) => void;
  onLatency?: (l: Latency) => void;
  /** Per-stage timing (Gemini first sentence here; TTS, avatar and audio start come from the speaker). */
  onTiming?: (e: TimingEvent) => void;
  /** The question as transcribed, so the UI can show it and a language badge. */
  onTranscript?: (text: string) => void;
  /** Runs a reader tool (#72) for the character: Realtime function calls, or a plain spoken command on the Gemini path. */
  tools?: ToolRunner;
  /** A tool ran: the chip to show in the chat ("Jumped to Chapter 3"). */
  onToolRun?: (chip: string) => void;
  /** True while answer audio is coming out of the speakers (drives the waveform). */
  onSpeaking?: (speaking: boolean) => void;
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
  private run: { abort: AbortController; timer: ReturnType<typeof setTimeout> | null } | null = null; // the Gemini answer in flight
  private forceResume = false; // a tool asked for reading to go on after the answer, whatever the auto-resume setting
  private off: () => void;

  constructor(deps: TalkDeps) {
    this.d = { now: () => Date.now(), autoResumeDelayMs: 1500, transcriptTimeoutMs: 2500, retrieveTimeoutMs: 8000, tapMs: 300, ...deps };
    this.off = deps.session.on((e) => this.onSession(e));
  }

  get state(): TalkState { return this._state; }

  private held = false;
  /** True while the live answer is paused (pauseAnswer). */
  get answerPaused(): boolean { return this.held; }

  /**
   * Pause the answer being spoken, keeping its place (#90): the sentence speaker holds its clip (Gemini path) or the
   * session holds its playback (Realtime). The answer stays the current turn; a question or Stop still cancels it.
   * False when nothing is being spoken or it cannot be held.
   */
  pauseAnswer(): boolean {
    if (this._state !== "answering" || this.held) return false;
    if (this.run && this.d.speaker?.pause) this.d.speaker.pause();
    else if (!this.run && this.d.session.pausePlayback) void this.d.session.pausePlayback().catch(() => {});
    else return false;
    this.held = true;
    this.setSpeaking(false);
    return true;
  }

  /** Continue a paused answer from where it was. False when there is none. */
  resumeAnswer(): boolean {
    if (!this.held) return false;
    this.held = false;
    if (this._state !== "answering") return false;
    if (this.run && this.d.speaker?.resume) this.d.speaker.resume(); // the speaker reports playing again (notePlaying)
    else if (!this.run && this.d.session.resumePlayback) { void this.d.session.resumePlayback().catch(() => {}); this.setSpeaking(true); }
    return true;
  }

  private speaking = false;
  private setSpeaking(v: boolean): void {
    if (v === this.speaking) return;
    this.speaking = v;
    this.d.onSpeaking?.(v);
  }

  /** The sentence audio started or stopped coming out of the speakers (drives the face and the waveform). */
  notePlaying(on: boolean): void { this.setSpeaking(on && this._state === "answering"); }

  private set(s: TalkState): void {
    if (s === this._state) return;
    this._state = s;
    if (s !== "answering") this.held = false;
    if (s !== "answering") this.setSpeaking(false);
    this.d.onState?.(s);
  }

  setAutoResume(v: boolean): void { this.autoResume = v; }

  private talkback = false;
  /** In the Talkback view a short tap on the mic never starts or stops the story; it just cancels the question. */
  setTalkback(v: boolean): void { this.talkback = v; }

  /** Picking someone else to talk to; applied to the session voice before the next question. */
  async setSpeaker(id: string): Promise<void> {
    this.speakerId = id;
    try { await this.d.session.setVoice(this.d.voiceFor(id)); } catch { /* the next ensureOpen reconnects with the right voice */ }
  }

  /** The resolved voice of the current speaker changed (Talk voice pick, cast edit, late cast load): re-apply it. */
  async refreshVoice(): Promise<void> {
    try { await this.d.session.setVoice(this.d.voiceFor(this.speakerId)); } catch { /* the next ensureOpen reconnects */ }
  }

  dispose(): void {
    this.off();
    this.clearResumeTimer();
    this.cancelAnswer();
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
    const viaAnswer = this.run !== null;
    this.cancelAnswer(); // barge-in: stop the clip, drop the queue, abort the stream

    if (this.resumeIndex === null) {
      this.wasPlayingBeforePress = this.d.reader.isPlaying();
      this.resumeIndex = this.d.reader.currentIndex(); // start of the current chunk = start of the sentence
    }
    if (this.d.reader.isPlaying()) this.d.reader.pause();
    this.d.onCaption?.("", false);
    this.set("listening");

    if ((wasAnswering || wasBusy) && !viaAnswer) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    if (turn !== this.turn) return;
    // connect and capture in parallel: the mic is buffered until the socket is open
    this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)).catch((e: unknown) => this.fail(turn, e));
    try { await this.d.session.startMic(); } catch (e) { this.fail(turn, e, micErrorMessage(e)); }
  }

  async pttUp(): Promise<void> {
    if (this._state !== "listening") return;
    const turn = this.turn;
    const held = this.d.now() - this.pressedAt;

    if (held < this.d.tapMs) { // a tap toggles play/pause instead of asking a question
      await this.d.session.stopMic(true);
      if (this.talkback) { this.resumeIndex = null; this.set("talk-idle"); return; }
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
    if (query) this.d.onTranscript?.(query);

    await this.answerQuery(turn, query);
  }

  /** A typed question: the same turn as a spoken one, without the mic. */
  async ask(text: string): Promise<void> {
    const query = text.trim();
    if (!query) return;
    this.clearResumeTimer();
    const turn = ++this.turn;
    const wasAnswering = this._state === "answering";
    const wasBusy = ["transcribing", "retrieving"].includes(this._state);
    const viaAnswer = this.run !== null;
    this.cancelAnswer();
    if (this.resumeIndex === null) {
      this.wasPlayingBeforePress = this.d.reader.isPlaying();
      this.resumeIndex = this.d.reader.currentIndex();
    }
    if (this.d.reader.isPlaying()) this.d.reader.pause();
    this.d.onCaption?.("", false);
    this.latency = {};
    this.d.onTranscript?.(query);
    this.set("retrieving");
    if ((wasAnswering || wasBusy) && !viaAnswer) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    if (turn !== this.turn) return;
    if (!(this.d.answer && this.d.speaker)) {
      try { await this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)); } catch (e) { this.fail(turn, e); return; }
      if (turn !== this.turn) return;
    }
    await this.answerQuery(turn, query);
  }

  /** Stop speaking and drop the current answer (Stop button). */
  async stop(): Promise<void> {
    const viaAnswer = this.run !== null;
    this.cancelAnswer();
    this.turn++;
    if (!viaAnswer && ["answering", "retrieving", "transcribing"].includes(this._state)) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    this.backToReading();
  }

  private async answerQuery(turn: number, query: string): Promise<void> {
    this.set("retrieving");
    const r0 = this.d.now();
    if (this.d.answer && this.d.speaker) {
      // Gemini writes the answer (no Realtime, so no function calling): a plain spoken command is acted on here instead
      const intent = this.d.tools ? parseIntent(query) : null;
      if (intent && this.d.tools) {
        const outcome = this.runTool(intent.name, intent.args);
        if (outcome.ok) { this.turn++; this.endTurn(); return; }
        this.d.onToast?.(outcome.output); // refused (e.g. a spoiler jump): tell the reader, no question is sent
        this.turn++;
        this.endTurn();
        return;
      }
      void this.streamAndSpeak(turn, query, r0);
      return;
    }
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

  /** Gemini path: sentences arrive on a stream and are spoken in order, the first while later ones are still coming. */
  private async streamAndSpeak(turn: number, query: string, r0: number): Promise<void> {
    const { answer, speaker } = this.d;
    if (!answer || !speaker) return;
    const run = { abort: new AbortController(), timer: null as ReturnType<typeof setTimeout> | null };
    this.run = run;
    const queue: string[] = [];
    let streamDone = false;
    let pumping = false;
    let delivered = 0;
    let streamError: string | null = null;
    const alive = () => turn === this.turn && this.run === run;
    const clearTimer = () => { if (run.timer) clearTimeout(run.timer); run.timer = null; };

    const finish = () => {
      if (!alive() || !streamDone || pumping || queue.length) return;
      clearTimer();
      this.run = null;
      if (streamError && delivered === 0) { this.d.onToast?.(`Could not look that up: ${streamError}. Continuing the story.`); this.backToReading(); return; }
      if (streamError) this.d.onToast?.(streamError);
      this.afterAnswer();
    };
    const prefetchAhead = () => queue.slice(0, PREFETCH_DEPTH).forEach((t, i) => speaker.prefetch(t, i));
    const pump = async () => {
      if (pumping) return;
      pumping = true;
      while (queue.length && alive()) {
        const text = queue.shift() as string;
        // "speaking" is not set here: the speaker reports it (notePlaying) when the audio really plays
        try {
          const playing = speaker.play(text);
          prefetchAhead(); // the next few sentences synthesise while this one plays
          await playing;
        } catch (e) {
          if (!alive()) return;
          pumping = false;
          this.cancelAnswer();
          this.d.onToast?.(e instanceof Error ? e.message : String(e));
          this.turn++;
          this.backToReading();
          return;
        }
      }
      pumping = false;
      finish();
    };

    run.timer = setTimeout(() => {
      if (!alive()) return;
      this.cancelAnswer();
      this.turn++;
      this.d.onToast?.("The answer took too long. Continuing the story.");
      this.backToReading();
    }, this.d.firstSentenceTimeoutMs ?? 15000);

    try {
      await answer(query, this.d.reader.currentPara(), this.speakerId, (text) => {
        if (!alive() || !text.trim()) return;
        if (delivered++ === 0) {
          clearTimer();
          this.latency.retrieveMs = this.d.now() - r0;
          this.d.onLatency?.({ ...this.latency });
          this.d.onTiming?.({ stage: "first-sentence", ms: this.latency.retrieveMs, sentence: text.slice(0, 40) });
          this.set("answering");
        }
        queue.push(text);
        if (pumping) prefetchAhead();
        void pump();
      }, run.abort.signal);
    } catch (e) {
      if (!alive()) return; // barge-in or a dead turn: the abort error is expected
      streamError = e instanceof Error ? e.message : String(e);
    }
    streamDone = true;
    finish();
  }

  /** Stop speaking, drop queued sentences and abort the stream. Safe to call when nothing is running. */
  private cancelAnswer(): void {
    this.held = false;
    const run = this.run;
    if (!run) return;
    this.run = null;
    if (run.timer) clearTimeout(run.timer);
    run.abort.abort();
    this.d.speaker?.stop();
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
    if (e.type === "tool-call" && ["retrieving", "answering"].includes(this._state) && !this.run) this.onToolCalls(e.calls);
    if (e.type === "audio-start" && this._state === "answering") this.setSpeaking(true);
    if (e.type === "playback-drained") this.setSpeaking(false);
    if (e.type === "error") {
      this.d.onToast?.(e.message);
      if (["listening", "transcribing", "retrieving", "answering"].includes(this._state)) { this.cancelAnswer(); this.turn++; this.backToReading(); }
    }
    if (e.type === "closed" && this._state === "answering" && !this.run) { this.turn++; this.backToReading(); }
    if (e.type === "playback-drained" && this._state === "answering") this.afterAnswer();
  }

  // ---- tools (#72) -----------------------------------------------------------------------------------------

  /** The model stopped to act: run each call in the reader, hand the results back, and let it go on speaking. */
  private onToolCalls(calls: ToolCall[]): void {
    const results: ToolResult[] = calls.map((c) => {
      const outcome = this.d.tools ? this.runTool(c.name, c.args) : { ok: false, output: "Tools are not available here.", chip: null };
      return { callId: c.callId, output: outcome.output };
    });
    this.set("answering"); // the spoken confirmation follows
    this.d.session.returnTools?.(results);
  }

  /** Run one tool and apply what it means for the loop: where reading continues (or that it does not) once the answer ends. */
  private runTool(name: string, args: Record<string, unknown>): ToolOutcome {
    let outcome: ToolOutcome;
    try { outcome = (this.d.tools as ToolRunner)(name, args); } catch (e) { outcome = { ok: false, output: `The tool failed: ${e instanceof Error ? e.message : String(e)}`, chip: null }; }
    if (outcome.chip) this.d.onToolRun?.(outcome.chip);
    if (!outcome.ok) return outcome;
    if (outcome.moveTo !== undefined) this.resumeIndex = outcome.moveTo; // reading goes on there, if it goes on at all
    if (outcome.play) { this.resumeIndex ??= this.d.reader.currentIndex(); this.wasPlayingBeforePress = true; this.forceResume = true; }
    if (outcome.pause) { this.wasPlayingBeforePress = false; this.forceResume = false; }
    return outcome;
  }

  /** The turn ends without a spoken answer (a command on the Gemini path): read on where the tool said, or wait. */
  private endTurn(): void {
    if (this.resumeIndex !== null && this.wasPlayingBeforePress) this.backToReading();
    else { this.resumeIndex = null; this.forceResume = false; this.set("talk-idle"); }
  }

  private afterAnswer(): void {
    if ((this.autoResume || this.forceResume) && this.resumeIndex !== null && this.wasPlayingBeforePress) {
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
    this.forceResume = false;
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
