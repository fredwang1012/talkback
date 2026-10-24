/**
 * Hands-free on Boson turn detection, user reports of 2026-10-23:
 *  - the mic heard the character's own voice (echo turns, self-interruptions, echo transcribed as a question);
 *  - "I didn't catch that" too often (clicks, breaths and echo committed as turns, a 1.5 s wait, a sticky language hint);
 *  - answers stacking in the chat (late output of a cancelled answer, captions not bound to their answer);
 *  - speaking over the character must stop it quickly.
 * The server now never hears our own playback: the mic is held while the character speaks and for MIC_HOLD_TAIL_MS
 * after, unless the local gate hears a real barge-in (then a short pre-roll goes first).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BARGE_PREROLL_MS, TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import {
  BARGE_CONFIRM_MS, EMPTY_TURNS_FALLBACK, MIC_HOLD_TAIL_MS, MIN_TURN_MS, TalkController, failWaitMs,
  type ReaderControl, type TalkState,
} from "../src/talk/TalkController";
import { ECHO_RATIO, HANDS_FREE_OVER_SPEECH, createEchoFloor } from "../src/talk/handsFree";
import { addQuestion, cutHeard, finishAnswer, markInterrupted, setTextOf, type ChatMessage } from "../src/talk/chatLog";

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  open() { this.readyState = 1; this.onopen?.(); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
  types() { return this.sent.map((e) => e.type); }
  /** The labels of the mic chunks that reached the server, in order. */
  heard() { return this.sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => atob(e.audio).trim()); }
}
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await vi.advanceTimersByTimeAsync(0); };
const pcm = (label: string) => btoa(label.padEnd(4800, " ")); // 100 ms of PCM16 at 24 kHz
const td = (ws: FakeSocket) => ws.sent.find((e) => e.type === "session.update")?.session.audio.input.turn_detection;

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function makeClient() {
  const sockets: FakeSocket[] = [];
  let micChunk: (b64: string) => void = () => {};
  const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };
  const player = { played: 0, log: [] as string[], onSpeaking: (_s: boolean) => {} };
  const p: PlayerLike = {
    resume: async () => {}, enqueueBase64: () => { player.log.push("enqueue"); }, flush: async () => { player.log.push("flush"); return player.played; },
    playedMs: () => player.played, reset: () => { player.log.push("reset"); }, close: async () => {},
  };
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: () => { const s = new FakeSocket(); sockets.push(s); return s; },
    createPlayer: (onSpeaking) => { player.onSpeaking = onSpeaking; return p; },
    createMic: (onChunk) => { micChunk = onChunk; return mic; },
    cancelWaitMs: 50, voiceChangeWaitMs: 50, backoffMs: [],
  });
  return { client, sockets, player, mic, chunk: (b: string) => micChunk(b), ws: () => sockets[sockets.length - 1] };
}

/** The controller over the real client, with the chat wired as useTalk wires it. */
function loop(opts: { serverTurns?: boolean } = {}) {
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
  const talk = new TalkController({
    session: h.client, reader, serverTurns: opts.serverTurns ?? true,
    retrieve: async (q) => { asked.push(q); return { instructions: "INSTR" }; },
    voiceFor: () => "marcus", now: () => now,
    onState: (s) => { states.push(s); if (["reading", "talk-idle", "resuming"].includes(s)) chat = finishAnswer(chat); },
    onToast: (t) => toasts.push(t),
    onTranscript: (t) => { chat = addQuestion(chat, t, "alice", "voice"); liveId = chat.findLast((x) => x.pending)?.id; },
    onCaption: (t) => { if (t) chat = setTextOf(chat, liveId, t); },
    onInterrupted: () => { interrupted++; chat = markInterrupted(chat); },
    onHeard: (t) => { chat = cutHeard(chat, t); },
  });
  const advance = async (ms: number) => { now += ms; await vi.advanceTimersByTimeAsync(ms); };
  /** The mic for `ms`: a level reading every 20 ms and a 100 ms chunk labelled `label` every 100 ms (as MicCapture does). */
  let readings = 0;
  const audio = async (label: string, rms: number, ms: number) => {
    for (let t = 0; t < ms; t += 20) {
      talk.noteLevel(rms);
      if (++readings % 5 === 0) h.chunk(pcm(label));
      await advance(20);
    }
  };
  const bubbles = () => chat.map((x) => `${x.role}:${x.text}${x.interrupted ? " [cut]" : ""}`);
  return { ...h, talk, reader, states, toasts, asked, advance, audio, bubbles, interrupted: () => interrupted };
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
/** A server turn while the reader speaks `ms` at `rms`; `text` null: no transcript ever comes. */
async function serverTurn(l: L, id: string, text: string | null, ms = 800, rms = 0.2, afterStart?: () => void) {
  l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: id });
  await settle();
  afterStart?.();
  await settle();
  await l.audio(`said:${id}`, rms, ms);
  l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: id });
  l.ws().recv({ type: "input_audio_buffer.committed", item_id: id });
  await l.audio("quiet", 0.005, 200);
  if (text !== null) l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: id, transcript: text });
  if (text !== null) await l.audio("quiet", 0.005, 700); // past the merge window
  await settle();
}
/** The character answers (Realtime): created, audio, a 2 s caption fragment, done; playback continues until drain(). */
async function characterSpeaks(l: L, rid = "r1", text = "A girl who fell down a rabbit hole.") {
  l.ws().recv({ type: "response.created", response: { id: rid } });
  l.ws().recv({ type: "response.output_item.added", response_id: rid, item: { id: `a-${rid}`, type: "message" } });
  l.ws().recv({ type: "response.output_audio.delta", response_id: rid, item_id: `a-${rid}`, delta: "AAAA" });
  l.ws().recv({ type: "response.output_audio_transcript.length", response_id: rid, delta: text, length_ms: 2000 });
  l.ws().recv({ type: "response.output_audio_transcript.done", response_id: rid, transcript: text });
  l.ws().recv({ type: "response.done", response: { id: rid, status: "completed", output: [] } });
  await settle();
}
async function drain(l: L) { l.player.played = 2000; l.player.onSpeaking(false); await settle(); }

// ---- the server never hears our own playback ------------------------------------------------------------------

describe("echo: the server never hears the character", () => {
  it("echo during playback and in the tail after it never reaches the server; the mic streams again after the tail", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    expect(l.talk.state).toBe("answering");
    await l.audio("echo", 0.06, 2000);
    await drain(l);
    await l.audio("tail", 0.03, MIC_HOLD_TAIL_MS - 100);
    expect(l.ws().heard().filter((x) => x === "echo" || x === "tail")).toEqual([]);
    await l.audio("tail", 0.03, 200);
    await l.audio("room", 0.005, 300);
    expect(l.ws().heard()).toContain("room");
    expect(l.ws().heard()).not.toContain("echo");
    expect(l.interrupted()).toBe(0);
    expect(l.asked).toEqual(["Who is Alice?"]);
  });

  it("a loud echo (speakers up) never interrupts the character: the barge-in threshold follows the echo level", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    await l.audio("echo", 0.16, 3000); // over the fixed 0.10, all of it the character
    expect(l.talk.state).toBe("answering");
    expect(l.interrupted()).toBe(0);
    expect(l.player.log).not.toContain("flush");
  });

  it("a speech_started for audio sent before the hold (or a stray one) while the character speaks is echo: no turn, no bubble, no message", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    await serverTurn(l, "echo1", "A girl who fell", 400, 0.06);
    await l.advance(5000);
    expect(l.ws().sent).toContainEqual({ type: "conversation.item.delete", item_id: "echo1" });
    expect(l.talk.state).toBe("answering");
    expect(l.toasts).toEqual([]);
    expect(l.bubbles()).toHaveLength(2); // no question was made of the echo
    expect(l.bubbles()[0]).toBe("user:Who is Alice?");
  });
});

// ---- barge-in ---------------------------------------------------------------------------------------------------

describe("barge-in in hands-free (server turns)", () => {
  it("speaking over the character stops it within ~300 ms, the speech reaches the server with a short pre-roll, and the new turn gets its own bubbles", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    await l.audio("echo", 0.05, 1000);
    l.player.played = 1000; // half of the answer heard
    let cutAt = -1;
    for (let t = 0; t < 600 && cutAt < 0; t += 20) {
      await l.audio("me", 0.3, 20);
      if (l.talk.state === "listening") cutAt = t + 20;
    }
    expect(cutAt).toBeGreaterThan(0);
    expect(cutAt).toBeLessThanOrEqual(300);
    await settle();
    expect(l.player.log).toContain("flush"); // the answer was fully generated: playback stops, truncated at what was heard
    expect(l.ws().types()).toContain("conversation.item.truncate");
    const heard = l.ws().heard();
    expect(heard.filter((x) => x === "echo").length).toBeLessThanOrEqual(1); // the pre-roll is short: at most one 100 ms chunk from before the reader spoke
    expect(heard.filter((x) => x === "me").length).toBeGreaterThan(0);
    expect(heard.filter((x) => x === "me").length * 100).toBeLessThanOrEqual(BARGE_PREROLL_MS + 100);
    await l.audio("me", 0.3, 300);
    expect(l.ws().heard().at(-1)).toBe("me"); // live speech keeps flowing
    // the server hears the reader: the same turn goes on
    await serverTurn(l, "u2", "Wait, why a rabbit?");
    expect(l.asked).toEqual(["Who is Alice?", "Wait, why a rabbit?"]);
    await characterSpeaks(l, "r2", "Because it was late.");
    l.player.played = 2000;
    await l.advance(200);
    expect(l.bubbles()).toEqual([
      "user:Who is Alice?", "character:A girl who fell [cut]",
      "user:Wait, why a rabbit?", "character:Because it was late.",
    ]);
  });

  it("a barge-in the server never confirms (a short cough over the character) ends quietly", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    await l.audio("echo", 0.05, 500);
    await l.audio("cough", 0.3, 300);
    expect(l.talk.state).toBe("listening");
    await l.audio("quiet", 0.005, BARGE_CONFIRM_MS + 200);
    expect(l.talk.state).not.toBe("listening");
    expect(l.toasts).toEqual([]);
    expect(l.bubbles().some((b) => b.startsWith("character:") && !b.includes("A girl"))).toBe(false);
  });

  it("pressing the mic while the character speaks interrupts it and the press's audio reaches the server", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    await l.audio("echo", 0.05, 500);
    await l.talk.pttDown();
    await settle();
    expect(l.talk.state).toBe("listening");
    expect(l.interrupted()).toBe(1);
    expect(l.player.log).toContain("flush");
    await l.audio("pressed", 0.2, 300);
    expect(l.ws().heard()).toContain("pressed");
  });

  it("a typed question while the character speaks interrupts it and gets its own bubbles", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    await characterSpeaks(l);
    l.player.played = 1000;
    await l.advance(200); // the caption follows the voice
    const p = l.talk.ask("And the cat?");
    await settle();
    await p;
    expect(l.interrupted()).toBe(1);
    expect(l.player.log).toContain("flush");
    await characterSpeaks(l, "r2", "The cat grins.");
    l.player.played = 2000;
    await l.advance(200);
    expect(l.bubbles()).toEqual(["user:Who is Alice?", "character:A girl who fell [cut]", "user:And the cat?", "character:The cat grins."]);
  });
});

// ---- the cancelled answer's late output, by response id ------------------------------------------------------------

describe("a cancelled answer's late output never reaches the next answer", () => {
  it("cut before its response.created arrived: that response is cancelled when it shows up and never played", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "Who is Alice?");
    expect(l.ws().types()).toContain("response.create");
    expect(l.talk.state).toBe("answering");
    // the reader speaks again before the answer even started (the server heard it: the mic was not held)
    await serverTurn(l, "u2", "Actually, who is the Queen?", 800, 0.2, () => {
      l.ws().recv({ type: "error", error: { code: "response_not_active", message: "no active response" } }); // the cancel ack
    });
    await characterSpeaks(l, "r1", "A girl who fell."); // the first answer, created late
    expect(l.ws().types().filter((t) => t === "response.cancel").length).toBeGreaterThanOrEqual(2);
    expect(l.player.log).not.toContain("enqueue");
    await characterSpeaks(l, "r2", "The Queen of Hearts.");
    expect(l.player.log).toContain("enqueue");
    l.player.played = 2000;
    await l.advance(200);
    expect(l.bubbles()).toEqual(["user:Who is Alice?", "user:Actually, who is the Queen?", "character:The Queen of Hearts."]);
  });

  it("deltas of another response id are dropped", async () => {
    const h = makeClient();
    const events: any[] = [];
    h.client.on((e) => events.push(e));
    const p = h.client.ensureOpen("m");
    await settle();
    h.ws().open();
    await p;
    h.client.respond("INSTR");
    h.ws().recv({ type: "response.created", response: { id: "r2" } });
    h.ws().recv({ type: "response.output_audio_transcript.length", response_id: "r1", delta: "Old words.", length_ms: 1000 });
    h.ws().recv({ type: "response.output_audio.delta", response_id: "r1", item_id: "a1", delta: "AAAA" });
    expect(h.player.log).not.toContain("enqueue");
    h.ws().recv({ type: "response.done", response: { id: "r1", status: "cancelled" } });
    h.ws().recv({ type: "response.output_audio.delta", response_id: "r2", item_id: "a2", delta: "BBBB" });
    h.ws().recv({ type: "response.output_audio_transcript.length", response_id: "r2", delta: "New words.", length_ms: 1000 });
    h.ws().recv({ type: "response.done", response: { id: "r2", status: "completed" } });
    h.player.played = 1000;
    h.player.onSpeaking(false);
    expect(events.filter((e) => e.type === "caption").at(-1)).toEqual({ type: "caption", text: "New words.", done: true });
    expect(events.filter((e) => e.type === "response-done")).toEqual([{ type: "response-done", status: "completed" }]);
  });
});

// ---- "I didn't catch that" only for a real turn -----------------------------------------------------------------

describe("I didn't catch that: only for a real turn whose transcript never came", () => {
  it("a turn shorter than a syllable (click, breath) is ignored silently and removed from the conversation", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", null, MIN_TURN_MS - 200);
    await l.advance(8000);
    expect(l.toasts).toEqual([]);
    expect(["reading", "talk-idle"]).toContain(l.talk.state);
    expect(l.ws().sent).toContainEqual({ type: "conversation.item.delete", item_id: "u1" });
    expect(l.bubbles()).toEqual([]);
  });

  it("an empty transcript of a short turn is ignored too", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", "  ", 200);
    await l.advance(5000);
    expect(l.toasts).toEqual([]);
  });

  it("a real turn with no transcript: the message comes only after a wait scaled with the turn length", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", null, 2000);
    const wait = failWaitMs(2000);
    expect(wait).toBeGreaterThanOrEqual(2500);
    await l.advance(wait - 500);
    expect(l.toasts).toEqual([]);
    expect(l.talk.state).toBe("transcribing");
    await l.advance(500);
    expect(l.toasts).toEqual(["I didn't catch that. Try again."]);
    expect(l.bubbles()).toEqual([]);
  });

  it("a late transcript within the wait is answered normally", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", null, 2000);
    await l.advance(2000); // live, a long turn took longer than the old 1.5 s
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "A long question?" });
    await l.advance(700);
    await settle();
    expect(l.toasts).toEqual([]);
    expect(l.asked).toEqual(["A long question?"]);
  });

  it("a turn we cancelled (Stop) never says it", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", null, 1000);
    await l.talk.stop();
    await l.advance(8000);
    expect(l.toasts).toEqual([]);
  });

  it("a failed real turn clears the language hint; the hint needs two clearly non-English questions", async () => {
    const l = loop();
    await handsFreeOn(l);
    const hintSent = () => l.ws().sent.filter((e) => e.type === "session.update" && e.session.audio?.input?.transcription && !e.session.type).map((e) => e.session.audio.input.transcription.language);
    await serverTurn(l, "u1", "¿Quién escribió esta carta y por qué?");
    expect(hintSent()).toEqual([]); // one question is not enough
    await characterSpeaks(l);
    await drain(l);
    await l.advance(MIC_HOLD_TAIL_MS + 2000);
    await serverTurn(l, "u2", "¿Por qué la reina está tan enfadada con el gato?");
    expect(hintSent()).toEqual(["es"]);
    await characterSpeaks(l, "r2");
    await drain(l);
    await l.advance(MIC_HOLD_TAIL_MS + 2000);
    await serverTurn(l, "u3", null, 1500);
    await l.advance(failWaitMs(1500) + 100);
    expect(hintSent()).toEqual(["es", null]);
  });

  it(`after ${EMPTY_TURNS_FALLBACK} real turns in a row with no transcript, hands-free falls back to the local gate and says so`, async () => {
    const l = loop();
    await handsFreeOn(l);
    for (let i = 1; i <= EMPTY_TURNS_FALLBACK; i++) {
      await serverTurn(l, `u${i}`, null, 1500);
      await l.advance(failWaitMs(1500) + 100);
    }
    expect(l.toasts).toHaveLength(EMPTY_TURNS_FALLBACK);
    expect(l.toasts.at(-1)).toMatch(/local/i);
    expect(l.talk.serverTurnsActive).toBe(false);
    expect(l.sockets[0].readyState).toBe(3); // the server-turn session is dropped
    // the local gate runs the next turn
    await l.audio("me", 0.2, 400);
    expect(l.talk.state).toBe("listening");
    await settle();
    l.ws().open();
    await settle();
    expect(td(l.ws())).toBeNull();
    await l.audio("quiet", 0.005, 900);
    expect(l.talk.state).toBe("transcribing");
    expect(l.ws().types()).toContain("input_audio_buffer.commit");
  });

  it("a successful turn in between resets the count", async () => {
    const l = loop();
    await handsFreeOn(l);
    await serverTurn(l, "u1", null, 1500);
    await l.advance(failWaitMs(1500) + 100);
    await serverTurn(l, "u2", "Who is Alice?");
    await characterSpeaks(l);
    await drain(l);
    await l.advance(MIC_HOLD_TAIL_MS + 2000);
    await serverTurn(l, "u3", null, 1500);
    await l.advance(failWaitMs(1500) + 100);
    expect(l.talk.serverTurnsActive).toBe(true);
  });
});

// ---- the local gate and push-to-talk ----------------------------------------------------------------------------

describe("barge-in with the local gate, and push-to-talk / typed bubbles", () => {
  it("local gate: speech over the character stops it within ~300 ms; a loud echo does not", async () => {
    const l = loop({ serverTurns: false });
    await l.talk.setHandsFree(true);
    await l.audio("me", 0.2, 400);
    await settle();
    l.ws().open();
    await settle();
    await l.audio("quiet", 0.005, 900);
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Who is Alice?" });
    await settle();
    await characterSpeaks(l);
    expect(l.talk.state).toBe("answering");
    await l.audio("echo", 0.16, 2000);
    expect(l.talk.state).toBe("answering");
    let cutAt = -1;
    for (let t = 0; t < 600 && cutAt < 0; t += 20) {
      await l.audio("me", 0.6, 20);
      if (l.talk.state === "listening") cutAt = t + 20;
    }
    expect(cutAt).toBeGreaterThan(0);
    expect(cutAt).toBeLessThanOrEqual(300);
  });

  it("push-to-talk and typed questions still get one question and one answer bubble each", async () => {
    const l = loop({ serverTurns: false });
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
    await characterSpeaks(l);
    await drain(l);
    await l.talk.ask("And the cat?");
    await settle();
    await characterSpeaks(l, "r2", "The cat grins.");
    await drain(l);
    expect(l.bubbles()).toEqual([
      "user:Who is Alice?", "character:A girl who fell down a rabbit hole.", "user:And the cat?", "character:The cat grins.",
    ]);
  });
});

// ---- the echo level, and the hold with the Gemini answer path ---------------------------------------------------

describe("echo floor and the mic hold on the sentence-speaker (Gemini) path", () => {
  it("the barge-in bar follows the echo heard while the character speaks; the reader's own speech never raises it", () => {
    const f = createEchoFloor();
    expect(f.profile().threshold).toBe(HANDS_FREE_OVER_SPEECH.threshold);
    for (let t = 0; t < 2000; t += 20) f.feed(0.16, t); // a loud speaker
    expect(f.profile().threshold).toBeCloseTo(0.16 * ECHO_RATIO);
    for (let t = 2000; t < 4000; t += 20) f.feed(0.8, t); // the reader talking over it for 2 s
    expect(f.profile().threshold).toBeCloseTo(0.16 * ECHO_RATIO);
  });

  it("server turns with Gemini answers: the mic is held while a sentence plays and MIC_HOLD_TAIL_MS after", async () => {
    let now = 0;
    const holds: [boolean, number | undefined][] = [];
    const listeners = new Set<(e: any) => void>();
    let release: (() => void) | null = null;
    const session = {
      ensureOpen: async () => {}, setVoice: async () => {}, startMic: async () => {}, stopMic: async () => {}, commit() {},
      respond() {}, interrupt: async () => {}, setMonitor: async () => {}, setServerTurns() {}, deleteItem() {}, isOpen: true,
      setMicHold: (on: boolean, pre?: number) => { holds.push([on, pre]); },
      on: (fn: (e: any) => void) => { listeners.add(fn); return () => listeners.delete(fn); },
    };
    const emit = (e: any) => [...listeners].forEach((fn) => fn(e));
    const reader: ReaderControl = { isPlaying: () => false, currentIndex: () => 0, currentPara: () => 0, pause() {}, play() {}, resumeAt() {} };
    const talk = new TalkController({
      session, reader, serverTurns: true, now: () => now, voiceFor: () => "m", retrieve: async () => ({ instructions: "I" }),
      answer: async (_q, _p, _s, onSentence) => { onSentence("One."); },
      speaker: { play: () => new Promise<void>((r) => { release = r; }), prefetch: () => {}, stop: () => { release?.(); } },
    });
    await talk.setHandsFree(true);
    emit({ type: "speech-started", itemId: "u1" });
    await settle();
    for (let i = 0; i < 30; i++) { talk.noteLevel(0.2); now += 20; await vi.advanceTimersByTimeAsync(20); }
    emit({ type: "speech-stopped" });
    emit({ type: "transcript", text: "Who?", itemId: "u1" });
    now += 700; await vi.advanceTimersByTimeAsync(700);
    await settle();
    expect(talk.state).toBe("answering");
    talk.notePlaying(true);
    expect(holds.at(-1)).toEqual([true, 0]);
    talk.notePlaying(false);
    now += MIC_HOLD_TAIL_MS - 50; await vi.advanceTimersByTimeAsync(MIC_HOLD_TAIL_MS - 50);
    expect(holds.at(-1)?.[0]).toBe(true);
    now += 100; await vi.advanceTimersByTimeAsync(100);
    expect(holds.at(-1)).toEqual([false, 0]); // the tail is over: the echo held meanwhile is dropped, not sent
  });
});
