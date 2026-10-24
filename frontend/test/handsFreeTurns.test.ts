/**
 * Hands-free on Boson turn detection, user report of 2026-10-24 (textbook, Tutor, Gemini answers): every question
 * contained all the earlier ones ("Hey, what is Boolean retrieval? Hey, I don't understand anything. Yes, see, okay."),
 * every answer was labelled interrupted, and sometimes it stuck until hands-free was toggled.
 *
 * Live probe (scripts/probe_realtime_turns.py, 2026-10-24): with `create_response: false` the server never clears the
 * input audio buffer. Every committed turn reuses the SAME item_id and its transcript covers all audio since the
 * session started (or since the last `input_audio_buffer.clear`); an assistant item or deleting the item changes
 * nothing. The fakes here behave like that server.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import { EMPTY_TURNS_FALLBACK, MIC_HOLD_TAIL_MS, TalkController, failWaitMs, type ReaderControl, type TalkState } from "../src/talk/TalkController";
import { addQuestion, appendSentenceTo, cutHeard, finishAnswer, markInterrupted, setTextOf, type ChatMessage } from "../src/talk/chatLog";

/** Boson as probed: one input buffer, one item id, a transcript of everything in the buffer. */
class FakeBoson implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  buffer: string[] = []; // what was said into the buffer since the last clear
  readonly itemId = "1a91cb05";
  send(data: string) {
    const e = JSON.parse(data);
    this.sent.push(e);
    if (e.type === "input_audio_buffer.clear") this.buffer = [];
  }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  open() { this.readyState = 1; this.onopen?.(); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
  types() { return this.sent.map((e) => e.type); }
  heard() { return this.sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => atob(e.audio).trim()); }
  /** The server detects a turn whose speech was `said`; the transcript is of the whole buffer. */
  commitTurn(said: string) {
    this.buffer.push(said);
    this.recv({ type: "input_audio_buffer.speech_stopped", item_id: this.itemId });
    this.recv({ type: "input_audio_buffer.committed", item_id: this.itemId });
    this.recv({ type: "conversation.item.added", item: { id: this.itemId, type: "message", role: "user" } });
  }
  transcribe() {
    this.recv({ type: "conversation.item.input_audio_transcription.completed", item_id: this.itemId, transcript: this.buffer.join(" ") });
  }
}
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await vi.advanceTimersByTimeAsync(0); };
const pcm = (label: string) => btoa(label.padEnd(4800, " ")); // 100 ms of PCM16 at 24 kHz

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function makeClient() {
  const sockets: FakeBoson[] = [];
  let micChunk: (b64: string) => void = () => {};
  const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };
  const player = { played: 0, log: [] as string[], onSpeaking: (_s: boolean) => {} };
  const p: PlayerLike = {
    resume: async () => {}, enqueueBase64: () => { player.log.push("enqueue"); }, flush: async () => { player.log.push("flush"); return player.played; },
    playedMs: () => player.played, reset: () => { player.log.push("reset"); }, close: async () => {},
  };
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: () => { const s = new FakeBoson(); sockets.push(s); return s; },
    createPlayer: (onSpeaking) => { player.onSpeaking = onSpeaking; return p; },
    createMic: (onChunk) => { micChunk = onChunk; return mic; },
    cancelWaitMs: 50, voiceChangeWaitMs: 50, backoffMs: [],
  });
  return { client, sockets, player, mic, chunk: (b: string) => micChunk(b), ws: () => sockets[sockets.length - 1] };
}

/**
 * The controller over the real client with the chat wired as useTalk wires it. `gemini`: the backend writes the
 * answer and a sentence speaker voices it (the user's setup: RETRIEVAL_BACKEND=vertex); else Realtime answers.
 */
function loop(opts: { gemini?: boolean } = {}) {
  let now = 0;
  const h = makeClient();
  const reader: ReaderControl & { log: string[]; playing: boolean } = {
    log: [], playing: false,
    isPlaying() { return this.playing; }, currentIndex: () => 4, currentPara: () => 2,
    pause() { this.playing = false; this.log.push("pause"); }, play() { this.playing = true; this.log.push("play"); },
    resumeAt(i: number) { this.playing = true; this.log.push(`resumeAt:${i}`); },
  };
  const states: TalkState[] = [];
  const toasts: string[] = [];
  const asked: string[] = [];
  let chat: ChatMessage[] = [];
  let liveId: number | undefined;
  let interrupted = 0;
  // the Gemini path: the answer is one sentence; its clip plays until endSentence() is called
  let endSentence: (() => void) | null = null;
  let talk: TalkController;
  const gemini = opts.gemini ? {
    answer: async (q: string, _p: number, _s: string, onSentence: (t: string) => void) => { asked.push(q); const t = `About "${q}".`; chat = appendSentenceTo(chat, liveId, t); onSentence(t); },
    speaker: {
      prefetch: () => {},
      play: () => new Promise<void>((resolve) => { talk.notePlaying(true); endSentence = () => { endSentence = null; talk.notePlaying(false); resolve(); }; }),
      stop: () => { endSentence?.(); },
    },
  } : {};
  talk = new TalkController({
    session: h.client, reader, serverTurns: true,
    retrieve: async (q) => { asked.push(q); return { instructions: "INSTR" }; },
    ...gemini,
    voiceFor: () => "tutor", now: () => now,
    onState: (s) => { states.push(s); if (["reading", "talk-idle", "resuming"].includes(s)) chat = finishAnswer(chat); },
    onToast: (t) => toasts.push(t),
    onTranscript: (t) => { chat = addQuestion(chat, t, "tutor", "voice"); liveId = chat.findLast((x) => x.pending)?.id; },
    onCaption: (t) => { if (t && !opts.gemini) chat = setTextOf(chat, liveId, t); },
    onInterrupted: () => { interrupted++; chat = markInterrupted(chat); },
    onHeard: (t) => { chat = cutHeard(chat, t); },
  });
  const advance = async (ms: number) => { now += ms; await vi.advanceTimersByTimeAsync(ms); };
  let readings = 0;
  /** The mic for `ms`: a level reading every 20 ms and a 100 ms chunk labelled `label` every 100 ms (as MicCapture does). */
  const audio = async (label: string, rms: number, ms: number) => {
    for (let t = 0; t < ms; t += 20) {
      talk.noteLevel(rms);
      if (++readings % 5 === 0) h.chunk(pcm(label));
      await advance(20);
    }
  };
  const bubbles = () => chat.map((x) => `${x.role}:${x.text}${x.interrupted ? " [cut]" : ""}`);
  return { ...h, talk, reader, states, toasts, asked, advance, audio, bubbles, interrupted: () => interrupted, endSentence: () => endSentence?.() };
}
type L = ReturnType<typeof loop>;

async function handsFreeOn(l: L) {
  const p = l.talk.setHandsFree(true);
  await settle();
  l.ws().open();
  await p;
  await settle();
  l.ws().recv({ type: "session.created", session: { audio: { input: { turn_detection: { type: "semantic_vad", create_response: false } } } } });
}
/** The reader says `said` for `ms`; the server detects it, commits and transcribes (the whole buffer, as live). */
async function says(l: L, said: string, ms = 800, rms = 0.2) {
  const ws = l.ws();
  ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId });
  await settle();
  await l.audio(`said:${said.slice(0, 8)}`, rms, ms);
  ws.commitTurn(said);
  await l.audio("quiet", 0.005, 200);
  ws.transcribe();
  await l.audio("quiet", 0.005, 700); // past the merge window
  await settle();
}
/** Realtime answers: created, audio, caption, done; playback continues until drain(). */
async function realtimeAnswer(l: L, rid: string, text: string) {
  const ws = l.ws();
  ws.recv({ type: "response.created", response: { id: rid } });
  ws.recv({ type: "response.output_item.added", response_id: rid, item: { id: `a-${rid}`, type: "message" } });
  ws.recv({ type: "response.output_audio.delta", response_id: rid, item_id: `a-${rid}`, delta: "AAAA" });
  ws.recv({ type: "response.output_audio_transcript.length", response_id: rid, delta: text, length_ms: 2000 });
  ws.recv({ type: "response.output_audio_transcript.done", response_id: rid, transcript: text });
  ws.recv({ type: "response.done", response: { id: rid, status: "completed", output: [] } });
  await settle();
}
async function drain(l: L) { l.player.played = 2000; l.player.onSpeaking(false); await settle(); }
/** The Gemini answer: its one sentence plays for `ms` (the mic is held), then ends. */
async function geminiAnswerPlays(l: L, ms = 2000) {
  await settle();
  expect(l.talk.state).toBe("answering");
  await l.audio("echo", 0.05, ms);
  l.endSentence();
  await settle();
}
/** The silence after an answer: the hold tail passes and the mic streams to the server again. */
const afterAnswer = (l: L) => l.audio("room", 0.005, MIC_HOLD_TAIL_MS + 600);
const clears = (l: L) => l.ws().types().filter((t) => t === "input_audio_buffer.clear").length;

// ---- 1. each question is its own words --------------------------------------------------------------------------

describe("hands-free questions do not accumulate (the server's buffer is cleared after every turn)", () => {
  it("Gemini answers: the second question is only the second question, with its own bubbles and no interrupted label", async () => {
    const l = loop({ gemini: true });
    await handsFreeOn(l);
    await says(l, "Hey, what is Boolean retrieval?");
    expect(l.asked).toEqual(["Hey, what is Boolean retrieval?"]);
    await geminiAnswerPlays(l);
    expect(["talk-idle", "resuming", "reading"]).toContain(l.talk.state);
    await afterAnswer(l);
    expect(clears(l)).toBeGreaterThanOrEqual(1); // the first question's audio is gone from the server's buffer
    await says(l, "Hey, I don't understand anything.");
    expect(l.asked).toEqual(["Hey, what is Boolean retrieval?", "Hey, I don't understand anything."]);
    await geminiAnswerPlays(l);
    expect(l.interrupted()).toBe(0);
    expect(l.bubbles()).toEqual([
      "user:Hey, what is Boolean retrieval?", 'character:About "Hey, what is Boolean retrieval?".',
      "user:Hey, I don't understand anything.", 'character:About "Hey, I don\'t understand anything.".',
    ]);
  });

  it("Realtime answers: the same, across three turns", async () => {
    const l = loop();
    await handsFreeOn(l);
    const qs = ["What is Boolean retrieval?", "I don't understand anything.", "Yes, see, okay."];
    for (const [i, q] of qs.entries()) {
      await says(l, q);
      await realtimeAnswer(l, `r${i}`, `Answer ${i}.`);
      await l.audio("echo", 0.05, 1000);
      await drain(l);
      await afterAnswer(l);
    }
    expect(l.asked).toEqual(qs);
    expect(l.interrupted()).toBe(0);
    expect(l.bubbles().filter((b) => b.startsWith("user:"))).toEqual(qs.map((q) => `user:${q}`));
  });

  it("the buffer is cleared only once the turn is over: a question the server splits in two is still one question, not doubled", async () => {
    const l = loop({ gemini: true });
    await handsFreeOn(l);
    const ws = l.ws();
    ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId });
    await settle();
    await l.audio("said", 0.2, 600);
    ws.commitTurn("What is the");
    await l.advance(150);
    ws.transcribe(); // "What is the"
    await l.advance(100);
    ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId }); // within the merge window: more of it
    await settle();
    expect(clears(l)).toBe(0);
    await l.audio("said", 0.2, 600);
    ws.commitTurn("capital of France?");
    await l.advance(150);
    ws.transcribe(); // live: the whole buffer again, "What is the capital of France?"
    await l.advance(700);
    await settle();
    expect(l.asked).toEqual(["What is the capital of France?"]);
  });

  it("a turn that is dropped or not transcribed still clears the buffer, so its audio never leads the next question", async () => {
    const l = loop({ gemini: true });
    await handsFreeOn(l);
    const ws = l.ws();
    ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId });
    await settle();
    await l.audio("click", 0.2, 100); // a click: too short to be a question
    ws.commitTurn("(click)");
    await l.advance(failWaitMs(100) + 100);
    expect(l.toasts).toEqual([]);
    expect(clears(l)).toBe(1);
    await says(l, "What is Boolean retrieval?");
    expect(l.asked).toEqual(["What is Boolean retrieval?"]);
  });

  it("push-to-talk never clears the buffer behind the reader's back (only a tap discards)", async () => {
    const l = loop({ gemini: true });
    await l.talk.pttDown();
    await settle();
    l.ws().open();
    await l.advance(600);
    const up = l.talk.pttUp();
    await settle();
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Who is Alice?" });
    await settle();
    await up;
    expect(l.asked).toEqual(["Who is Alice?"]);
    expect(clears(l)).toBe(0);
  });
});

// ---- 2./3. one item id for everything: a stray turn must not silence the real ones ---------------------------------

describe("the server reuses one item id: a stray speech_started during the answer never breaks the turns after it", () => {
  it("echo while the character speaks is ignored, and the next real question (same item id) is still answered", async () => {
    const l = loop({ gemini: true });
    await handsFreeOn(l);
    await says(l, "What is Boolean retrieval?");
    await settle();
    expect(l.talk.state).toBe("answering");
    const ws = l.ws();
    await l.audio("echo", 0.05, 500);
    // a stray speech_started for audio sent just before the hold; its commit and transcript follow while the mic is held
    ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId });
    await l.audio("echo", 0.05, 400);
    ws.commitTurn("um");
    ws.transcribe();
    await l.audio("echo", 0.05, 1000);
    expect(l.talk.state).toBe("answering");
    expect(l.interrupted()).toBe(0);
    l.endSentence();
    await settle();
    await afterAnswer(l);
    await says(l, "I don't understand anything.");
    expect(l.toasts).toEqual([]);
    expect(l.asked).toEqual(["What is Boolean retrieval?", "I don't understand anything."]);
    expect(l.talk.state).toBe("answering"); // not stuck in transcribing, no "didn't catch that"
    expect(l.bubbles().filter((b) => b.startsWith("user:"))).toEqual(["user:What is Boolean retrieval?", "user:I don't understand anything."]);
  });

  it("toggling hands-free off and on gives Boson's turn detection another chance after the local-gate fallback", async () => {
    const l = loop({ gemini: true });
    await handsFreeOn(l);
    const ws = l.ws();
    for (let i = 0; i < EMPTY_TURNS_FALLBACK; i++) {
      ws.recv({ type: "input_audio_buffer.speech_started", item_id: ws.itemId });
      await settle();
      await l.audio("said", 0.2, 1500);
      ws.recv({ type: "input_audio_buffer.speech_stopped", item_id: ws.itemId });
      ws.recv({ type: "input_audio_buffer.committed", item_id: ws.itemId });
      await l.advance(failWaitMs(1500) + 100);
    }
    expect(l.talk.serverTurnsActive).toBe(false);
    await l.talk.setHandsFree(false);
    const p = l.talk.setHandsFree(true);
    await settle();
    l.ws().open();
    await p;
    expect(l.talk.serverTurnsActive).toBe(true);
  });
});
