import { micErrorMessage } from "../audio/mediaErrors";
/**
 * The core loop as an explicit state machine (brief 7.3):
 *   reading -> listening -> transcribing -> retrieving -> answering -> resuming -> reading   (+ talk-idle)
 * Push-to-talk, or hands-free (the local voice gate, or Boson's turn detection since #135). It talks to the network through `TalkSession` and to the reader through `ReaderControl`,
 * so the whole loop can be tested with fakes.
 */

import type { SentenceSpeaker } from "./sentencePlayer";
import type { TimingEvent } from "./speechGate";
import { HANDS_FREE, HANDS_FREE_OVER_SPEECH, createEchoFloor, createVoiceGate } from "./handsFree";
import { PRESS_PREROLL_MS } from "./TalkClient";
import { transcriptionHint } from "./language";
import { QUIZ_QUESTIONS, endsQuiz, parseIntent, quizGeminiQuery, quizTurnInstructions, type Quiz, type ToolOutcome, type ToolRunner } from "./tools";

/**
 * #135: how long to wait for the transcript of a server turn before saying "I didn't catch that". Boson never sends
 * transcription `.failed`; live the transcript of a 2 s question came 0.15-0.4 s after the commit, or not at all (e.g.
 * a wrong language hint). The first 1.5 s was too short for longer turns in real use (the message showed, then the
 * transcript arrived), so the wait is 2.5 s plus a quarter of the turn's length, at most 5 s.
 */
export const FAIL_WAIT_MS = 2500;
export function failWaitMs(turnMs: number): number {
  return Math.min(5000, FAIL_WAIT_MS + Math.max(0, turnMs) / 4);
}
/** A server turn with less speech than this (by the local level) is a click, a breath or a noise: it never says
 * "I didn't catch that", it is dropped quietly (and removed from the conversation). */
export const MIN_TURN_MS = 400;
/**
 * Server turns: the mic is held (nothing sent) while the character speaks and this long after its playback ends, so the
 * server never hears our own voice (#103 did the same for the local gate). A barge-in the local gate hears releases it.
 */
export const MIC_HOLD_TAIL_MS = 600;
/** A barge-in the local gate heard but the server never confirmed (no speech_started) ends quietly after this long. */
export const BARGE_CONFIRM_MS = 2500;
/** This many real server turns in a row without a transcript: hands-free falls back to the local voice gate. */
export const EMPTY_TURNS_FALLBACK = 2;
/**
 * #135: after the server ends a turn, speech that starts again within this window continues the same question (live,
 * the server VAD split one spoken question into two committed turns). The transcript takes about this long anyway.
 */
export const MERGE_MS = 600;

/** How many queued sentences are synthesised ahead of the one being spoken. */
export const PREFETCH_DEPTH = 3;

/** #79 group scene: at most this many characters answer one question (each answer is a separate, paid answer). */
export const MAX_GROUP = 3;

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
  | { type: "searching"; active: boolean; completed?: boolean } // B2: Boson's server-side web_search runs (active), then the answer it started arrives (completed) or the turn was cut
  | { type: "sources"; sources: { uri: string; title: string }[] } // B2: links out of the web_search result
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
  /** Ask for an answer under per-response instructions; `question` (typed, or the transcript) goes with it as response.input (#97). */
  respond(instructions: string, question?: string): void;
  /** Flush local playback, truncate the interrupted item at the played position and cancel the response. Rejects when the cancel is never acknowledged. */
  interrupt(): Promise<void>;
  /** Hand the results of a `tool-call` back and let the model go on speaking. Optional: a session without tools never asks. */
  returnTools?(results: ToolResult[]): void;
  /** Hands-free (#10): keep the mic open between turns (levels keep coming) without sending audio. Optional. */
  setMonitor?(on: boolean): Promise<void>;
  /** Hold local playback where it is (the reply keeps arriving and waits), and continue it. Optional. */
  pausePlayback?(): Promise<void>;
  /** False while the reply cannot be held (a live face, #140: the avatar protocol has no pause). Absent = it can. */
  canPause?(): boolean;
  resumePlayback?(): Promise<void>;
  /** #135: use Boson's turn detection for hands-free (the session reconnects when the mode changes). Optional. */
  setServerTurns?(on: boolean): void;
  /** #135: remove a turn from the server's conversation (it was the character's echo). Optional. */
  deleteItem?(itemId: string): void;
  /**
   * Empty the server's input audio buffer. Live (probe 2026-10-24): with `create_response: false` the server never
   * clears it itself; every committed turn reuses one item id and is transcribed with all the audio since the last
   * clear, so a hands-free turn must end with this or the next question carries every earlier one. Optional.
   */
  clearInput?(): void;
  /** Server turns: hold the mic (nothing sent) while our own playback could be heard; release it with a pre-roll
   * (ms of the held audio sent first, a barge-in) or without (the held audio is dropped). Optional. */
  setMicHold?(hold: boolean, preRollMs?: number): void;
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
  /** B2: Boson's server-side web_search is running (true), or finished (false; `completed` when its answer followed). */
  onSearching?: (active: boolean, completed: boolean) => void;
  /** B2: the links a web_search result carried, for the reply bubble. */
  onSources?: (sources: { uri: string; title: string }[]) => void;
  /** #135: hands-free uses Boson's turn detection when the session supports it (else, or when false, the local gate). */
  serverTurns?: boolean;
  /** #135: the cut-off answer's words that were actually heard (cut the stored reply to them). */
  onHeard?: (text: string) => void;
  /** True while answer audio is coming out of the speakers (drives the waveform). */
  onSpeaking?: (speaking: boolean) => void;
  /**
   * #34: who a question is addressed to (a character the reader may talk to now), or null to keep the current
   * speaker. Asked once per question, before it is shown or answered; never during a quiz (the reply is an answer).
   */
  route?: (query: string) => string | null;
  /** #34: a question was addressed to someone else: they answer it (and the questions after it). */
  onRoute?: (speakerId: string) => void;
  /**
   * #34: hands-free while the book is read aloud. The reading voice through the speakers is treated like the
   * character's: on Boson's turn detection the mic is held while it plays (the server must not take the book for a
   * question) and a barge-in needs clear speech over its echo, as over an answer. Off: only answers count as echo.
   */
  readingIsEcho?: boolean;
  /** #79: the next character of a group round starts answering (`index` >= 1; the first is the one talked to). */
  onRoundTurn?: (speakerId: string, index: number) => void;
  now?: () => number;
  autoResumeDelayMs?: number;
  transcriptTimeoutMs?: number;
  retrieveTimeoutMs?: number;
  tapMs?: number;
}

/**
 * The transcripts of one spoken question so far, with the latest one. Live, a server-detected turn is transcribed with
 * the whole input buffer (everything since our last clear), so when the server splits a question in two, the second
 * transcript repeats the first: it then replaces what was heard; a transcript of only the new words is appended.
 */
export function mergeTranscript(sofar: string[], text: string): string[] {
  const t = text.trim();
  if (!t) return sofar;
  const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  const prev = norm(sofar.join(" "));
  if (prev && norm(t).startsWith(prev)) return [t];
  return [...sofar, t];
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
  private strongGate = createVoiceGate(HANDS_FREE_OVER_SPEECH); // the barge-in gate: clear speech over the character
  private echo = createEchoFloor(); // the character's own voice as the mic hears it: barge-in must be well over it
  /** The question the server is hearing now: its transcripts so far, whether speech goes on, when it last stopped,
   * how much speech the local level heard in it, and its server items (removed if the turn is dropped). */
  private srv: {
    transcripts: string[]; speaking: boolean; heardSpeech: boolean; stoppedAt: number; startedAt: number;
    voicedMs: number; items: string[]; pressed: boolean; timer: ReturnType<typeof setTimeout> | null;
  } | null = null;
  private micHeld = false; // server turns: the mic is held (the character speaks, or its tail)
  private bargeOpen = false; // the reader cut in (or pressed): the mic stays open until the character speaks again
  private holdTimer: ReturnType<typeof setTimeout> | null = null;
  private lastLevelAt: number | null = null;
  private voiced: { t: number; ms: number }[] = []; // the reader's speech by the local level, last 2 s (not while held)
  private emptyTurns = 0; // real server turns in a row without a transcript
  private serverGaveUp = false; // fell back to the local gate after EMPTY_TURNS_FALLBACK of them
  private recentQuestions: string[] = []; // for the transcription language hint
  private group: string[] = []; // #79: the other characters every question also goes to, in order
  // #79: the group round of the current question: who answers, who answers now (`at`), and whether the session voice
  // was switched for it. Tied to its turn: a barge-in, a new question or Stop bumps the turn and the rest never answer.
  private round: { turn: number; query: string; speakers: string[]; at: number; voiced: boolean } | null = null;
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
    if (this.run && this.d.speaker?.pause && this.d.speaker.canPause !== false) this.d.speaker.pause();
    else if (!this.run && this.d.session.pausePlayback && this.d.session.canPause?.() !== false) void this.d.session.pausePlayback().catch(() => {});
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
    if (v) this.bargeOpen = false; // the character speaks (again): hold the mic again
    if (!v) this.quietSince = this.d.now();
    this.updateMicHold();
    this.d.onSpeaking?.(v);
  }

  /**
   * Server turns: the server must never hear the character (user report: it heard itself, cut itself off and made
   * questions of its own words). The mic is held while it speaks and MIC_HOLD_TAIL_MS after, unless the reader cut in.
   */
  private updateMicHold(): void {
    if (this.holdTimer) { clearTimeout(this.holdTimer); this.holdTimer = null; }
    const now = this.d.now();
    const tail = this.quietSince + TalkController.MIC_HOLD_TAIL - now;
    const want = this.handsFree && this.serverMode && !this.bargeOpen && (this.speaking || this.holdForReading || tail > 0);
    // only a tail ends by itself: a hold for the reading ends when the reading stops (noteLevel), not on a timer
    if (want && !this.speaking && tail > 0) this.holdTimer = setTimeout(() => { this.holdTimer = null; this.updateMicHold(); }, tail);
    this.applyHold(want);
  }

  private applyHold(on: boolean, preRollMs = 0): void {
    if (on === this.micHeld) return;
    this.micHeld = on;
    if (on) this.strongGate.reset();
    this.d.session.setMicHold?.(on, preRollMs);
  }

  private static readonly MIC_HOLD_TAIL = MIC_HOLD_TAIL_MS;

  private holdForReading = false; // #34 server turns: the mic is held because the book is being read aloud
  /** #34: the book is being read aloud right now, and that counts as echo (`readingIsEcho`). */
  private readingAloud(): boolean {
    return !!this.d.readingIsEcho && this.handsFree && this._state === "reading" && this.d.reader.isPlaying();
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

  /**
   * #34: a question addressed to another character goes to them: the speaker changes before the question is shown
   * (so its bubble, voice, face and memory are theirs). True when it changed; the Realtime session's voice is then
   * switched by `routedVoice` once nothing is being cancelled (the Gemini path voices each sentence itself).
   */
  private routeQuestion(query: string): boolean {
    // during a quiz the reply is an answer, and an answer may well be a name ("The Knave of Hearts."): never routed
    if (this.quiz) return false;
    const id = this.d.route?.(query) ?? null;
    if (!id || id === this.speakerId) return false;
    this.endQuiz(); // a quiz belongs to the one who asked it
    this.speakerId = id;
    this.d.onRoute?.(id);
    return true;
  }

  /** The routed speaker's voice on the Realtime session (never rejects: the next ensureOpen reconnects anyway). */
  private async routedVoice(changed: boolean): Promise<void> {
    if (!changed || (this.d.answer && this.d.speaker)) return;
    try { await this.d.session.setVoice(this.d.voiceFor(this.speakerId)); } catch { /* reconnects on the next open */ }
  }

  /**
   * #79 group scene: every question also goes to these characters, one after the other in this order, after the one
   * talked to (at most MAX_GROUP in all; duplicates and the one talked to are dropped). [] = a normal one-to-one chat.
   */
  setGroup(ids: string[]): void {
    this.group = [...new Set(ids)].filter(Boolean).slice(0, MAX_GROUP); // startRound drops the one talked to and caps the round
  }

  /** Who answers now: the current character of a group round, else the one talked to. */
  get answerSpeaker(): string {
    const r = this.liveRound();
    return r ? r.speakers[r.at] : this.speakerId;
  }

  private liveRound() {
    return this.round && this.round.turn === this.turn ? this.round : null;
  }

  /** The resolved voice of the current speaker changed (Talk voice pick, cast edit, late cast load): re-apply it. */
  async refreshVoice(): Promise<void> {
    try { await this.d.session.setVoice(this.d.voiceFor(this.speakerId)); } catch { /* the next ensureOpen reconnects */ }
  }

  dispose(): void {
    this.clearSrv();
    if (this.holdTimer) clearTimeout(this.holdTimer);
    this.holdTimer = null;
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
    this.echo.reset();
    this.bargeOpen = false;
    this.holdForReading = false;
    this.emptyTurns = 0;
    if (on) this.serverGaveUp = false; // a fresh hands-free session gives Boson's turn detection another chance
    if (!on) this.applyHold(false);
    const canServer = !!this.d.serverTurns && !!this.d.session.setServerTurns && !this.serverGaveUp;
    this.serverMode = on && canServer;
    if (canServer) this.d.session.setServerTurns?.(this.serverMode);
    try { await this.d.session.setMonitor?.(on); } catch (e) {
      this.handsFree = false;
      this.applyHold(false);
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
    const dt = this.lastLevelAt === null ? 0 : Math.min(200, Math.max(0, now - this.lastLevelAt));
    this.lastLevelAt = now;
    const reading = this.readingAloud();
    // what the mic hears of the character (or of the book read aloud, #34): barge-in must be well over it
    if (this.speaking || reading) this.echo.feed(rms, now);
    if (this.serverMode) { // #135: the server decides the turns; the level detects barge-in (and wakes a closed session)
      if (reading !== this.holdForReading) { // the book started or stopped being read aloud: hold the mic, or release it
        this.holdForReading = reading;
        if (reading) this.bargeOpen = false;
        this.updateMicHold();
      }
      if (this.micHeld) { // the character speaks (or its tail): the server hears nothing; clear speech over it cuts in
        const ev = this.strongGate.feed(rms, now, this.echo.profile());
        if (ev === "start" && ["reading", "talk-idle", "answering", "resuming"].includes(this._state)) this.bargeIn();
        return;
      }
      if (rms >= HANDS_FREE.threshold) { // the reader's speech, for telling a real turn from a click or a breath
        this.voiced.push({ t: now, ms: dt });
        if (this.srv) this.srv.voicedMs += dt;
      }
      while (this.voiced.length && now - this.voiced[0].t > 2000) this.voiced.shift();
      const idle = this.gate.feed(rms, now);
      if (idle === "start" && this.d.session.isOpen === false && ["reading", "talk-idle", "resuming"].includes(this._state)) {
        // the session closed (idle timeout): reopen; the mic audio held meanwhile goes first, so the server hears the question
        void this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)).catch(() => {});
      }
      return;
    }
    const overSpeech = this._state !== "listening" && (this.speaking || reading || now - this.quietSince < TalkController.ECHO_TAIL_MS);
    const ev = this.gate.feed(rms, now, overSpeech ? this.echo.profile() : undefined);
    if (ev === "start" && ["reading", "talk-idle", "answering", "resuming"].includes(this._state)) void this.beginTurn(true, overSpeech);
    else if (ev === "stop" && this._state === "listening") { this.handsFreeTurn = true; void this.pttUp(); } // a pressed turn ends on silence too, and is never a tap
  }

  // ---- push to talk ---------------------------------------------------------------------------------------

  pttDown(): Promise<void> { return this.beginTurn(false); }

  /** Server turns: the local gate heard the reader over the character. Cut it off now, and let the server hear them
   * (the held audio's last BARGE_PREROLL_MS first); the server's speech_started then continues this turn. */
  private bargeIn(): void {
    void this.beginTurn(true, true, true, true);
  }

  /** Speech the local level heard in the last `ms` (before the server's speech_started arrived). */
  private recentVoiced(ms: number): number {
    const now = this.d.now();
    return this.voiced.filter((v) => now - v.t <= ms).reduce((a, v) => a + v.ms, 0);
  }

  /**
   * `serverStarted` (#135): Boson's VAD opened the turn; the mic is already streaming to it. `awaitServer`: a barge-in
   * the local gate heard; the server has not reported speech yet (if it never does, the turn ends quietly).
   */
  private async beginTurn(handsFree: boolean, overSpeech = false, serverStarted = false, awaitServer = false, itemId?: string): Promise<void> {
    this.handsFreeTurn = handsFree;
    this.clearSrv();
    if (this.serverMode) {
      this.bargeOpen = true; // a turn is starting: the server must hear it, whatever is playing
      // a barge-in: the speech began about minSpeechMs before the gate opened; keep that much, little echo comes along
      this.applyHold(false, awaitServer ? HANDS_FREE_OVER_SPEECH.minSpeechMs + 50 : serverStarted ? 0 : PRESS_PREROLL_MS);
      const heard = serverStarted && !awaitServer;
      this.srv = {
        transcripts: [], speaking: heard, heardSpeech: heard, stoppedAt: 0, startedAt: this.d.now(),
        voicedMs: awaitServer ? HANDS_FREE_OVER_SPEECH.minSpeechMs : this.recentVoiced(1500), items: itemId ? [itemId] : [], pressed: !handsFree, timer: null,
      };
    }
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
    if (awaitServer) this.armSrv(BARGE_CONFIRM_MS);

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
      srv.voicedMs = Math.max(srv.voicedMs, held); // a pressed turn is the reader's on purpose: always a real one
      this.set("transcribing");
      this.armSrv(failWaitMs(held));
      return;
    }
    this.set("transcribing");
    // The first press often releases before the socket is open (key fetch + connect take seconds). The commit is
    // queued until then, so the transcript window must start when it is really sent, not at the release (#102).
    await this.opening; // a failed connect already ended this turn through fail()
    if (turn !== this.turn) return;
    const t0 = this.d.now();
    this.d.session.commit();
    // push-to-talk and the local gate keep the full window: a long question can take its time to transcribe (#141);
    // the shorter failWaitMs is for server turns only (armSrv)
    const transcript = await this.waitFor((e) => e.type === "transcript", this.d.transcriptTimeoutMs);
    if (turn !== this.turn) return;
    const query = transcript && transcript.type === "transcript" ? transcript.text.trim() : "";
    this.latency = { commitToTranscriptMs: this.d.now() - t0 };
    if (!query) { this.noTranscript(); return; } // nothing heard (silence, a failed or late transcription): never ask an empty question
    const routed = this.routeQuestion(query);
    this.d.onTranscript?.(query);
    this.noteQuestion(query);
    await this.routedVoice(routed);
    if (turn !== this.turn) return;

    await this.answerQuery(turn, query);
  }

  /**
   * Nothing usable was heard: say so now, and drop a language hint that may be the cause (#135). `serverTurn`: a real
   * hands-free turn on Boson's turn detection; after EMPTY_TURNS_FALLBACK in a row, hands-free falls back to the local gate.
   */
  private noTranscript(serverTurn = false): void {
    const giveUp = serverTurn && ++this.emptyTurns >= EMPTY_TURNS_FALLBACK;
    this.d.onToast?.(giveUp ? "I didn't catch that. Hands-free now uses the local voice gate." : "I didn't catch that. Try again.");
    this.turn++;
    this.clearSrv();
    this.backToReading();
    if (giveUp) this.fallBackToLocal();
    if (this.hint !== null) { // live: a wrong hint swallowed the transcript entirely
      this.recentQuestions = [];
      this.hint = null;
      this.d.session.setLanguageHint?.(null);
    }
  }

  /** Server turns keep failing: the local voice gate runs hands-free from now on (this session). */
  private fallBackToLocal(): void {
    this.serverGaveUp = true;
    this.serverMode = false;
    this.applyHold(false);
    this.gate.reset();
    this.clearSrv();
    this.d.session.setServerTurns?.(false);
  }

  /** A server turn that was not a question (a click, a breath, echo): end it without a word and remove its items. */
  private dropTurn(srv: NonNullable<TalkController["srv"]>): void {
    for (const id of srv.items) this.d.session.deleteItem?.(id);
    this.turn++;
    this.clearSrv();
    this.backToReading();
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

  /**
   * speech_started: a new question, more of the current one, or (busy, or the mic held over the character) stray
   * echo, which is simply ignored: its commit clears the buffer (onCommitted) and its transcript finds no turn. The
   * item id is no key for that (live, every committed turn carries the same id).
   */
  private onServerSpeech(itemId: string | undefined): void {
    const srv = this.srv;
    if (srv && (this._state === "listening" || this._state === "transcribing")) { // the same question goes on
      if (srv.timer) { clearTimeout(srv.timer); srv.timer = null; }
      srv.speaking = true;
      srv.heardSpeech = true;
      if (itemId && !srv.items.includes(itemId)) srv.items.push(itemId);
      if (this._state === "transcribing") this.set("listening");
      return;
    }
    if (!["reading", "talk-idle", "answering", "resuming"].includes(this._state)) return; // busy: not a question we answer
    // The mic is held while the character speaks, so the server cannot hear them now; a speech_started anyway is for
    // audio sent before the hold (their first syllable) or stray: echo. A barge-in comes from the local gate (bargeIn).
    if (this.micHeld || (this.speaking && !this.bargeOpen)) return;
    void this.beginTurn(true, false, true, false, itemId);
  }

  private onServerStop(): void {
    const srv = this.srv;
    if (!srv || this._state !== "listening") return;
    srv.speaking = false;
    srv.stoppedAt = this.d.now();
    this.set("transcribing");
    this.armSrv(failWaitMs(srv.stoppedAt - srv.startedAt));
  }

  private onCommitted(itemId: string | undefined): void {
    const srv = this.srv;
    if (!srv || !["listening", "transcribing"].includes(this._state)) {
      // a stray turn (echo, a noise while the character speaks or thinks): its audio must not lead the next question
      this.d.session.clearInput?.();
      return;
    }
    if (itemId && !srv.items.includes(itemId)) srv.items.push(itemId);
  }

  private onServerTranscript(text: string, _itemId: string | undefined): void {
    const srv = this.srv;
    if (!srv || !["listening", "transcribing"].includes(this._state)) return;
    srv.transcripts = mergeTranscript(srv.transcripts, text);
    if (srv.speaking) return; // more of the question is coming
    this.armSrv(Math.max(0, srv.stoppedAt + MERGE_MS - this.d.now()));
  }

  /** The server's turn is over: answer it through the normal flow (retrieval instructions, then our response.create). */
  private async finishSrv(): Promise<void> {
    const srv = this.srv;
    if (!srv) return;
    this.clearSrv();
    this.d.session.clearInput?.(); // the question's audio is transcribed: the next question starts from an empty buffer
    const turn = this.turn;
    const query = srv.transcripts.join(" ").trim();
    if (!query) {
      if (srv.voicedMs < MIN_TURN_MS) { this.dropTurn(srv); return; } // a click, a breath, a noise: not worth a word
      for (const id of srv.items) this.d.session.deleteItem?.(id); // an empty user turn must not stay in the conversation
      this.noTranscript(!srv.pressed); // a pressed turn never counts towards giving up on server turns
      return;
    }
    this.emptyTurns = 0;
    this.latency = { commitToTranscriptMs: this.d.now() - srv.stoppedAt };
    const routed = this.routeQuestion(query);
    this.d.onTranscript?.(query);
    this.noteQuestion(query);
    await this.opening; // a barge-in's cancel must be acknowledged before our response.create
    await this.routedVoice(routed);
    if (turn !== this.turn) return;
    await this.answerQuery(turn, query);
  }

  /** A half-heard server turn is abandoned (a typed question, Stop): its audio must not lead the next question. */
  private abandonSrv(): void {
    if (this.srv) this.d.session.clearInput?.();
    this.clearSrv();
  }

  /** A typed question: the same turn as a spoken one, without the mic. */
  async ask(text: string): Promise<void> {
    const query = text.trim();
    if (!query) return;
    this.abandonSrv();
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
    const routed = this.routeQuestion(query); // before onTranscript: the question bubble is the routed speaker's
    this.d.onTranscript?.(query);
    this.noteQuestion(query);
    this.set("retrieving");
    if ((wasAnswering || wasBusy) && !viaAnswer) {
      try { await this.d.session.interrupt(); } catch (e) { this.fail(turn, e); return; }
    }
    if (turn !== this.turn) return;
    await this.routedVoice(routed);
    if (turn !== this.turn) return;
    if (!(this.d.answer && this.d.speaker)) {
      try { await this.d.session.ensureOpen(this.d.voiceFor(this.speakerId)); } catch (e) { this.fail(turn, e); return; }
      if (turn !== this.turn) return;
    }
    await this.answerQuery(turn, query);
  }

  /** Stop speaking and drop the current answer (Stop button). */
  async stop(): Promise<void> {
    this.abandonSrv();
    const viaAnswer = this.run !== null;
    this.cancelAnswer();
    this.turn++;
    if (!viaAnswer && ["answering", "retrieving", "transcribing"].includes(this._state)) {
      try { await this.d.session.interrupt(); } catch { /* nothing to cancel is fine */ }
    }
    this.backToReading();
  }

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

  private async answerQuery(turn: number, query: string, step = false): Promise<void> {
    this.set("retrieving");
    const r0 = this.d.now();
    const quiz = step ? null : this.quizTurn(query);
    if (this.d.answer && this.d.speaker) {
      // Gemini writes the answer (no Realtime, so no function calling): a plain spoken command is acted on here instead
      const intent = this.d.tools && !step ? parseIntent(query) : null;
      if (intent && this.d.tools) {
        const outcome = this.runTool(intent.name, intent.args);
        if (outcome.ok && outcome.ask) { void this.streamAndSpeak(turn, outcome.ask, r0); return; } // quiz / explain: Gemini answers it aloud
        if (outcome.ok) { this.turn++; this.endTurn(); return; }
        this.d.onToast?.(outcome.output); // refused (e.g. a spoiler jump): tell the reader, no question is sent
        this.turn++;
        this.endTurn();
        return;
      }
      if (!step) this.startRound(turn, query, !!quiz);
      void this.streamAndSpeak(turn, quiz ? quizGeminiQuery(quiz.quiz, quiz.n, query) : query, r0);
      return;
    }
    if (!step) this.startRound(turn, query, !!quiz);
    const who = this.answerSpeaker;
    if (step) { // the Realtime voice is per session: switch it for this character (TalkClient reconnects if refused)
      const r = this.liveRound();
      if (r) r.voiced = true;
      try { await this.d.session.setVoice(this.d.voiceFor(who)); } catch { /* answered in the old voice: still their words */ }
      if (turn !== this.turn) return;
    }
    let instructions: string;
    try {
      const res = await this.withTimeout(this.d.retrieve(query, this.d.reader.currentPara(), who), this.d.retrieveTimeoutMs);
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
    // the question always goes with the request (spoken or typed): live, the model does not see the committed audio
    // turn (or any conversation item) when the response carries instructions (TalkClient.respond)
    this.d.session.respond(instructions, query);
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
      await answer(query, this.d.reader.currentPara(), this.answerSpeaker, (text) => {
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
    if (e.type === "searching") {
      this.d.onSearching?.(e.active, e.completed ?? false);
      // the search and the answer the server then writes take their own time: a fresh watchdog for them
      if (e.active && this._state === "answering") void this.awaitAnswer(this.turn, this.d.now());
    }
    if (e.type === "sources" && ["retrieving", "answering"].includes(this._state)) this.d.onSources?.(e.sources);
    if (e.type === "turn-mode" && !e.server && this.serverMode) { // refused: the local gate takes over hands-free
      this.serverMode = false;
      this.applyHold(false);
      this.gate.reset();
      this.clearSrv();
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
    if (this.nextInRound()) return;
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

  // ---- group scene (#79) -----------------------------------------------------------------------------------

  /** A new question: a group round when other characters are picked (not during a quiz, which is one-to-one). */
  private startRound(turn: number, query: string, quiz: boolean): void {
    this.endRound();
    const speakers = [this.speakerId, ...this.group.filter((id) => id !== this.speakerId)].slice(0, MAX_GROUP);
    if (quiz || speakers.length < 2) return;
    this.round = { turn, query, speakers, at: 0, voiced: false };
  }

  /** One answer of the round is complete: the next character answers the same question (true), or the round is over. */
  private nextInRound(): boolean {
    const r = this.liveRound();
    if (!r || r.at + 1 >= r.speakers.length) { this.endRound(); return false; }
    r.at++;
    this.d.onRoundTurn?.(r.speakers[r.at], r.at);
    void this.answerQuery(r.turn, r.query, true);
    return true;
  }

  /** The round is over (or abandoned): the session voice goes back to the one talked to. */
  private endRound(): void {
    const r = this.round;
    this.round = null;
    if (r?.voiced) void this.d.session.setVoice(this.d.voiceFor(this.speakerId)).catch(() => {});
  }

  /** Back to where we were: resume reading only if the story was playing when the question started. */
  private backToReading(): void {
    this.endRound(); // cut off, failed or stopped: the rest of a group round never answers
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
