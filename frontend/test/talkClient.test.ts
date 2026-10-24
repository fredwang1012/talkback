import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import type { SessionEvent } from "../src/talk/TalkController";

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
  enqueued: string[] = [];
  playedMs = 0;
  resets = 0;
  onSpeaking: (s: boolean) => void = () => {};
  async resume() {}
  enqueueBase64(b: string) { this.enqueued.push(b); }
  async flush() { return this.playedMs; }
  reset() { this.resets++; }
  async close() {}
}

function setup(extra: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const player = new FakePlayer();
  let micChunk: (b64: string) => void = () => {};
  const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };
  let tokens = 0;
  const client = new TalkClient({
    tokenProvider: async () => `bai-eph-${++tokens}`,
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: (onSpeaking) => { player.onSpeaking = onSpeaking; return player; },
    createMic: (onChunk) => { micChunk = onChunk; return mic; },
    cancelWaitMs: 50, voiceChangeWaitMs: 50,
    ...extra,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  return { client, sockets, player, mic, events, chunk: (b: string) => micChunk(b), tokens: () => tokens };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("TalkClient concurrency limit (close 1013)", () => {
  it("backs off and retries with a fresh key, then connects", async () => {
    const h = setup({ backoffMs: [5, 5, 5] });
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].close(1013, "busy");
    await new Promise((r) => setTimeout(r, 15));
    h.sockets[1].close(1013, "busy");
    await new Promise((r) => setTimeout(r, 15));
    h.sockets[2].open();
    await p;
    expect(h.sockets).toHaveLength(3);
    expect(h.tokens()).toBe(3); // a new ephemeral key every attempt
    expect(h.events.filter((e) => e.type === "error")).toHaveLength(0);
  });

  it("gives up after the last retry and reports a readable error", async () => {
    const h = setup({ backoffMs: [2] });
    const p = h.client.ensureOpen("marcus");
    const settled = p.then(() => "ok", (e: Error) => e.message);
    await flush();
    h.sockets[0].close(1013, "busy");
    await new Promise((r) => setTimeout(r, 10));
    h.sockets[1].close(1013, "busy");
    const msg = await settled;
    expect(msg).not.toBe("ok");
    expect(h.sockets).toHaveLength(2);
  });

  it("a 1013 after the session opened is surfaced, not retried", async () => {
    const h = setup({ backoffMs: [2] });
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    h.sockets[0].close(1013, "busy");
    expect(h.sockets).toHaveLength(1);
    expect(h.events.some((e) => e.type === "error")).toBe(true);
  });
});

describe("TalkClient connection", () => {
  it("opens with a fresh ephemeral key in the subprotocol and configures manual turns", async () => {
    const h = setup();
    const p = h.client.ensureOpen("oliver");
    await flush();
    h.sockets[0].open();
    await p;
    expect(h.sockets[0].protocols).toEqual(["realtime", "bai-client-secret.bai-eph-1"]);
    const cfg = h.sockets[0].sent[0];
    expect(cfg.type).toBe("session.update");
    expect(cfg.session.audio.input.turn_detection).toBeNull();
    expect(cfg.session.audio.input.transcription.model).toBe("higgs-stt-3.1");
    expect(cfg.session.audio.output.voice).toBe("oliver");
    expect(cfg.session.output_modalities).toEqual(["audio"]);
    expect(cfg.session.instructions.length).toBeLessThan(300); // short and behavioural
  });

  it("is idempotent: concurrent ensureOpen calls (StrictMode, double press) open exactly one socket", async () => {
    const h = setup();
    const a = h.client.ensureOpen("marcus");
    const b = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await Promise.all([a, b]);
    await h.client.ensureOpen("marcus");
    expect(h.sockets).toHaveLength(1);
    expect(h.tokens()).toBe(1);
  });

  it("mints a new key for every new connection", async () => {
    const h = setup();
    const a = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await a;
    h.sockets[0].close(1000, "idle");
    const b = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[1].open();
    await b;
    expect(h.sockets[1].protocols[1]).toBe("bai-client-secret.bai-eph-2");
  });

  it("treats idle timeout / max duration as a quiet close, then reconnects on the next press", async () => {
    const h = setup();
    const a = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await a;
    h.sockets[0].recv({ type: "session.idle_timeout", seconds_idle: 300 });
    h.sockets[0].close(1000, "idle");
    expect(h.events.some((e) => e.type === "error")).toBe(false);
    expect(h.events.some((e) => e.type === "closed")).toBe(true);
    const b = h.client.ensureOpen("marcus");
    await flush();
    expect(h.sockets).toHaveLength(2);
    h.sockets[1].open();
    await b;
  });

  it("surfaces billing refusal (4429) and a bad key (3000) as real errors", async () => {
    for (const code of [4429, 3000]) {
      const h = setup();
      const a = h.client.ensureOpen("marcus");
      await flush();
      h.sockets[0].open();
      await a;
      h.sockets[0].close(code, "x");
      expect(h.events.find((e) => e.type === "error")).toBeTruthy();
    }
  });

  it("ignores unknown events and malformed frames", async () => {
    const h = setup();
    const a = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await a;
    h.sockets[0].recv({ type: "conversation.context.summarized", summary: "x" });
    h.sockets[0].recv({ type: "response.output_audio_transcript.length", length_ms: 5 });
    h.sockets[0].onmessage?.({ data: "not json" });
    expect(h.events).toEqual([]);
  });
});

describe("TalkClient turns", () => {
  async function open(h: ReturnType<typeof setup>, voice = "marcus") {
    const p = h.client.ensureOpen(voice);
    await flush();
    h.sockets[0].open();
    await p;
    return h.sockets[0];
  }

  it("buffers mic audio while connecting, sends it in order, then commits", async () => {
    const h = setup();
    const opening = h.client.ensureOpen("marcus");
    await h.client.startMic();
    h.chunk("A"); h.chunk("B");
    h.client.commit(); // released before the socket opened
    await flush();
    h.sockets[0].open();
    await opening;
    expect(h.sockets[0].types()).toEqual([
      "session.update", "input_audio_buffer.append", "input_audio_buffer.append", "input_audio_buffer.commit"]);
    expect(h.sockets[0].sent.slice(1, 3).map((e) => e.audio)).toEqual(["A", "B"]);
  });

  it("streams chunks straight through when open, and a discard clears the server buffer", async () => {
    const h = setup();
    const s = await open(h);
    await h.client.startMic();
    h.chunk("X");
    await h.client.stopMic(true);
    expect(s.types()).toEqual(["session.update", "input_audio_buffer.append", "input_audio_buffer.clear"]);
    h.chunk("late"); // mic is off: nothing more is sent
    expect(s.types()).toHaveLength(3);
  });

  it("emits the transcript, then per-response instructions go out with response.create", async () => {
    const h = setup();
    const s = await open(h);
    s.recv({ type: "conversation.item.input_audio_transcription.completed", transcript: "who is she?" });
    expect(h.events).toContainEqual({ type: "transcript", text: "who is she?" });
    h.client.respond("You are Harry.");
    const rc = s.sent.at(-1)!;
    expect(rc.type).toBe("response.create");
    expect(rc.response.instructions).toBe("You are Harry.");
  });

  it("plays audio, emits audio-start once, captions, and drains when playback ends", async () => {
    const h = setup();
    const s = await open(h);
    h.client.respond("x");
    s.recv({ type: "response.created", response: { id: "r1" } });
    expect(h.player.resets).toBe(1);
    s.recv({ type: "response.output_item.added", item: { id: "item1", type: "message" } });
    s.recv({ type: "response.output_audio.delta", item_id: "item1", delta: "AAA" });
    s.recv({ type: "response.output_audio.delta", item_id: "item1", delta: "BBB" });
    s.recv({ type: "response.output_audio_transcript.delta", delta: "Hello " });
    s.recv({ type: "response.output_audio_transcript.delta", delta: "there." });
    s.recv({ type: "response.output_audio_transcript.done", transcript: "Hello there." });
    expect(h.player.enqueued).toEqual(["AAA", "BBB"]);
    expect(h.events.filter((e) => e.type === "audio-start")).toHaveLength(1);
    expect(h.events).toContainEqual({ type: "caption", text: "Hello there.", done: true });
    s.recv({ type: "response.done", response: { status: "completed" } });
    expect(h.events.some((e) => e.type === "playback-drained")).toBe(false); // still speaking
    h.player.onSpeaking(false);
    expect(h.events.filter((e) => e.type === "playback-drained")).toHaveLength(1);
  });

  it("a response with no audio drains immediately", async () => {
    const h = setup();
    const s = await open(h);
    h.client.respond("x");
    s.recv({ type: "response.done", response: { status: "completed" } });
    expect(h.events.map((e) => e.type)).toEqual(["response-done", "playback-drained"]);
  });

  it("interrupt truncates at the played position, cancels, and waits for the ack", async () => {
    const h = setup();
    const s = await open(h);
    h.client.respond("x");
    s.recv({ type: "response.created", response: { id: "r1" } });
    s.recv({ type: "response.output_audio.delta", item_id: "item9", delta: "AAA" });
    h.player.playedMs = 1234;
    const p = h.client.interrupt();
    await flush();
    const types = s.types();
    expect(s.sent.find((e) => e.type === "conversation.item.truncate")).toMatchObject({
      item_id: "item9", content_index: 0, audio_end_ms: 1234 });
    expect(types.indexOf("conversation.item.truncate")).toBeLessThan(types.indexOf("response.cancel"));
    s.recv({ type: "response.done", response: { status: "cancelled" } });
    await p;
    // a new question can now be committed without hitting voice_output_task_ongoing
    h.client.commit();
    expect(s.types().at(-1)).toBe("input_audio_buffer.commit");
  });

  it("interrupt with nothing playing sends nothing and does not hang", async () => {
    const h = setup();
    const s = await open(h);
    const before = s.sent.length;
    await h.client.interrupt();
    expect(s.sent.length).toBe(before);
  });

  it("benign 'response_not_active' errors are swallowed, real ones surface", async () => {
    const h = setup();
    const s = await open(h);
    s.recv({ type: "error", error: { type: "invalid_request_error", code: "response_not_active", message: "none" } });
    expect(h.events).toEqual([]);
    s.recv({ type: "error", error: { type: "insufficient_quota", code: "monthly_cap_reached", message: "Cap reached" } });
    expect(h.events).toEqual([{ type: "error", message: "Cap reached" }]);
  });
});

describe("TalkClient voice changes", () => {
  async function open(h: ReturnType<typeof setup>) {
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
  }

  it("changes voice in place when the server accepts session.update", async () => {
    const h = setup();
    await open(h);
    const p = h.client.setVoice("oliver");
    await flush();
    expect(h.sockets[0].sent.at(-1)).toMatchObject({ type: "session.update", session: { audio: { output: { voice: "oliver" } } } });
    h.sockets[0].recv({ type: "session.updated", session: {} });
    await p;
    expect(h.sockets).toHaveLength(1);
  });

  it("reconnects with the new voice when the server rejects a mid-session change", async () => {
    const h = setup();
    await open(h);
    const p = h.client.setVoice("oliver");
    await flush();
    h.sockets[0].recv({ type: "error", error: { type: "invalid_request_error", message: "voice cannot change" } });
    await flush();
    h.sockets[1]?.open();
    await p;
    expect(h.sockets).toHaveLength(2);
    expect(h.sockets[1].sent[0].session.audio.output.voice).toBe("oliver");
  });

  it("ensureOpen with a different voice while open switches voice instead of opening a second socket", async () => {
    const h = setup();
    await open(h);
    const p = h.client.ensureOpen("jake");
    await flush();
    h.sockets[0].recv({ type: "session.updated", session: {} });
    await p;
    expect(h.sockets).toHaveLength(1);
  });
});

beforeEach(() => { /* real timers: the client only uses short waits in tests */ });
afterEach(() => vi.restoreAllMocks());

describe("TalkClient reply audio capture", () => {
  it("hands every PCM chunk to onAudioChunk, flagging the first of a response", async () => {
    const got: [string, boolean][] = [];
    const h = setup({ onAudioChunk: (b: string, first: boolean) => got.push([b, first]) });
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    h.sockets[0].recv({ type: "response.created", response: {} });
    h.sockets[0].recv({ type: "response.output_audio.delta", item_id: "i", delta: "AAA" });
    h.sockets[0].recv({ type: "response.output_audio.delta", item_id: "i", delta: "BBB" });
    expect(got).toEqual([["AAA", true], ["BBB", false]]);
  });
});

describe("TalkClient history on a fresh session", () => {
  it("puts the last turns back as text items after the session config, never audio, and tolerates their errors", async () => {
    const turns = [{ role: "user" as const, text: "Who is Hagrid?" }, { role: "character" as const, text: "A big fellow." }];
    const h = setup({ historyProvider: () => turns });
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    const sent = h.sockets[0].sent;
    expect(sent.map((e) => e.type)).toEqual(["session.update", "conversation.item.create", "conversation.item.create"]);
    expect(sent[1].item).toEqual({ type: "message", role: "user", content: [{ type: "input_text", text: "Who is Hagrid?" }] });
    expect(sent[2].item.role).toBe("assistant");
    expect(JSON.stringify(sent)).not.toContain("input_audio");
    h.sockets[0].recv({ type: "error", error: { type: "invalid_request_error", message: "bad item", event_id: "seed-1" } });
    expect(h.events.filter((e) => e.type === "error")).toHaveLength(0);
  });

  it("seeds again after a reconnect and an emptied chat seeds nothing; resetConversation drops the socket", async () => {
    let turns = [{ role: "user" as const, text: "Hi" }];
    const h = setup({ historyProvider: () => turns });
    let p = h.client.ensureOpen("marcus");
    await flush(); h.sockets[0].open(); await p;
    h.client.resetConversation();
    turns = [];
    p = h.client.ensureOpen("marcus");
    await flush(); h.sockets[1].open(); await p;
    expect(h.sockets[1].types()).toEqual(["session.update"]);
    expect(h.tokens()).toBe(2);
  });
});
