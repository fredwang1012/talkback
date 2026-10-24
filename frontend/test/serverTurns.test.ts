/**
 * #135: hands-free on Boson's own turn detection (semantic_vad, create_response false), with the local voice gate as
 * the echo guard and the fallback; voice-timed captions; the heard cut on barge-in; fast "didn't catch that".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkClient, captionAt, sessionConfig, turnDetection, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import { FAST_FAIL_MS, TalkController, type ReaderControl, type SessionEvent, type TalkState } from "../src/talk/TalkController";
import { localGateForced } from "../src/talk/handsFree";
import { addQuestion, cutHeard, markInterrupted, setPendingText } from "../src/talk/chatLog";

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
  played = 0;
  log: string[] = [];
  onSpeaking: (s: boolean) => void = () => {};
  async resume() {}
  enqueueBase64() { this.log.push("enqueue"); }
  async flush() { this.log.push("flush"); return this.played; }
  playedMs() { return this.played; }
  reset() { this.log.push("reset"); }
  async close() {}
}
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await vi.advanceTimersByTimeAsync(0); };

function makeClient(timed = true) {
  const sockets: FakeSocket[] = [];
  let micChunk: (b64: string) => void = () => {};
  const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };
  const player = new FakePlayer();
  const p: PlayerLike = timed ? player : { resume: async () => {}, enqueueBase64: () => {}, flush: async () => player.played, reset: () => {}, close: async () => {} };
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: (onSpeaking) => { player.onSpeaking = onSpeaking; return p; },
    createMic: (onChunk) => { micChunk = onChunk; return mic; },
    cancelWaitMs: 50, voiceChangeWaitMs: 50, backoffMs: [],
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  return { client, sockets, player, mic, events, chunk: (b: string) => micChunk(b), ws: () => sockets[sockets.length - 1] };
}
async function open(h: ReturnType<typeof makeClient>, voice = "marcus") {
  const p = h.client.ensureOpen(voice);
  await settle();
  h.ws().open();
  await p;
}
const td = (ws: FakeSocket) => ws.sent.find((e) => e.type === "session.update")?.session.audio.input.turn_detection;
const ack = (type: string, extra: Record<string, unknown> = {}) => ({
  type: "session.created",
  session: { audio: { input: { turn_detection: { type, create_response: false, interrupt_response: false, ...extra }, transcription: { model: "higgs-stt-3.1", language: null } } } },
});

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

// ---- TalkClient -----------------------------------------------------------------------------------------------

describe("TalkClient server turn detection (#135)", () => {
  it("push-to-talk sessions stay manual; a hands-free session asks for semantic_vad that never answers by itself", () => {
    expect((sessionConfig("v") as any).session.audio.input.turn_detection).toBeNull();
    const t = turnDetection("semantic_vad");
    expect(t).toMatchObject({ type: "semantic_vad", create_response: false, interrupt_response: false });
    const cfg = sessionConfig("v", [], { turnDetection: t }) as any;
    expect(cfg.session.audio.input.turn_detection).toEqual(t);
    // live: near_field noise reduction under server VAD started the turn late and lost the first words ("France."
    // for "What is the capital of France?"); without it the whole question came through. PTT keeps it.
    expect(cfg.session.audio.input.noise_reduction).toBeUndefined();
    expect((sessionConfig("v") as any).session.audio.input.noise_reduction).toEqual({ type: "near_field" });
  });

  it("turn_detection cannot change mid-session (live 400): switching modes drops the socket and the next open uses the new config", async () => {
    const h = makeClient();
    await open(h);
    expect(td(h.ws())).toBeNull();
    h.client.setServerTurns(true);
    expect(h.sockets[0].readyState).toBe(3);
    await open(h);
    expect(h.sockets).toHaveLength(2);
    expect(td(h.ws())).toMatchObject({ type: "semantic_vad", create_response: false });
    h.client.setServerTurns(false);
    await open(h);
    expect(td(h.ws())).toBeNull();
  });

  it("while monitoring in server mode the mic streams continuously; pushed audio before the socket opens is kept briefly", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await h.client.setMonitor(true);
    for (let i = 0; i < 40; i++) h.chunk(btoa(`c${i}`.padEnd(4800, "\0"))); // 4 s before the socket is open
    await open(h);
    const appended = h.ws().sent.filter((e) => e.type === "input_audio_buffer.append");
    expect(appended.length).toBeGreaterThan(0);
    expect(appended.length).toBeLessThanOrEqual(30); // capped (about 3 s), newest kept
    expect(atob(appended.at(-1)!.audio).startsWith("c39")).toBe(true);
    h.chunk(btoa("live".padEnd(4800, "\0")));
    expect(h.ws().sent.at(-1)?.type).toBe("input_audio_buffer.append");
  });

  it("emits the VAD events with their item ids", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u1", audio_start_ms: 500 });
    h.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u1", audio_end_ms: 1400 });
    h.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    h.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Who?" });
    expect(h.events).toEqual([
      { type: "speech-started", itemId: "u1" }, { type: "speech-stopped" }, { type: "committed", itemId: "u1" },
      { type: "transcript", text: "Who?", itemId: "u1" },
    ]);
  });

  it("falls back semantic_vad -> server_vad -> manual when the server rejects the turn detection", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.ws().recv({ type: "error", error: { type: "invalid_request_error", message: "unsupported turn_detection type semantic_vad" } });
    await settle(); // the client dropped that socket itself and opens the next one
    h.ws().open();
    await settle();
    expect(h.sockets).toHaveLength(2);
    expect(td(h.ws())).toMatchObject({ type: "server_vad", create_response: false });
    h.ws().recv(ack("semantic_vad")); // the echo is not what was asked for: no server turns at all
    await settle();
    h.ws().open();
    await settle();
    expect(h.sockets).toHaveLength(3);
    expect(td(h.ws())).toBeNull();
    expect(h.events).toContainEqual({ type: "turn-mode", server: false });
    expect(h.events.filter((e) => e.type === "error")).toHaveLength(0);
  });

  it("an ack that would answer by itself (create_response true) goes straight back to manual turns", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.ws().recv(ack("semantic_vad", { create_response: true }));
    await settle();
    h.ws().open();
    await settle();
    expect(td(h.ws())).toBeNull();
    expect(h.events).toContainEqual({ type: "turn-mode", server: false });
  });

  it("an answer the server starts on its own in server mode is cancelled and never played", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.ws().recv(ack("semantic_vad"));
    h.ws().recv({ type: "response.created", response: { id: "r0" } });
    h.ws().recv({ type: "response.output_audio.delta", item_id: "a", delta: "AAAA" });
    expect(h.ws().types()).toContain("response.cancel");
    expect(h.player.log).not.toContain("enqueue");
    expect(h.events.some((e) => e.type === "audio-start")).toBe(false);
    // our own response afterwards plays normally
    h.client.respond("INSTR");
    h.ws().recv({ type: "response.created", response: { id: "r1" } });
    h.ws().recv({ type: "response.output_audio.delta", item_id: "b", delta: "BBBB" });
    expect(h.player.log).toContain("enqueue");
  });

  it("deleteItem removes an echo turn from the conversation", async () => {
    const h = makeClient();
    await open(h);
    h.client.deleteItem("u9");
    expect(h.ws().sent.at(-1)).toEqual({ type: "conversation.item.delete", item_id: "u9" });
  });

  it("language hint: in the session config, and sent mid-session when it changes", async () => {
    const h = makeClient();
    h.client.setLanguageHint("es");
    await open(h);
    expect(h.ws().sent[0].session.audio.input.transcription).toEqual({ model: "higgs-stt-3.1", language: "es" });
    const n = h.ws().sent.length;
    h.client.setLanguageHint("es");
    expect(h.ws().sent.length).toBe(n); // unchanged: nothing sent
    h.client.setLanguageHint(null);
    expect(h.ws().sent.at(-1)).toEqual({ type: "session.update", session: { audio: { input: { transcription: { model: "higgs-stt-3.1", language: null } } } } });
    expect(h.sockets).toHaveLength(1); // transcription can change mid-session (live: session.updated echoes it)
  });
});

describe("voice-timed captions (#135)", () => {
  it("captionAt reveals the fragments whose audio has started, word by word inside the current one", () => {
    const segs = [{ text: "Hello there!", start: 0, end: 1000 }, { text: " How are you today?", start: 1000, end: 3000 }];
    expect(captionAt(segs, 0)).toBe("");
    expect(captionAt(segs, 10)).toBe("Hello");
    expect(captionAt(segs, 999)).toBe("Hello there!");
    expect(captionAt(segs, 1600)).toBe("Hello there! How are");
    expect(captionAt(segs, 5000)).toBe("Hello there! How are you today?");
  });

  async function answering(timed = true) {
    const h = makeClient(timed);
    await open(h);
    h.client.respond("INSTR");
    h.ws().recv({ type: "response.created", response: { id: "r1" } });
    h.ws().recv({ type: "response.output_item.added", item: { id: "a1", type: "message" } });
    h.ws().recv({ type: "response.output_audio.delta", item_id: "a1", delta: "AAAA" });
    // live order: each fragment's .delta, then its .length with the audio duration of that fragment
    h.ws().recv({ type: "response.output_audio_transcript.delta", delta: "Hello!" });
    h.ws().recv({ type: "response.output_audio_transcript.length", delta: "Hello!", length_ms: 840 });
    h.ws().recv({ type: "response.output_audio_transcript.delta", delta: " How can I help you today?" });
    h.ws().recv({ type: "response.output_audio_transcript.length", delta: " How can I help you today?", length_ms: 2680 });
    h.ws().recv({ type: "response.output_audio_transcript.done", transcript: "Hello! How can I help you today?" });
    h.ws().recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    return h;
  }
  const captions = (h: ReturnType<typeof makeClient>) => h.events.filter((e): e is Extract<SessionEvent, { type: "caption" }> => e.type === "caption");

  it("the caption follows the audio being played, not the text arriving", async () => {
    const h = await answering();
    expect(captions(h).filter((c) => c.text)).toEqual([]); // all the text is here, none of it has been heard
    h.player.played = 500;
    await vi.advanceTimersByTimeAsync(200);
    expect(captions(h).at(-1)).toEqual({ type: "caption", text: "Hello!", done: false });
    h.player.played = 2200;
    await vi.advanceTimersByTimeAsync(200);
    expect(captions(h).at(-1)?.text).toBe("Hello! How can I help");
    h.player.played = 3520;
    h.player.onSpeaking(false); // drained
    expect(captions(h).at(-1)).toEqual({ type: "caption", text: "Hello! How can I help you today?", done: true });
    expect(h.events.at(-1)).toEqual({ type: "playback-drained" });
  });

  it("without a playback clock the caption shows the text as it arrives (as before)", async () => {
    const h = await answering(false);
    expect(captions(h).map((c) => c.text)).toEqual(["Hello!", "Hello! How can I help you today?", "Hello! How can I help you today?"]);
  });

  it("a barge-in reports what was actually heard, then nothing more of the old answer", async () => {
    const h = await answering();
    h.player.played = 1500; // into the second fragment
    const p = h.client.interrupt();
    await settle();
    await p;
    expect(h.events).toContainEqual({ type: "heard", text: "Hello! How can" });
    const n = captions(h).length;
    await vi.advanceTimersByTimeAsync(500);
    expect(captions(h).length).toBe(n);
  });
});

describe("chat: the interrupted answer keeps only the words heard (#135)", () => {
  it("cutHeard trims the interrupted reply, also once a typed question opened a new one", () => {
    let m = addQuestion([], "Who is Alice?", "alice", "voice");
    m = setPendingText(m, "A girl who fell down a rabbit hole.");
    m = markInterrupted(m);
    m = addQuestion(m, "And then?", "alice", "typed"); // ask(): the new question comes first
    m = cutHeard(m, "A girl who");
    expect(m[1]).toMatchObject({ text: "A girl who", interrupted: true });
    expect(m[3]).toMatchObject({ text: "", pending: true });
  });
  it("Stop (no barge-in mark) trims the pending reply; an empty heard text keeps it", () => {
    let m = addQuestion([], "Who?", "alice", "voice");
    m = setPendingText(m, "A girl who fell.");
    expect(cutHeard(m, "")[1].text).toBe("A girl who fell.");
    expect(cutHeard(m, "A girl")[1].text).toBe("A girl");
  });
});

describe("feature flag: force the local gate", () => {
  it("?gate=local or localStorage talkback:gate=local", () => {
    expect(localGateForced("?gate=local", null)).toBe(true);
    expect(localGateForced("", "local")).toBe(true);
    expect(localGateForced("?x=1", null)).toBe(false);
    expect(localGateForced("", "server")).toBe(false);
  });
});

// ---- TalkController over the real TalkClient -------------------------------------------------------------------

function loop(opts: { tapMs?: number } = {}) {
  let now = 0;
  const h = makeClient();
  const reader: ReaderControl & { log: string[]; playing: boolean } = {
    log: [], playing: true,
    isPlaying() { return this.playing; }, currentIndex: () => 4, currentPara: () => 2,
    pause() { this.playing = false; this.log.push("pause"); }, play() { this.playing = true; this.log.push("play"); },
    resumeAt(i: number) { this.playing = true; this.log.push(`resumeAt:${i}`); },
  };
  const states: TalkState[] = [];
  const toasts: string[] = [];
  const asked: string[] = [];
  const transcripts: string[] = [];
  let interrupted = 0;
  const heard: string[] = [];
  const talk = new TalkController({
    session: h.client, reader, serverTurns: true,
    retrieve: async (q) => { asked.push(q); return { instructions: "INSTR" }; },
    voiceFor: () => "marcus", now: () => now, tapMs: opts.tapMs ?? 300,
    onState: (s) => states.push(s), onToast: (t) => toasts.push(t), onTranscript: (t) => transcripts.push(t),
    onInterrupted: () => { interrupted++; }, onHeard: (t) => heard.push(t),
  });
  const advance = async (ms: number) => { now += ms; await vi.advanceTimersByTimeAsync(ms); };
  const level = async (rms: number, ms: number) => { for (let t = 0; t < ms; t += 20) { talk.noteLevel(rms); await advance(20); } };
  return { ...h, talk, reader, states, toasts, asked, transcripts, heard, interrupted: () => interrupted, advance, level };
}
async function handsFreeOn(l: ReturnType<typeof loop>) {
  const p = l.talk.setHandsFree(true);
  await settle();
  l.ws().open();
  await p;
  await settle();
  l.ws().recv(ack("semantic_vad"));
}
async function speakTurn(l: ReturnType<typeof loop>, text: string, id = "u1") {
  l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: id });
  await settle();
  l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: id });
  l.ws().recv({ type: "input_audio_buffer.committed", item_id: id });
  await l.advance(250);
  l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: id, transcript: text });
  await l.advance(700); // past the merge window
  await settle();
}
async function characterSpeaks(l: ReturnType<typeof loop>) {
  l.ws().recv({ type: "response.created", response: { id: "r1" } });
  l.ws().recv({ type: "response.output_item.added", item: { id: "a1", type: "message" } });
  l.ws().recv({ type: "response.output_audio.delta", item_id: "a1", delta: "AAAA" });
  l.ws().recv({ type: "response.output_audio_transcript.length", delta: "A girl who fell.", length_ms: 2000 });
  l.ws().recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
  await settle();
}

describe("TalkController on Boson turn detection (#135)", () => {
  it("hands-free opens a semantic_vad session at once; the server's turn runs our normal flow and we send response.create", async () => {
    const l = loop();
    await handsFreeOn(l);
    expect(td(l.ws())).toMatchObject({ type: "semantic_vad", create_response: false });
    expect(l.talk.serverTurnsActive).toBe(true);
    const startsBefore = (l.mic.start as any).mock.calls.length;
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u1" });
    await settle();
    expect(l.talk.state).toBe("listening");
    expect(l.reader.log).toEqual(["pause"]);
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u1" });
    expect(l.talk.state).toBe("transcribing");
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    await l.advance(250);
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Who is Alice?" });
    await l.advance(700);
    await settle();
    expect(l.asked).toEqual(["Who is Alice?"]);
    expect(l.transcripts).toEqual(["Who is Alice?"]);
    const create = l.ws().sent.filter((e) => e.type === "response.create");
    expect(create).toHaveLength(1);
    expect(create[0].response.instructions).toBe("INSTR");
    expect(l.ws().types()).not.toContain("input_audio_buffer.commit"); // the server committed the turn
    expect((l.mic.start as any).mock.calls.length).toBe(startsBefore); // the mic was already streaming
  });

  it("a pause mid-question (the server splits it) is joined into one question", async () => {
    const l = loop();
    await handsFreeOn(l);
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    await l.advance(200);
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "What is the" });
    await l.advance(100);
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u2" });
    await settle();
    expect(l.talk.state).toBe("listening");
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u2" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u2" });
    await l.advance(200);
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u2", transcript: "capital of France?" });
    await l.advance(700);
    await settle();
    expect(l.asked).toEqual(["What is the capital of France?"]);
  });

  it("barge-in: the server's speech_started plus clear local speech cuts the character off and keeps the words heard", async () => {
    const l = loop();
    await handsFreeOn(l);
    await speakTurn(l, "Who is Alice?");
    await characterSpeaks(l);
    expect(l.talk.state).toBe("answering");
    l.player.played = 1000;
    await l.level(0.2, 500); // the reader speaks over the character, clearly
    expect(l.talk.state).toBe("answering"); // the local level alone never starts a server-mode turn
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u2" });
    await settle();
    expect(l.talk.state).toBe("listening");
    expect(l.interrupted()).toBe(1);
    expect(l.ws().types()).toContain("conversation.item.truncate");
    expect(l.heard).toEqual(["A girl"]);
  });

  it("echo guard: speech_started over the character without clear local speech is ignored and its turn deleted", async () => {
    const l = loop();
    await handsFreeOn(l);
    await speakTurn(l, "Who is Alice?");
    await characterSpeaks(l);
    await l.level(0.06, 300); // what the echo canceller leaves of the character
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "echo1" });
    await l.level(0.06, 400);
    expect(l.talk.state).toBe("answering");
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "echo1" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "echo1" });
    await settle();
    expect(l.ws().sent.at(-1)).toEqual({ type: "conversation.item.delete", item_id: "echo1" });
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "echo1", transcript: "A girl who" });
    await l.advance(2000);
    expect(l.asked).toEqual(["Who is Alice?"]);
    expect(l.interrupted()).toBe(0);
  });

  it("echo guard: speech the server heard first is confirmed once the local level is clearly speech", async () => {
    const l = loop();
    await handsFreeOn(l);
    await speakTurn(l, "Who is Alice?");
    await characterSpeaks(l);
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u2" });
    await settle();
    expect(l.talk.state).toBe("answering");
    await l.level(0.2, 500);
    expect(l.talk.state).toBe("listening");
    expect(l.interrupted()).toBe(1);
  });

  it("fast failure: a committed turn without a transcript ends in well under the old 6 s", async () => {
    const l = loop();
    await handsFreeOn(l);
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    await l.advance(FAST_FAIL_MS - 100);
    expect(l.toasts).toEqual([]);
    await l.advance(200);
    expect(l.toasts).toEqual(["I didn't catch that. Try again."]);
    expect(FAST_FAIL_MS).toBeLessThanOrEqual(1500);
    expect(l.talk.state).toBe("reading");
    expect(l.reader.log.at(-1)).toBe("resumeAt:4");
  });

  it("fast failure: an empty transcript ends the turn at once", async () => {
    const l = loop();
    await handsFreeOn(l);
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u1" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "  " });
    await l.advance(700);
    expect(l.toasts).toEqual(["I didn't catch that. Try again."]);
    expect(l.ws().types()).not.toContain("response.create");
  });

  it("fallback: when the server mode is refused, the local voice gate drives hands-free again", async () => {
    const l = loop();
    await handsFreeOn(l);
    l.ws().recv({ type: "session.created", session: { audio: { input: { turn_detection: { type: "semantic_vad", create_response: true } } } } });
    await settle();
    l.ws().open();
    await settle();
    expect(td(l.ws())).toBeNull();
    expect(l.talk.serverTurnsActive).toBe(false);
    await l.level(0.2, 300);
    expect(l.talk.state).toBe("listening"); // the old gate started the turn
    await l.level(0.01, 800);
    expect(l.talk.state).toBe("transcribing");
    expect(l.ws().types()).toContain("input_audio_buffer.commit");
  });

  it("a forced local gate never asks for server turns", async () => {
    const h = makeClient();
    const reader: ReaderControl = { isPlaying: () => false, currentIndex: () => 0, currentPara: () => 0, pause() {}, play() {}, resumeAt() {} };
    const talk = new TalkController({ session: h.client, reader, serverTurns: false, retrieve: async () => ({ instructions: "I" }), voiceFor: () => "m" });
    await talk.setHandsFree(true);
    expect(h.sockets).toHaveLength(0); // nothing opened until a turn starts (the old behaviour)
    expect(talk.serverTurnsActive).toBe(false);
  });

  it("push-to-talk keeps the full transcript window after committed (a long question is not cut at 1.5 s)", async () => {
    const l = loop();
    await l.talk.pttDown();
    await settle();
    l.ws().open();
    await l.advance(600);
    const up = l.talk.pttUp();
    await settle();
    expect(l.ws().types()).toContain("input_audio_buffer.commit");
    expect(td(l.ws())).toBeNull();
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    await l.advance(FAST_FAIL_MS + 1000);
    expect(l.toasts).toEqual([]); // still waiting: the fast failure is for server turns only
    l.ws().recv({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "A long question?" });
    await settle();
    await up;
    expect(l.asked).toEqual(["A long question?"]);
  });

  it("push-to-talk without any transcript still fails after the full window", async () => {
    const l = loop();
    await l.talk.pttDown();
    await settle();
    l.ws().open();
    await l.advance(600);
    const up = l.talk.pttUp();
    await settle();
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u1" });
    await l.advance(6100);
    await up;
    expect(l.toasts).toEqual(["I didn't catch that. Try again."]);
  });

  it("language hint: clearly Spanish questions set the transcription language; a failed turn clears it", async () => {
    const l = loop();
    await handsFreeOn(l);
    await speakTurn(l, "¿Quién escribió esta carta y por qué?");
    expect(l.ws().sent).toContainEqual({ type: "session.update", session: { audio: { input: { transcription: { model: "higgs-stt-3.1", language: "es" } } } } });
    l.ws().recv({ type: "response.created", response: { id: "r1" } });
    l.ws().recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    await settle();
    l.ws().recv({ type: "input_audio_buffer.speech_started", item_id: "u2" });
    l.ws().recv({ type: "input_audio_buffer.speech_stopped", item_id: "u2" });
    l.ws().recv({ type: "input_audio_buffer.committed", item_id: "u2" });
    await l.advance(FAST_FAIL_MS + 100); // live: a wrong hint can swallow the transcript entirely
    expect(l.ws().sent.at(-1)).toEqual({ type: "session.update", session: { audio: { input: { transcription: { model: "higgs-stt-3.1", language: null } } } } });
  });
});

// ---- #141 review: a mode switch mid-answer, held audio after hands-free goes off ---------------------------------

async function pttAnswerPlaying(l: ReturnType<typeof loop>) {
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
  await settle();
  await characterSpeaks(l);
  expect(l.talk.state).toBe("answering");
}

describe("turn-mode change while an answer plays (#141 review)", () => {
  it("hands-free on mid-answer: the answer finishes, the turn ends normally, then a semantic_vad session opens", async () => {
    const l = loop();
    await pttAnswerPlaying(l);
    await l.talk.setHandsFree(true);
    await settle();
    expect(l.sockets).toHaveLength(1);
    expect(l.sockets[0].readyState).toBe(1); // not dropped under the playing answer
    l.player.onSpeaking(false); // the answer drained
    await settle();
    expect(l.talk.state).not.toBe("answering");
    expect(["resuming", "reading"]).toContain(l.talk.state);
    expect(l.sockets[0].readyState).toBe(3);
    expect(l.sockets).toHaveLength(2);
    l.ws().open();
    await settle();
    expect(td(l.ws())).toMatchObject({ type: "semantic_vad" });
    await l.advance(2000);
    expect(l.reader.log.at(-1)).toBe("resumeAt:4"); // auto-resume still works
  });

  it("hands-free off mid-answer: the answer finishes and the turn ends; the next press uses a manual session", async () => {
    const l = loop();
    await handsFreeOn(l);
    await speakTurn(l, "Who is Alice?");
    await characterSpeaks(l);
    expect(l.talk.state).toBe("answering");
    await l.talk.setHandsFree(false);
    await settle();
    expect(l.sockets[0].readyState).toBe(1);
    l.player.onSpeaking(false);
    await settle();
    expect(l.talk.state).not.toBe("answering");
    expect(l.sockets[0].readyState).toBe(3);
    await l.talk.pttDown();
    await settle();
    l.ws().open();
    await settle();
    expect(td(l.ws())).toBeNull();
  });

  it("a refused turn detection during an answer waits for the answer to drain", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.client.respond("INSTR");
    h.ws().recv({ type: "response.created", response: { id: "r1" } });
    h.ws().recv({ type: "response.output_audio.delta", item_id: "a", delta: "AAAA" });
    h.ws().recv({ type: "response.done", response: { id: "r1", status: "completed", output: [] } });
    h.ws().recv({ type: "error", error: { type: "invalid_request_error", message: "unsupported turn_detection type semantic_vad" } });
    await settle();
    expect(h.sockets).toHaveLength(1);
    expect(h.sockets[0].readyState).toBe(1);
    h.player.onSpeaking(false);
    await settle();
    expect(h.events).toContainEqual({ type: "playback-drained" });
    expect(h.sockets[0].readyState).toBe(3);
    expect(h.sockets).toHaveLength(2);
    h.ws().open();
    await settle();
    expect(td(h.ws())).toMatchObject({ type: "server_vad" });
  });

  it("audio held in server mode is dropped when hands-free goes off: the next press sends only its own audio", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await h.client.setMonitor(true);
    for (let i = 0; i < 10; i++) h.chunk(btoa(`old${i}`.padEnd(4800, " "))); // held: no socket yet
    h.client.setServerTurns(false);
    await h.client.setMonitor(false);
    await h.client.startMic();
    h.chunk(btoa("new".padEnd(4800, " ")));
    await open(h);
    const appended = h.ws().sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => atob(e.audio));
    expect(appended.some((a) => a.startsWith("old"))).toBe(false);
    expect(appended.some((a) => a.startsWith("new"))).toBe(true);
  });

  it("audio held while a socket connects in server mode is dropped; that socket then opens as a manual one", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await h.client.setMonitor(true);
    const p = h.client.ensureOpen("marcus");
    await settle(); // connecting: not open yet
    for (let i = 0; i < 5; i++) h.chunk(btoa(`old${i}`.padEnd(4800, " ")));
    h.client.setServerTurns(false);
    await h.client.setMonitor(false);
    await h.client.startMic();
    h.chunk(btoa("new".padEnd(4800, " ")));
    h.ws().open();
    await p;
    expect(h.sockets).toHaveLength(1);
    expect(td(h.ws())).toBeNull(); // the config is chosen when it opens
    const appended = h.ws().sent.filter((e) => e.type === "input_audio_buffer.append").map((e) => atob(e.audio));
    expect(appended.some((a) => a.startsWith("new"))).toBe(true);
    expect(appended.some((a) => a.startsWith("old"))).toBe(false);
  });

  it("a socket dropped while it connects never leaves the open pending: the next one opens with the new config", async () => {
    const h = makeClient();
    h.client.setServerTurns(true);
    await open(h);
    h.ws().close(1000, "idle timeout"); // the server session ended; the client remembers it was a server one
    const p = h.client.ensureOpen("marcus");
    await settle(); // connecting
    h.client.setServerTurns(false); // drops the connecting socket
    await settle();
    expect(h.sockets).toHaveLength(3);
    h.ws().open();
    await p;
    expect(td(h.ws())).toBeNull();
    expect(h.client.isOpen).toBe(true);
  });
});
