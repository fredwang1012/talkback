import { micErrorMessage } from "../audio/mediaErrors";
/**
 * The core loop as an explicit state machine (brief 7.3):
 *   reading -> listening -> transcribing -> retrieving -> answering -> resuming -> reading   (+ talk-idle)
 * Push-to-talk, or hands-free (the local voice gate, or Boson's turn detection since #135). It talks to the network through `TalkSession` and to the reader through `ReaderControl`,
 * so the whole loop can be tested with fakes.
 */

import type { SentenceSpeaker } from "./sentencePlayer";
import type { TimingEvent } from "./speechGate";
import { HANDS_FREE_OVER_SPEECH, createVoiceGate } from "./handsFree";
import { transcriptionHint } from "./language";
import { QUIZ_QUESTIONS, endsQuiz, parseIntent, quizGeminiQuery, quizTurnInstructions, type Quiz, type ToolOutcome, type ToolRunner } from "./tools";

/**
 * #135: how long to wait for the transcript of a committed turn before saying "I didn't catch that". Boson never sends
 * transcription `.failed`; live the transcript comes 0.15-0.4 s after the commit, or not at all (e.g. a wrong
 * language hint), so a missing one is known well before the old 6 s window.
 */
export const FAST_FAIL_MS = 1500;
/**
 * #135: after the server ends a turn, speech that starts again within this window continues the same question (live,
 * the server VAD split one spoken question into two committed turns). The transcript takes about this long anyway.
 */
export const MERGE_MS = 600;

/** How many queued sentences are synthesised ahead of the one being spoken. */
export const PREFETCH_DEPTH = 3;

export type TalkState =
  | "reading" | "listening" | "transcribing" | "retrieving" | "answering" | "resuming" | "talk-idle";

/** A function call the model made (#72): ours to run, then answer with `returnTools`. */
export type ToolCall = { callId: string; name: string; args: Record<string, unknown> };
export type ToolResult = { callId: string; output: string };

export type SessionEvent =
  | { type: "transcript"; text: string; itemId?: string }
  | { type: "speech-started"; itemId?: string } // #135: Boson's turn detection heard speech begin (input_audio_buffer.speech_started)
  | { type: "speech-stopped" }
  | { type: "committed"; itemId?: string } // a user audio turn was committed (by us or by the server's VAD)
  | { type: "heard"; text: string } // #135: on a cut, the words of the answer the reader actually heard
  | { type: "turn-mode"; server: boolean } // #135: the server's turn detection was refused: back to the local gate
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
  /**
   * Start streaming mic audio; audio captured while the socket is still connecting is buffered then sent.
   * `voiceStarted`: the hands-free voice gate opened the turn, so the pre-roll from before it goes first (#114).
   * `overSpeech`: it opened while the character was speaking (a barge-in): only a short pre-roll, the rest is echo.
   */
  startMic(voiceStarted?: boolean, overSpeech?: boolean): Promise<void>;
  /** Stop the mic. With `discard`, drop what was captured instead of keeping it for a commit. */
  stopMic(discard?: boolean): Promise<void>;
  commit(): void;
  /** Ask for a spoken answer using per-response instructions. A typed question is given as `question` (#97). */
  respond(instructions: string, question?: string): void;
  /** Flush local playback, truncate the interrupted item at the played position and cancel the response. Rejects when the cancel is never acknowledged. */
  interrupt(): Promise<void>;
  /** Hand the results of a `tool-call` back and let the model go on speaking. Optional: a session without tools never asks. */
  returnTools?(results: ToolResult[]): void;
  /** Hands-free (#10): keep the mic open between turns (levels keep coming) without sending audio. Optional. */
  setMonitor?(on: boolean): Promise<void>;
  /** Hold local playback where it is (the reply keeps arriving and waits), and continue it. Optional. */
  pausePlayback?(): Promise<void>;
  resumePlayback?(): Promise<void>;
  /** #135: use Boson's turn detection for hands-free (the session reconnects when the mode changes). Optional. */
  setServerTurns?(on: boolean): void;
  /** #135: remove a turn from the server's conversation (it was the character's echo). Optional. */
  deleteItem?(itemId: string): void;
  /** #135: the transcription language hint (ISO 639-1), or null. Optional. */
  setLanguageHint?(code: string | null): void;
  /** Whether the socket is open now. Optional. */
  readonly isOpen?: boolean;
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
  /** The reader cut the answer being spoken off (barge-in, #10): mark its bubble; the next answer is told. */
  onInterrupted?: () => void;
  /** #135: hands-free uses Boson's turn detection when the session supports it (else, or when false, the local gate). */
  serverTurns?: boolean;
  /** #135: the cut-off answer's words that were actually heard (cut the stored reply to them). */
  onHeard?: (text: string) => void;
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
  /** A quiz in progress (quiz_me): the next turns carry its rules and text until the last answer is graded, or it is ended. */
  private quiz: (Quiz & { answered: number }) | null = null;
  /** The quiz answer being graded now: it counts only once that answer is complete (not cut off or cancelled). */
  private quizPending: { quiz: Quiz & { answered: number }; n: number } | null = null;

  /** End a quiz in progress (the chat was cleared, or reading went on). */
  endQuiz(): void { this.quiz = null; this.quizPending = null; }
  private turn = 0; // bumped whenever a turn is abandoned, so stale async work is ignored
  private resumeTimer: ReturnType<typeof setTimeout> | null = null;
  private waiters: { predicate: (e: SessionEvent) => boolean; resolve: (e: SessionEvent | null) => void }[] = [];
  private latency: Latency = {};
  private run: { abort: AbortController; timer: ReturnType<typeof setTimeout> | null } | null = null; // the Gemini answer in flight
  private forceResume = false; // a tool asked for reading to go on after the answer, whatever the auto-resume setting
  private handsFree = false; // #10: the voice gate starts and ends turns instead of the mic button
  private handsFreeTurn = false; // the current turn was started by the gate (never a tap)
  private gate = createVoiceGate();
  private opening: Promise<void> = Promise.resolve(); // the session connect started by the current turn (#102)
  private watch = 0; // the answer watchdog in force; an older one that wakes up does nothing
  private quietSince = Number.NEGATIVE_INFINITY; // when the character last stopped speaking (hands-free echo tail, #103)
  // #135 hands-free on Boson's turn detection
  private serverMode = false;
  private strongGate = createVoiceGate(HANDS_FREE_OVER_SPEECH); // the echo guard: clear speech over the character
  /** The question the server is hearing now: its transcripts so far, whether speech goes on, when it last stopped. */
  private srv: { transcripts: string[]; speaking: boolean; heardSpeech: boolean; stoppedAt: number; timer: ReturnType<typeof setTimeout> | null } | null = null;
  private pendingEcho: string | null = null; // a speech_started over the character, not yet confirmed by the local level
  private echoItems = new Set<string>(); // server turns that were judged echo: deleted on commit, transcripts ignored
  private recentQuestions: string[] = []; // for the transcription language hint
  private hint: string | null = null;
  private off: () => void;

  constructor(deps: TalkDeps) {
    this.d = { now: () => Date.now(), autoResumeDelayMs: 1500, transcriptTimeoutMs: 6000, retrieveTimeoutMs: 8000, tapMs: 300, ...deps };
    this.off = deps.session.on((e) => this.onSession(e));
  }

  /** The character's own voice is still in the room this long after playback ends: the strict gate profile still applies (#103). */
  static readonly ECHO_TAIL_MS = 300;

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
    if (!v) this.quietSince = this.d.now();
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
  setTalkback(v: boolean): void { if (v !== this.talkback) this.endQuiz(); this.talkback = v; }

  /** Picking someone else to talk to; applied to the session voice before the next question. */
  async setSpeaker(id: string): Promise<void> {
    if (id !== this.speakerId) this.endQuiz(); // a quiz belongs to the one who asked it
    this.speakerId = id;
    try { await this.d.session.setVoice(this.d.voiceFor(id)); } catch { /* the next ensureOpen reconnects with the right voice */ }
  }

  /** The resolved voice of the current speaker changed (Talk voice pick, cast edit, late cast load): re-apply it. */
  async refreshVoice(): Promise<void> {
    try { await this.d.session.setVoice(this.d.voiceFor(this.speakerId)); } catch { /* the next ensureOpen reconnects */ }
  }

  dispose(): void {
    this.clearSrv();
    this.endQuiz();
    this.off();
    this.clearResumeTimer();
    this.cancelAnswer();
    this.turn++;
    this.waiters.forEach((w) => w.resolve(null));
    this.waiters = [];
  }

  // ---- hands-free (#10) -----------------------------------------------------------------------------------

  get isHandsFree(): boolean { return this.handsFree; }

  /** Hands-free runs on Boson's turn detection now (#135); false: the local voice gate (or hands-free is off). */
  get serverTurnsActive(): boolean { return this.handsFree && this.serverMode; }

  /**
   * Turn hands-free on or off. Off by default. On Boson's turn detection (#135, `serverTurns`) the session is
   * reopened with semantic_vad at once and the mic streams to it; otherwise the mic stays open and `noteLevel` drives
   * the turns (the local gate, also the fallback when the server mode is refused).
   */
  async setHandsFree(on: boolean): Promise<void> {
    if (on === this.handsFree) return;
    this.handsFree = on;
    this.gate.reset();
    this.strongGate.reset();
    this.pendingEcho = null;
    const canServer = !!this.d.serverTurns && !!this.d.session.setServerTurns;
    this.serverMode = on && canServer;
    if (canServer) this.d.session.setServerTurns?.(this.serverMode);
    try { await this.d.session.setMonitor?.(on); } catch (e) {
      this.handsFree = false;
      if (this.serverMode) { this.serverMode = false; this.d.session.setServerTurns?.(false); }
      this.d.onToast?.(micErrorMessage(e));
      return;
    }
    if (this.serverMode) {
      try { await this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)); } catch (e) { this.d.onToast?.(e instanceof Error ? e.message : String(e)); }
    }
  }

  /**
   * A microphone level reading (hands-free only): speech starts a turn, silence after speech ends one.
   * While the character is speaking (and for a short tail after) the level is partly their own voice through the
   * speakers (#103), so a barge-in then needs the stricter profile: clearly louder than the echo, and sustained.
   * A blip or the echo itself never cuts them off; the mic button or Space always does.
   */
  noteLevel(rms: number): void {
    if (!this.handsFree) return;
    const now = this.d.now();
    if (this.serverMode) { // #135: the server decides the turns; the level is the echo guard (and wakes a closed session)
      this.strongGate.feed(rms, now);
      const idle = this.gate.feed(rms, now);
      if (this.pendingEcho !== null && this.strongGate.speaking) {
        this.pendingEcho = null; // confirmed: the reader is talking over the character
        if (["reading", "talk-idle", "answering", "resuming"].includes(this._state)) void this.beginTurn(true, true, true);
      }
      else if (idle === "start" && this.d.session.isOpen === false && ["reading", "talk-idle", "resuming"].includes(this._state)) {
        // the session closed (idle timeout): reopen; the mic audio held meanwhile goes first, so the server hears the question
        void this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)).catch(() => {});
      }
      return;
    }
    const overSpeech = this._state !== "listening" && (this.speaking || now - this.quietSince < TalkController.ECHO_TAIL_MS);
    const ev = this.gate.feed(rms, now, overSpeech ? HANDS_FREE_OVER_SPEECH : undefined);
    if (ev === "start" && ["reading", "talk-idle", "answering", "resuming"].includes(this._state)) void this.beginTurn(true, overSpeech);
    else if (ev === "stop" && this._state === "listening") { this.handsFreeTurn = true; void this.pttUp(); } // a pressed turn ends on silence too, and is never a tap
  }

  // ---- push to talk ---------------------------------------------------------------------------------------

  pttDown(): Promise<void> { return this.beginTurn(false); }

  /** `serverStarted` (#135): Boson's VAD opened the turn; the mic is already streaming to it. */
  private async beginTurn(handsFree: boolean, overSpeech = false, serverStarted = false): Promise<void> {
    this.handsFreeTurn = handsFree;
    this.clearSrv();
    this.pendingEcho = null;
    if (this.serverMode) this.srv = { transcripts: [], speaking: serverStarted, heardSpeech: serverStarted, stoppedAt: 0, timer: null };
    this.clearResumeTimer(); // pressing during the 1.5 s window starts a follow-up question
    const turn = ++this.turn;
    this.pressedAt = this.d.now();
    const wasAnswering = this._state === "answering";
    this.quizPending = null; // a barge-in: the answer being graded was cut off and does not count
    if (this.d.reader.isPlaying()) this.endQuiz(); // the reader read on since the quiz: this is not an answer to it
    const wasBusy = ["transcribing", "retrieving"].includes(this._state);
    const viaAnswer = this.run !== null;
    if (wasAnswering) this.d.onInterrupted?.(); // barge-in (#10): the bubble shows it was cut off, the next answer is told
    this.cancelAnswer(); // barge-in: stop the clip, drop the queue, abort the stream

    if (this.resumeIndex === null) {
      this.wasPlayingBeforePress = this.d.reader.isPlaying();
      this.resumeIndex = this.d.reader.currentIndex(); // start of the current chunk = start of the sentence
    }
    if (this.d.reader.isPlaying()) this.d.reader.pause();
    this.d.onCaption?.("", false);
    this.set("listening");

    // Cancel, connect and capture all at once (#100, #99): the mic starts now so the first words over the old answer
    // are kept; the session holds that audio and the commit until the cancel is acknowledged and the socket is open.
    // pttUp waits for both before it starts the transcript window. A cancel that is never acknowledged ends the turn.
    const interrupting = (wasAnswering || wasBusy) && !viaAnswer
      ? this.d.session.interrupt().catch((e: unknown) => this.fail(turn, e))
      : Promise.resolve();
    const connecting = this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)).catch((e: unknown) => this.fail(turn, e));
    this.opening = Promise.all([interrupting, connecting]).then(() => undefined);
    if (serverStarted) return; // the server already has the audio
    try { await this.d.session.startMic(handsFree, handsFree && overSpeech); } catch (e) { this.fail(turn, e, micErrorMessage(e)); }
  }

  async pttUp(): Promise<void> {
    if (this._state !== "listening") return;
    const turn = this.turn;
    const held = this.d.now() - this.pressedAt;
    const tap = held < this.d.tapMs && !this.handsFreeTurn; // the gate never taps
    this.handsFreeTurn = false;

    if (tap) { // a tap toggles play/pause instead of asking a question
      await this.d.session.stopMic(true);
      if (this.talkback) { this.resumeIndex = null; this.set("talk-idle"); return; }
      const wasPlaying = this.wasPlayingBeforePress;
      const index = this.resumeIndex;
      this.resumeIndex = null;
      this.set("reading");
      if (!wasPlaying && index !== null) { this.endQuiz(); this.d.reader.play(); } // reading on ends a quiz
      return;
    }

    await this.d.session.stopMic();
    if (turn !== this.turn) return;
    const srv = this.srv;
    if (srv) { // #135 a pressed turn in server mode: the server ends it; nothing heard yet means we commit it ourselves
      if (srv.speaking || srv.transcripts.length || srv.stoppedAt) return;
      await this.opening;
      if (turn !== this.turn || this.srv !== srv) return;
      this.d.session.commit();
      srv.stoppedAt = this.d.now();
      this.set("transcribing");
      this.armSrv(FAST_FAIL_MS);
      return;
    }
    this.set("transcribing");
    // The first press often releases before the socket is open (key fetch + connect take seconds). The commit is
    // queued until then, so the transcript window must start when it is really sent, not at the release (#102).
    await this.opening; // a failed connect already ended this turn through fail()
    if (turn !== this.turn) return;
    const t0 = this.d.now();
    this.d.session.commit();
    const transcript = await this.waitTranscript();
    if (turn !== this.turn) return;
    const query = transcript && transcript.type === "transcript" ? transcript.text.trim() : "";
    this.latency = { commitToTranscriptMs: this.d.now() - t0 };
    if (!query) { this.noTranscript(); return; } // nothing heard (silence, a failed or late transcription): never ask an empty question
    this.d.onTranscript?.(query);
    this.noteQuestion(query);

    await this.answerQuery(turn, query);
  }

  /**
   * The transcript of the turn just committed (#135): the server's `committed` arrives at once, and the transcript
   * 0.15-0.4 s later or never (Boson sends no `.failed`), so after a commit only FAST_FAIL_MS is waited. Without a
   * committed event (older servers) the full window applies.
   */
  private async waitTranscript(): Promise<SessionEvent | null> {
    const first = await this.waitFor((e) => e.type === "transcript" || e.type === "committed", this.d.transcriptTimeoutMs);
    if (first?.type !== "committed") return first;
    return this.waitFor((e) => e.type === "transcript", FAST_FAIL_MS);
  }

  /** Nothing usable was heard: say so now, and drop a language hint that may be the cause (#135). */
  private noTranscript(): void {
    this.d.onToast?.("I didn't catch that. Try again.");
    this.turn++;
    this.clearSrv();
    this.backToReading();
    if (this.hint !== null) { // live: a wrong hint swallowed the transcript entirely
      this.recentQuestions = [];
      this.hint = null;
      this.d.session.setLanguageHint?.(null);
    }
  }

  /** A question was asked (spoken or typed): update the transcription language hint from the recent ones (#135). */
  private noteQuestion(query: string): void {
    this.recentQuestions = [...this.recentQuestions, query].slice(-3);
    const hint = transcriptionHint(this.recentQuestions);
    if (hint === this.hint) return;
    this.hint = hint;
    this.d.session.setLanguageHint?.(hint);
  }

  // ---- Boson turn detection (#135) ----------------------------------------------------------------------------

  private clearSrv(): void {
    if (this.srv?.timer) clearTimeout(this.srv.timer);
    this.srv = null;
  }

  private armSrv(ms: number): void {
    const srv = this.srv;
    if (!srv) return;
    if (srv.timer) clearTimeout(srv.timer);
    srv.timer = setTimeout(() => { if (this.srv === srv) { srv.timer = null; void this.finishSrv(); } }, ms);
  }

  private markEcho(itemId: string | undefined): void {
    if (!itemId) return;
    if (this.echoItems.size > 50) this.echoItems.clear();
    this.echoItems.add(itemId);
  }

  /** speech_started: a new question, more of the current one, a barge-in, or (over the character) maybe their echo. */
  private onServerSpeech(itemId: string | undefined): void {
    const srv = this.srv;
    if (srv && (this._state === "listening" || this._state === "transcribing")) { // the same question goes on
      if (srv.timer) { clearTimeout(srv.timer); srv.timer = null; }
      srv.speaking = true;
      srv.heardSpeech = true;
      if (this._state === "transcribing") this.set("listening");
      return;
    }
    if (!["reading", "talk-idle", "answering", "resuming"].includes(this._state)) { this.markEcho(itemId); return; } // busy: not a question we answer
    const overSpeech = this.speaking || this.d.now() - this.quietSince < TalkController.ECHO_TAIL_MS;
    // Echo guard: over the character (speakers), the server's VAD alone may be hearing them. A barge-in needs clear
    // local speech too (the strict gate, #103); until then this speech is on probation (noteLevel may confirm it).
    if (overSpeech && !this.strongGate.speaking) { this.pendingEcho = itemId ?? ""; return; }
    void this.beginTurn(true, overSpeech, true);
  }

  private onServerStop(): void {
    if (this.pendingEcho !== null) { this.markEcho(this.pendingEcho); this.pendingEcho = null; return; } // never confirmed: echo
    const srv = this.srv;
    if (!srv || this._state !== "listening") return;
    srv.speaking = false;
    srv.stoppedAt = this.d.now();
    this.set("transcribing");
    this.armSrv(FAST_FAIL_MS);
  }

  private onCommitted(itemId: string | undefined): void {
    if (itemId && this.echoItems.has(itemId)) { this.d.session.deleteItem?.(itemId); return; }
    const srv = this.srv;
    if (srv && this._state === "transcribing" && !srv.speaking && srv.transcripts.length === 0) this.armSrv(FAST_FAIL_MS); // the transcript is due shortly
  }

  private onServerTranscript(text: string, itemId: string | undefined): void {
    if (itemId && this.echoItems.has(itemId)) return;
    const srv = this.srv;
    if (!srv || !["listening", "transcribing"].includes(this._state)) return;
    if (text.trim()) srv.transcripts.push(text.trim());
    if (srv.speaking) return; // more of the question is coming
    this.armSrv(Math.max(0, srv.stoppedAt + MERGE_MS - this.d.now()));
  }

  /** The server's turn is over: answer it through the normal flow (retrieval instructions, then our response.create). */
  private async finishSrv(): Promise<void> {
    const srv = this.srv;
    if (!srv) return;
    this.clearSrv();
    const turn = this.turn;
    const query = srv.transcripts.join(" ").trim();
    if (!query) { this.noTranscript(); return; }
    this.latency = { commitToTranscriptMs: this.d.now() - srv.stoppedAt };
    this.d.onTranscript?.(query);
    this.noteQuestion(query);
    await this.opening; // a barge-in's cancel must be acknowledged before our response.create
    if (turn !== this.turn) return;
    await this.answerQuery(turn, query);
  }

  /** A typed question: the same turn as a spoken one, without the mic. */
  async ask(text: string): Promise<void> {
    const query = text.trim();
    if (!query) return;
    this.clearSrv();
    this.clearResumeTimer();
    const turn = ++this.turn;
    const wasAnswering = this._state === "answering";
    const wasBusy = ["transcribing", "retrieving"].includes(this._state);
    const viaAnswer = this.run !== null;
    if (wasAnswering) this.d.onInterrupted?.();
    this.cancelAnswer();
    if (this.resumeIndex === null) {
      this.wasPlayingBeforePress = this.d.reader.isPlaying();
      this.resumeIndex = this.d.reader.currentIndex();
    }
    if (this.d.reader.isPlaying()) this.d.reader.pause();
    this.d.onCaption?.("", false);
    this.latency = {};
    this.d.onTranscript?.(query);
    this.noteQuestion(query);
    this.set("retrieving");
    if ((wasAnswering || wasBusy) && !viaAnswer) {
      try { await this.d.session.interrupt(); } catch (e) { this.fail(turn, e); return; }
    }
    if (turn !== this.turn) return;
    if (!(this.d.answer && this.d.speaker)) {
      try { await this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)); } catch (e) { this.fail(turn, e); return; }
      if (turn !== this.turn) return;
    }
    await this.answerQuery(turn, query, true);
  }

  /** Stop speaking and drop the current answer (Stop button). */
  async stop(): Promise<void> {
    this.clearSrv();
    const viaAnswer = this.run !== null;
    this.cancelAnswer();
    this.turn++;
    if (!viaAnswer && ["answering", "retrieving", "transcribing"].includes(this._state)) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    this.backToReading();
  }

  /** `typed`: the question is text, so the Realtime model must be given it as an item (#97); spoken audio is already the user item. */
  /** The quiz part of this turn (and the answer it grades), or null when no quiz runs. Ends the quiz on its last answer or on a stop / command. */
  private quizTurn(query: string): { quiz: Quiz; n: number } | null {
    const q = this.quiz;
    this.quizPending = null;
    if (!q) return null;
    if (endsQuiz(query)) { this.endQuiz(); return null; }
    const n = q.answered + 1; // counted when this answer completes (afterAnswer), so a cut-off one is asked again
    this.quizPending = { quiz: q, n };
    return { quiz: q, n };
  }

  private async answerQuery(turn: number, query: string, typed = false): Promise<void> {
    this.set("retrieving");
    const r0 = this.d.now();
    const quiz = this.quizTurn(query);
    if (this.d.answer && this.d.speaker) {
      // Gemini writes the answer (no Realtime, so no function calling): a plain spoken command is acted on here instead
      const intent = this.d.tools ? parseIntent(query) : null;
      if (intent && this.d.tools) {
        const outcome = this.runTool(intent.name, intent.args);
        if (outcome.ok && outcome.ask) { void this.streamAndSpeak(turn, outcome.ask, r0); return; } // quiz / explain: Gemini answers it aloud
        if (outcome.ok) { this.turn++; this.endTurn(); return; }
        this.d.onToast?.(outcome.output); // refused (e.g. a spoiler jump): tell the reader, no question is sent
        this.turn++;
        this.endTurn();
        return;
      }
      void this.streamAndSpeak(turn, quiz ? quizGeminiQuery(quiz.quiz, quiz.n, query) : query, r0);
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
    if (quiz) instructions = `${instructions}\n\n${quizTurnInstructions(quiz.quiz, quiz.n)}`;
    this.d.session.respond(instructions, typed ? query : undefined);
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

  /**
   * Waits for the answer to start (audio), end without audio, or fail; after 15 s of nothing it ends the turn itself.
   * Armed by `respond` and again by every `returnTools` (#102): a tool call's continuation is a new response that can
   * go missing too, and until it speaks the status would otherwise say "Thinking" for ever.
   */
  private async awaitAnswer(turn: number, a0: number): Promise<void> {
    const watch = ++this.watch;
    const first = await this.waitFor((e) => e.type === "audio-start" || e.type === "response-done" || e.type === "error", 15000);
    if (turn !== this.turn || watch !== this.watch) return;
    if (first?.type === "audio-start") {
      this.latency.firstAudioMs ??= this.d.now() - a0;
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
    if (e.type === "heard") this.d.onHeard?.(e.text);
    if (e.type === "turn-mode" && !e.server && this.serverMode) { // refused: the local gate takes over hands-free
      this.serverMode = false;
      this.gate.reset();
      this.clearSrv();
      this.pendingEcho = null;
      if (["listening", "transcribing"].includes(this._state)) { this.turn++; this.backToReading(); }
    }
    if (this.serverMode) {
      if (e.type === "speech-started") this.onServerSpeech(e.itemId);
      else if (e.type === "speech-stopped") this.onServerStop();
      else if (e.type === "committed") this.onCommitted(e.itemId);
      else if (e.type === "transcript") this.onServerTranscript(e.text, e.itemId);
    }
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
    void this.awaitAnswer(this.turn, this.d.now()); // the continuation gets its own watchdog
  }

  /** Run one tool and apply what it means for the loop: where reading continues (or that it does not) once the answer ends. */
  private runTool(name: string, args: Record<string, unknown>): ToolOutcome {
    let outcome: ToolOutcome;
    try { outcome = (this.d.tools as ToolRunner)(name, args); } catch (e) { outcome = { ok: false, output: `The tool failed: ${e instanceof Error ? e.message : String(e)}`, chip: null }; }
    if (outcome.chip) this.d.onToolRun?.(outcome.chip);
    this.quiz = outcome.ok && outcome.quiz ? { ...outcome.quiz, answered: 0 } : null; // quiz_me starts one; any other tool ends it
    this.quizPending = null;
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
    const p = this.quizPending;
    this.quizPending = null;
    if (p && this.quiz === p.quiz) { // the graded answer is complete: it counts; the last one ends the quiz
      p.quiz.answered = p.n;
      if (p.n >= QUIZ_QUESTIONS) this.quiz = null;
    }
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
    this.quizPending = null; // the turn ended without a complete answer (or reading goes on): it does not count
    this.resumeIndex = null;
    this.forceResume = false;
    this.clearResumeTimer();
    if (index !== null && wasPlaying) {
      this.endQuiz(); // reading goes on: a quiz in progress is over
      this.set("reading");
      this.d.reader.resumeAt(index);
    } else {
      this.set(index !== null ? "talk-idle" : "reading"); // Talk mode without reading: stay idle, never start playing
    }
  }

  private fail(turn: number, e: unknown, message?: string): void {
    if (turn !== this.turn) return;
    this.clearSrv();
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
