/**
 * #10 barge-in: the mic press (or, hands-free, the reader's voice) cuts the character off. Fake socket for the wire
 * order (truncate at what was heard, then cancel, then listen), fakes for the loop, pure tests for the voice gate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import { TalkController, type ReaderControl, type SessionEvent, type TalkSession, type TalkState } from "../src/talk/TalkController";
import { HANDS_FREE, HANDS_FREE_OVER_SPEECH, createVoiceGate, type VoiceGateEvent } from "../src/talk/handsFree";
import { INTERRUPTED_MARK, addQuestion, appendSentence, finishAnswer, markInterrupted, priorTurns } from "../src/talk/chatLog";

// ---- fake socket ----

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  constructor(public url: string, public protocols: string[]) {}
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  open() { this.readyState = 1; this.onopen?.(); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
  types() { return this.sent.map((e) => e.type); }
}
class FakePlayer implements PlayerLike {
  playedMs = 0;
  log: string[] = [];
  async resume() {}
  enqueueBase64() { this.log.push("enqueue"); }
  async flush() { this.log.push("flush"); return this.playedMs; }
  reset() { this.log.push("reset"); }
  async close() {}
}
const flush = () => new Promise((r) => setTimeout(r, 0));

async function openClient() {
  const sockets: FakeSocket[] = [];
  const micLog: string[] = [];
  let micChunk: (b64: string) => void = () => {};
  const mic: MicLike = { start: async () => { micLog.push("start"); }, stop: async () => { micLog.push("stop"); } };
  const player = new FakePlayer();
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: (onChunk) => { micChunk = onChunk; return mic; }, cancelWaitMs: 50,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  return { client, ws: sockets[0], events, player, micLog, chunk: (b: string) => micChunk(b) };
}

describe("barge-in on the wire (TalkClient)", () => {
  it("stops playback, truncates the assistant item at the audio heard, cancels, waits for the ack, then the next turn commits", async () => {
    const { client, ws, player } = await openClient();
    client.respond("Be Alice.");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_item.added", item: { id: "item_a", type: "message" } });
    ws.recv({ type: "response.output_audio.delta", item_id: "item_a", delta: "AAAA" });
    ws.recv({ type: "response.output_audio.delta", item_id: "item_a", delta: "BBBB" });
    player.playedMs = 1234.4;

    const done = client.interrupt();
    await flush();
    expect(player.log.at(-1)).toBe("flush"); // local audio stops first: the 0.15 s that matters
    const after = ws.types().indexOf("response.create");
    expect(ws.types().slice(after + 1)).toEqual(["conversation.item.truncate", "response.cancel"]);
    expect(ws.sent.at(-2)).toEqual({ type: "conversation.item.truncate", item_id: "item_a", content_index: 0, audio_end_ms: 1234 });
    // the cancel is acknowledged with a cancelled response.done; only then may the next turn be committed
    let settled = false;
    void done.then(() => { settled = true; });
    await flush();
    expect(settled).toBe(false);
    ws.recv({ type: "response.done", response: { id: "r1", status: "cancelled", output: [] } });
    await done;
    client.commit();
    expect(ws.types().at(-1)).toBe("input_audio_buffer.commit");
  });

  it("without any audio heard there is nothing to truncate: only the cancel goes out", async () => {
    const { client, ws } = await openClient();
    client.respond("x");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    const done = client.interrupt();
    await flush();
    expect(ws.types().slice(-2)).toEqual(["response.create", "response.cancel"]);
    ws.recv({ type: "response.done", response: { id: "r1", status: "cancelled", output: [] } });
    await done;
  });

  it("an interrupt when no response is active neither truncates nor cancels", async () => {
    const { client, ws } = await openClient();
    await client.interrupt();
    expect(ws.types()).toEqual(["session.update"]);
  });

  it("hands-free monitor keeps the mic open, keeps only the last ~300 ms, and sends it first when a turn starts", async () => {
    const { client, ws, micLog, chunk } = await openClient();
    await client.setMonitor(true);
    expect(micLog).toEqual(["start"]);
    ["A", "B", "C", "D", "E"].forEach(chunk); // monitoring: nothing is sent
    expect(ws.types().filter((t) => t === "input_audio_buffer.append")).toHaveLength(0);
    await client.startMic();
    chunk("F");
    expect(ws.sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => e.audio)).toEqual(["C", "D", "E", "F"]);
    await client.stopMic();
    expect(micLog).toEqual(["start", "start"]); // never stopped while monitoring
    chunk("G"); // back to monitoring
    expect(ws.sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => e.audio)).toEqual(["C", "D", "E", "F"]);
    await client.setMonitor(false);
    expect(micLog.at(-1)).toBe("stop");
  });
});

// ---- the voice gate ----

describe("voice gate (hands-free)", () => {
  it("opens after sustained speech, not on a click, and closes after the silence window", () => {
    const g = createVoiceGate({ threshold: 0.04, minSpeechMs: 150, silenceMs: 700 });
    expect(g.feed(0.1, 0)).toBeNull();
    expect(g.feed(0.01, 50)).toBeNull(); // a click: back under before 150 ms
    expect(g.feed(0.1, 100)).toBeNull();
    expect(g.feed(0.1, 200)).toBeNull();
    expect(g.feed(0.1, 260)).toBe("start");
    expect(g.speaking).toBe(true);
    expect(g.feed(0.01, 300)).toBeNull();
    expect(g.feed(0.1, 600)).toBeNull(); // a breath, then more words: the turn goes on
    expect(g.feed(0.01, 700)).toBeNull();
    expect(g.feed(0.01, 1300)).toBeNull();
    expect(g.feed(0.01, 1400)).toBe("stop");
    expect(g.speaking).toBe(false);
  });
  it("defaults suit a laptop microphone: room noise stays under the threshold, speech is over it", () => {
    const g = createVoiceGate();
    for (let t = 0; t < 2000; t += 20) expect(g.feed(0.015, t)).toBeNull(); // fan noise
    expect(HANDS_FREE.threshold).toBeLessThan(0.05);
    expect(HANDS_FREE.silenceMs).toBeGreaterThanOrEqual(600);
  });

  it("the over-speech profile (#103) needs louder, longer speech: residual echo and blips pass, real speech opens it", () => {
    expect(HANDS_FREE_OVER_SPEECH.threshold).toBeGreaterThanOrEqual(0.08);
    expect(HANDS_FREE_OVER_SPEECH.minSpeechMs).toBeGreaterThanOrEqual(350);
    const g = createVoiceGate();
    const strict = HANDS_FREE_OVER_SPEECH;
    for (let t = 0; t < 2000; t += 20) expect(g.feed(0.07, t, strict)).toBeNull(); // what the echo canceller leaves of the character
    for (let t = 2000; t < 2300; t += 20) expect(g.feed(0.3, t, strict)).toBeNull(); // a 300 ms blip
    expect(g.feed(0.01, 2300, strict)).toBeNull();
    let started: number | null = null;
    for (let t = 2320; t < 3000 && started === null; t += 20) if (g.feed(0.2, t, strict) === "start") started = t;
    expect(started).not.toBeNull();
    expect(started! - 2320).toBeGreaterThanOrEqual(strict.minSpeechMs);
    // once open, the profile in force decides the end as usual
    for (let t = started! + 20; t < started! + 700; t += 20) expect(g.feed(0.01, t, strict)).toBeNull();
    expect(g.feed(0.01, started! + 720, strict)).toBe("stop");
    // the idle profile is unchanged: the same 0.07 is speech when nobody else is talking
    const g2 = createVoiceGate();
    let idleStart: VoiceGateEvent = null;
    for (let t = 0; t < 400; t += 20) idleStart = g2.feed(0.07, t) ?? idleStart;
    expect(idleStart).toBe("start");
  });
});

// ---- the loop ----

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  monitor: boolean | null = null;
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); }
  async setVoice() {}
  async startMic() { this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(instructions: string) { this.calls.push(`respond:${instructions}`); }
  async interrupt() { this.calls.push("interrupt"); }
  async setMonitor(on: boolean) { this.monitor = on; this.calls.push(`monitor:${on}`); }
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}
class FakeReader implements ReaderControl {
  log: string[] = [];
  constructor(public playing = true, public index = 5) {}
  isPlaying() { return this.playing; }
  currentIndex() { return this.index; }
  currentPara() { return 2; }
  pause() { this.playing = false; this.log.push("pause"); }
  play() { this.playing = true; this.log.push("play"); }
  resumeAt(i: number) { this.playing = true; this.index = i; this.log.push(`resumeAt:${i}`); }
}

function setup(opts: { gemini?: boolean; tapMs?: number } = {}) {
  const session = new FakeSession();
  const reader = new FakeReader(false);
  const states: TalkState[] = [];
  const speakerLog: string[] = [];
  let interrupted = 0;
  let release: (() => void) | null = null;
  const talk = new TalkController({
    session, reader, tapMs: opts.tapMs,
    retrieve: async () => ({ instructions: "INSTR" }),
    ...(opts.gemini ? {
      answer: async (_q: string, _p: number, _s: string, onSentence: (t: string) => void) => { onSentence("First sentence."); onSentence("Second sentence."); },
      speaker: {
        play: (t: string) => { speakerLog.push(`play:${t}`); return new Promise<void>((r) => { release = r; }); },
        prefetch: () => {}, stop: () => { speakerLog.push("stop"); release?.(); },
      },
    } : {}),
    voiceFor: () => "marcus",
    onState: (s) => states.push(s), onInterrupted: () => { interrupted++; },
  });
  return { session, reader, talk, states, speakerLog, interrupted: () => interrupted };
}
const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);

async function ask(h: ReturnType<typeof setup>, text: string, holdMs = 600) {
  await h.talk.pttDown();
  await tick(holdMs);
  const up = h.talk.pttUp();
  await tick(0);
  h.session.emit({ type: "transcript", text });
  await tick(0);
  await up;
}

describe("barge-in in the loop (TalkController)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("Realtime: the mic press while the answer plays interrupts, marks the bubble and listens at once", async () => {
    const h = setup();
    await ask(h, "Who is Alice?");
    h.session.emit({ type: "audio-start" });
    expect(h.talk.state).toBe("answering");
    const down = h.talk.pttDown();
    expect(h.talk.state).toBe("listening"); // synchronous: no wait for the server
    expect(h.interrupted()).toBe(1);
    await down;
    expect(h.session.calls.slice(-3)).toEqual(["interrupt", "open:marcus", "mic-start"]);
  });

  it("Gemini + TTS: the mic press stops the sentence being spoken and starts a new question", async () => {
    const h = setup({ gemini: true });
    await ask(h, "Who is Alice?");
    await tick(0);
    expect(h.talk.state).toBe("answering");
    expect(h.speakerLog).toEqual(["play:First sentence."]);
    await h.talk.pttDown();
    expect(h.speakerLog).toEqual(["play:First sentence.", "stop"]);
    expect(h.interrupted()).toBe(1);
    expect(h.talk.state).toBe("listening");
    expect(h.session.calls).not.toContain("interrupt"); // nothing on the socket to cancel
    await tick(600);
    const up = h.talk.pttUp();
    await tick(0);
    h.session.emit({ type: "transcript", text: "And the Hatter?" });
    await tick(0);
    await up;
    await tick(0);
    expect(h.speakerLog.at(-1)).toBe("play:First sentence."); // the new answer plays from its first sentence
    expect(h.speakerLog.filter((l) => l.startsWith("play"))).toHaveLength(2);
  });

  it("a question typed during an answer interrupts it too; a question with nothing playing is not an interruption", async () => {
    const h = setup();
    await ask(h, "x");
    h.session.emit({ type: "audio-start" });
    void h.talk.ask("typed");
    expect(h.interrupted()).toBe(1);
    await tick(0);
    h.session.emit({ type: "response-done", status: "completed" });
    h.session.emit({ type: "playback-drained" });
    await tick(0);
    await ask(h, "another");
    expect(h.interrupted()).toBe(1);
  });

  it("hands-free is off by default; on, the reader's voice barges in and silence ends the question (never a tap)", async () => {
    const h = setup({ tapMs: 5000 }); // a gate turn shorter than a tap must still be a question
    expect(h.session.monitor).toBeNull();
    await h.talk.setHandsFree(true);
    expect(h.session.calls.at(-1)).toBe("monitor:true");
    await ask(h, "Who is Alice?", 6000); // held well past the long tap window
    h.session.emit({ type: "audio-start" });
    expect(h.talk.state).toBe("answering");
    // the character talks; the reader speaks over them, clearly and for half a second (HANDS_FREE_OVER_SPEECH)
    for (let i = 0; i < 25; i++) { h.talk.noteLevel(0.2); await tick(20); }
    expect(h.talk.state).toBe("listening");
    expect(h.interrupted()).toBe(1);
    await tick(0);
    expect(h.session.calls.filter((c) => c === "interrupt")).toHaveLength(1);
    // then goes quiet
    for (let i = 0; i < 40; i++) { h.talk.noteLevel(0.01); await tick(20); }
    expect(h.talk.state).toBe("transcribing");
    expect(h.session.calls.at(-1)).toBe("commit");
    expect(h.reader.log).not.toContain("play"); // not treated as a tap
  });

  it("hands-free: the character's echo and a short blip never barge in; sustained loud speech does (#103)", async () => {
    const h = setup({ tapMs: 5000 });
    await h.talk.setHandsFree(true);
    await ask(h, "Who is Alice?", 6000);
    h.session.emit({ type: "audio-start" }); // the character speaks: the laptop mic hears what the echo canceller leaves
    expect(h.talk.state).toBe("answering");
    for (let i = 0; i < 50; i++) { h.talk.noteLevel(0.06); await tick(20); } // a second of residual echo: loud for the idle gate
    expect(h.talk.state).toBe("answering");
    for (let i = 0; i < 10; i++) { h.talk.noteLevel(0.25); await tick(20); } // a 200 ms blip (cough, click)
    for (let i = 0; i < 10; i++) { h.talk.noteLevel(0.06); await tick(20); }
    expect(h.talk.state).toBe("answering");
    expect(h.interrupted()).toBe(0);
    for (let i = 0; i < 25; i++) { h.talk.noteLevel(0.2); await tick(20); } // half a second of real speech over the character
    expect(h.talk.state).toBe("listening");
    expect(h.interrupted()).toBe(1);
  });

  it("hands-free: the echo tail right after the answer is judged strictly too, then the idle gate applies again (#103)", async () => {
    const h = setup({ tapMs: 5000 });
    await h.talk.setHandsFree(true);
    await ask(h, "Who is Alice?", 6000);
    h.session.emit({ type: "audio-start" });
    h.session.emit({ type: "response-done", status: "completed" });
    h.session.emit({ type: "playback-drained" });
    await tick(0);
    expect(h.talk.state).toBe("talk-idle");
    for (let i = 0; i < 10; i++) { h.talk.noteLevel(0.06); await tick(20); } // 200 ms of tail at echo level
    expect(h.talk.state).toBe("talk-idle");
    await tick(300);
    for (let i = 0; i < 10; i++) { h.talk.noteLevel(0.06); await tick(20); } // the same level once the room is quiet: the reader
    expect(h.talk.state).toBe("listening");
  });

  it("hands-free: pressing the mic while the character speaks still interrupts, and silence then ends that turn", async () => {
    const h = setup({ tapMs: 5000 });
    await h.talk.setHandsFree(true);
    await ask(h, "Who is Alice?", 6000);
    h.session.emit({ type: "audio-start" });
    await h.talk.pttDown();
    expect(h.talk.state).toBe("listening");
    expect(h.interrupted()).toBe(1);
    for (let i = 0; i < 10; i++) { h.talk.noteLevel(0.12); await tick(20); }
    for (let i = 0; i < 40; i++) { h.talk.noteLevel(0.01); await tick(20); }
    expect(h.talk.state).toBe("transcribing"); // the gate ends the pressed turn too: no need to release the key
  });

  it("hands-free off again releases the monitor, and levels are ignored", async () => {
    const h = setup();
    await h.talk.setHandsFree(true);
    await h.talk.setHandsFree(false);
    expect(h.session.calls.slice(-2)).toEqual(["monitor:true", "monitor:false"]);
    for (let i = 0; i < 20; i++) { h.talk.noteLevel(0.2); await tick(20); }
    expect(h.talk.state).toBe("reading");
  });
});

describe("the chat remembers the interruption", () => {
  it("marks the pending answer and tells the next answer where the reader cut in", () => {
    let m = addQuestion([], "Who is Alice?", "alice", "voice");
    m = appendSentence(m, "A girl who");
    m = markInterrupted(m);
    m = finishAnswer(m);
    expect(m[1]).toMatchObject({ role: "character", text: "A girl who", interrupted: true, pending: false });
    expect(priorTurns(m, "alice")).toEqual([
      { role: "user", text: "Who is Alice?" },
      { role: "character", text: `A girl who${INTERRUPTED_MARK}` },
    ]);
  });
});
