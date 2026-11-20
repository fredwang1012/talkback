/**
 * #72: Realtime function tools through a fake socket (TalkClient) and the talk loop (TalkController): the model's
 * call -> reader action -> function_call_output -> response.create, the spoiler guard, and the Gemini-path intent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import {
  TalkController, type ReaderControl, type SessionEvent, type TalkSession, type TalkState, type ToolResult,
} from "../src/talk/TalkController";
import { runTool, type ToolActions, type ToolContext, type ToolOutcome } from "../src/talk/tools";

// ---- the fake socket ----

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
  async resume() {}
  enqueueBase64() {}
  async flush() { return this.playedMs; }
  reset() {}
  async close() {}
}
const flush = () => new Promise((r) => setTimeout(r, 0));

async function openClient(extra: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const mic: MicLike = { start: async () => {}, stop: async () => {} };
  const player = new FakePlayer();
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: () => mic, cancelWaitMs: 20, ...extra,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  return { client, ws: sockets[0], events, player };
}

const functionCallDone = (ws: FakeSocket, call: { call_id: string; name: string; arguments: string; executor?: string }, status = "completed") => {
  ws.recv({ type: "response.created", response: { id: "r1" } });
  ws.recv({ type: "response.output_item.added", item: { id: "fc1", type: "function_call", call_id: call.call_id, name: call.name } });
  ws.recv({ type: "response.function_call_arguments.done", response_id: "r1", item_id: "fc1", call_id: call.call_id, name: call.name, arguments: call.arguments });
  ws.recv({ type: "response.done", response: { id: "r1", status, output: [{ type: "function_call", status: "completed", ...call }] } });
};

describe("TalkClient tools (fake socket)", () => {
  it("declares the client function tools in session.update; the default (novel) set has no web_search", async () => {
    const { ws } = await openClient();
    const cfg = ws.sent[0];
    expect(cfg.type).toBe("session.update");
    expect(cfg.session.tool_choice).toBe("auto");
    const names = cfg.session.tools.map((t: { name: string; type: string }) => `${t.type}:${t.name}`);
    expect(names).toContain("function:jump_to");
    expect(names).toContain("function:read_chapter");
    expect(JSON.stringify(cfg.session.tools)).not.toContain("web_search"); // webSearch.sessionTools adds it for textbooks / spoilers on
  });

  it("a response ending in a client function call becomes a tool-call event, not a finished answer", async () => {
    const { client, ws, events } = await openClient();
    client.respond("Be Alice.");
    functionCallDone(ws, { call_id: "c1", name: "jump_to", arguments: JSON.stringify({ chapter: "2" }), executor: "client" });
    expect(events).toEqual([{ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] }]);
    expect(events.some((e) => e.type === "response-done" || e.type === "playback-drained")).toBe(false);
  });

  it("returnTools sends function_call_output then response.create with the same instructions and the results as its input (live: the item is not seen)", async () => {
    const { client, ws } = await openClient();
    client.respond("Be Alice.", "faster please");
    functionCallDone(ws, { call_id: "c1", name: "set_speed", arguments: JSON.stringify({ speed: "faster" }), executor: "client" });
    client.returnTools([{ callId: "c1", output: "Reading speed set to 1.25x." }]);
    const tail = ws.sent.slice(-2);
    expect(tail[0]).toEqual({ type: "conversation.item.create", item: { type: "function_call_output", call_id: "c1", output: "Reading speed set to 1.25x." } });
    expect(tail[1].type).toBe("response.create");
    expect(tail[1].response.instructions).toBe("Be Alice.");
    expect(tail[1].response.input).toHaveLength(1);
    expect(tail[1].response.input[0].role).toBe("user"); // live: a `system` input role is refused
    expect(tail[1].response.input[0].content[0].text).toContain("- set_speed: Reading speed set to 1.25x.");
    expect(tail[1].response.input[0].content[0].text).toContain('"faster please"');
    // the continuation is an ordinary answer again
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed", output: [{ type: "message", role: "assistant" }] } });
    expect(ws.sent.filter((e) => e.type === "response.create")).toHaveLength(2);
  });

  it("a tool the server runs itself (executor != client) is not ours and does not end the turn (#4); malformed arguments become {}", async () => {
    const { client, ws, events } = await openClient();
    client.respond("x");
    functionCallDone(ws, { call_id: "s1", name: "web_search", arguments: "{}", executor: "server" });
    expect(events.filter((e) => e.type === "tool-call")).toHaveLength(0);
    expect(events.some((e) => e.type === "response-done" || e.type === "playback-drained")).toBe(false); // the server's answer is still to come
    expect(events).toContainEqual({ type: "searching", active: true });
    client.respond("x");
    functionCallDone(ws, { call_id: "c2", name: "bookmark", arguments: "not json", executor: "client" });
    expect(events.at(-1)).toEqual({ type: "tool-call", calls: [{ callId: "c2", name: "bookmark", args: {} }] });
  });

  it("a cancelled response (barge-in) drops its tool call", async () => {
    const { client, ws, events } = await openClient();
    client.respond("x");
    functionCallDone(ws, { call_id: "c3", name: "jump_to", arguments: "{}", executor: "client" }, "cancelled");
    expect(events.filter((e) => e.type === "tool-call")).toHaveLength(0);
    expect(events.some((e) => e.type === "response-done" && e.status === "cancelled")).toBe(true);
  });
});

// ---- the loop ----

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  returned: ToolResult[][] = [];
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); }
  async startMic() { this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(instructions: string) { this.calls.push(`respond:${instructions}`); }
  async interrupt() { this.calls.push("interrupt"); }
  returnTools(results: ToolResult[]) { this.calls.push("return-tools"); this.returned.push(results); }
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}
class FakeReader implements ReaderControl {
  playing: boolean;
  index: number;
  log: string[] = [];
  constructor(playing = true, index = 5) { this.playing = playing; this.index = index; }
  isPlaying() { return this.playing; }
  currentIndex() { return this.index; }
  currentPara() { return Math.floor(this.index / 2); }
  pause() { this.playing = false; this.log.push("pause"); }
  play() { this.playing = true; this.log.push("play"); }
  resumeAt(i: number) { this.playing = true; this.index = i; this.log.push(`resumeAt:${i}`); }
}

const sections = [{ id: "s1", title: "One", start_para: 0 }, { id: "s2", title: "Two", start_para: 4 }, { id: "s3", title: "Three", start_para: 8 }];
const paragraphs = Array.from({ length: 12 }, (_, i) => ({ idx: i, text: `p${i}` }));
const chunks = paragraphs.flatMap((p) => [{ para: p.idx }, { para: p.idx }]);

function setup(opts: { playing?: boolean; gemini?: boolean; spoilers?: boolean } = {}) {
  const session = new FakeSession();
  const reader = new FakeReader(opts.playing ?? true, 10); // paragraph 5: chapter Two reached, Three not
  const states: TalkState[] = [];
  const toasts: string[] = [];
  const chips: string[] = [];
  const actions: string[] = [];
  const ctx = (): ToolContext => ({
    bookId: "b", sections, paragraphs, characters: [], chunks, index: reader.index, readerPara: reader.currentPara(), spoilers: !!opts.spoilers,
    playing: reader.isPlaying(), speed: 1, speeds: [0.75, 1, 1.25, 1.5], voiceMode: "two", narrator: "male", inSeries: false,
  });
  const act: ToolActions = {
    seek: (i) => { reader.index = i; actions.push(`seek:${i}`); }, pause: () => { reader.pause(); actions.push("pause"); },
    setVoiceMode: (m) => actions.push(`mode:${m}`), setNarrator: (g) => actions.push(`narrator:${g}`), setSpeed: (s) => actions.push(`speed:${s}`),
    openRecap: (s) => actions.push(`recap:${s}`), showCast: () => actions.push("cast"),
  };
  const tools = (name: string, args: Record<string, unknown>): ToolOutcome => runTool(name, args, ctx(), act);
  const spoken: string[] = [];
  const asked: string[] = [];
  const talk = new TalkController({
    session, reader, tools, onToolRun: (c) => chips.push(c),
    retrieve: async () => ({ instructions: "INSTR" }),
    ...(opts.gemini ? {
      answer: async (q: string, _p: number, _s: string, onSentence: (t: string) => void) => { asked.push(q); onSentence("An answer."); },
      speaker: { play: async (t: string) => { spoken.push(t); }, prefetch: () => {}, stop: () => {} },
    } : {}),
    voiceFor: () => "marcus",
    onState: (s) => states.push(s), onToast: (m) => toasts.push(m),
  });
  return { session, reader, talk, states, toasts, chips, actions, spoken, asked };
}
const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);

async function ask(h: ReturnType<typeof setup>, text: string) {
  await h.talk.pttDown();
  await tick(600);
  const up = h.talk.pttUp();
  await tick(0);
  h.session.emit({ type: "transcript", text });
  await tick(0);
  await up;
}
async function finishSpeaking(h: ReturnType<typeof setup>) {
  h.session.emit({ type: "audio-start" });
  await tick(0);
  h.session.emit({ type: "response-done", status: "completed" });
  h.session.emit({ type: "playback-drained" });
  await tick(0);
}

describe("TalkController tools (Realtime)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("call -> reader action -> function_call_output -> response.create, chip shown, reading goes on at the new place", async () => {
    const h = setup({ playing: true });
    await ask(h, "Jump to chapter two");
    expect(h.talk.state).toBe("answering");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] });
    expect(h.actions).toEqual(["seek:8"]);
    expect(h.session.calls.at(-1)).toBe("return-tools");
    expect(h.session.returned[0][0]).toMatchObject({ callId: "c1" });
    expect(h.session.returned[0][0].output).toMatch(/Moved to Two/);
    expect(h.chips).toEqual(["Jumped to Two"]);
    expect(h.talk.state).toBe("answering");
    await finishSpeaking(h); // the spoken confirmation
    await tick(1500); // auto-resume
    expect(h.reader.log.at(-1)).toBe("resumeAt:8"); // not back to where the question was asked (10)
    expect(h.talk.state).toBe("reading");
  });

  it("spoiler guard: a chapter past the reader is refused, the reader is not moved, and the model is told", async () => {
    const h = setup({ playing: true });
    await ask(h, "Read chapter three");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "read_chapter", args: { chapter: "3" } }] });
    expect(h.actions).toEqual([]);
    expect(h.chips).toEqual([]);
    expect(h.session.returned[0][0].output).toMatch(/spoilers are off/);
    await finishSpeaking(h);
    await tick(1500);
    expect(h.reader.log.at(-1)).toBe("resumeAt:10"); // back where it was
  });

  it("with spoilers on the same jump is allowed", async () => {
    const h = setup({ playing: true, spoilers: true });
    await ask(h, "Read chapter three");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "read_chapter", args: { chapter: "3" } }] });
    expect(h.actions).toEqual(["seek:16"]);
  });

  it("read_chapter from Talk mode (reader paused) starts reading there once the character has spoken, auto-resume or not", async () => {
    const h = setup({ playing: false });
    h.talk.setAutoResume(false);
    await ask(h, "Read chapter two");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "read_chapter", args: { chapter: "two" } }] });
    await finishSpeaking(h);
    await tick(1500);
    expect(h.reader.log.at(-1)).toBe("resumeAt:8");
    expect(h.talk.state).toBe("reading");
  });

  it("pause_reading while reading: the story does not resume after the answer", async () => {
    const h = setup({ playing: true });
    await ask(h, "Pause the reading");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "pause_reading", args: {} }] });
    expect(h.chips).toEqual(["Reading paused"]);
    await finishSpeaking(h);
    await tick(1500);
    expect(h.reader.log.filter((l) => l.startsWith("resumeAt"))).toEqual([]);
    expect(h.talk.state).toBe("talk-idle");
  });

  it("a tool call after the turn was abandoned is ignored", async () => {
    const h = setup({ playing: true });
    await ask(h, "x");
    await h.talk.stop();
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "set_speed", args: { speed: "faster" } }] });
    expect(h.actions).toEqual([]);
    expect(h.session.returned).toEqual([]);
  });
});

describe("TalkController tools: every voice path ends somewhere visible (#102)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("a tool call whose spoken continuation never comes times out with a toast instead of 'Thinking' forever", async () => {
    const h = setup({ playing: true });
    await ask(h, "Jump to chapter two");
    h.session.emit({ type: "audio-start" }); // the first response said a word, then stopped to act
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] });
    expect(h.session.calls.at(-1)).toBe("return-tools");
    await tick(14000);
    expect(h.talk.state).toBe("answering"); // still inside the window
    await tick(2000);
    expect(h.toasts.at(-1)).toMatch(/took too long/);
    expect(["reading", "talk-idle"]).toContain(h.talk.state);
    expect(h.toasts).toHaveLength(1); // one watchdog, one toast
  });

  it("a tool call answered by a server error ends the turn with the error shown", async () => {
    const h = setup({ playing: true });
    await ask(h, "Jump to chapter two");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] });
    h.session.emit({ type: "error", message: "function_call_output rejected" });
    await tick(0);
    expect(h.toasts).toEqual(["function_call_output rejected"]);
    expect(h.talk.state).toBe("reading");
    await tick(20000);
    expect(h.toasts).toHaveLength(1); // the watchdog does not add a second toast after the turn ended
  });

  it("a continuation that only speaks (no second tool) ends normally; a second tool call re-arms the watchdog once", async () => {
    const h = setup({ playing: true });
    await ask(h, "Jump to chapter two then read it");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "jump_to", args: { chapter: "2" } }] });
    await tick(10000);
    h.session.emit({ type: "tool-call", calls: [{ callId: "c2", name: "bookmark", args: {} }] });
    await tick(10000); // 20 s since the first call, 10 s since the second: still waiting
    expect(h.talk.state).toBe("answering");
    expect(h.toasts).toEqual([]);
    await finishSpeaking(h);
    await tick(1500);
    expect(h.talk.state).toBe("reading");
  });
});

describe("TalkController tools (Gemini path: spoken commands as the transcriber writes them)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("punctuation, 'please' and a polite prefix from the transcript still run the tool (#102)", async () => {
    for (const said of ["Read chapter two.", "Read chapter 2, please!", "Could you read chapter two?", "Okay, jump to chapter two."]) {
      const h = setup({ gemini: true, playing: false });
      await ask(h, said);
      await tick(0);
      expect(h.actions, said).toEqual(["seek:8"]);
      expect(h.spoken, said).toEqual([]);
    }
  });
});

describe("TalkController tools (Gemini path: intent parse, no model call)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("a plain command runs the tool and ends the turn without asking Gemini", async () => {
    const h = setup({ gemini: true, playing: false });
    await ask(h, "Read chapter two");
    expect(h.actions).toEqual(["seek:8"]);
    expect(h.chips).toEqual(["Jumped to Two"]);
    expect(h.spoken).toEqual([]);
    expect(h.reader.log.at(-1)).toBe("resumeAt:8");
    expect(h.talk.state).toBe("reading");
  });
  it("a refused command (spoilers) toasts and stays idle; a real question still goes to Gemini", async () => {
    const h = setup({ gemini: true, playing: false });
    await ask(h, "Jump to chapter three");
    expect(h.actions).toEqual([]);
    expect(h.toasts[0]).toMatch(/spoilers are off/);
    expect(h.talk.state).toBe("talk-idle");
    await ask(h, "What happens in chapter three?");
    await tick(0);
    expect(h.spoken).toEqual(["An answer."]);
  });
});

describe("TalkController tools (Gemini path: a tool that needs an answer)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("'explain that' pauses reading and asks Gemini the tool's question instead of ending silently", async () => {
    const h = setup({ gemini: true, playing: true });
    await ask(h, "Explain that.");
    await tick(0);
    expect(h.chips).toEqual(["Explaining this paragraph"]);
    expect(h.asked).toHaveLength(1);
    expect(h.asked[0]).toMatch(/2-4 sentences/);
    expect(h.spoken).toEqual(["An answer."]);
    await tick(5000);
    expect(h.reader.log).not.toContain("play");
    expect(h.reader.log.filter((l) => l.startsWith("resumeAt"))).toEqual([]);
  });
});

describe("TalkController quiz mode", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const responds = (h: ReturnType<typeof setup>) => h.session.calls.filter((c) => c.startsWith("respond:")).map((c) => c.slice(8));

  async function startQuiz(h: ReturnType<typeof setup>) {
    await ask(h, "Quiz me");
    h.session.emit({ type: "tool-call", calls: [{ callId: "q1", name: "quiz_me", args: {} }] });
    await finishSpeaking(h); // the model asks question 1
  }

  it("the next three turns carry the quiz rules, text and progress; the turn after the end does not", async () => {
    const h = setup({ playing: false });
    await startQuiz(h);
    expect(h.chips).toEqual(["Quiz: Two"]);
    for (const n of [1, 2, 3]) { await ask(h, `answer ${n}`); await finishSpeaking(h); }
    const r = responds(h);
    expect(r).toHaveLength(4);
    expect(r[0]).toBe("INSTR"); // the turn that asked for the quiz
    expect(r[1]).toMatch(/^INSTR/); // normal grounding is kept
    expect(r[1]).toMatch(/QUIZ.*question 1 of 3/s);
    expect(r[1]).toContain("p4"); // the section text
    expect(r[1]).toMatch(/ask question 2/);
    expect(r[2]).toMatch(/question 2 of 3/);
    expect(r[3]).toMatch(/question 3 of 3/);
    expect(r[3]).toMatch(/final score/);
    await ask(h, "What happens next?");
    expect(responds(h).at(-1)).toBe("INSTR");
    expect(h.reader.log.filter((l) => l === "play" || l.startsWith("resumeAt"))).toEqual([]); // reading stayed paused
  });

  it("a navigation command, 'keep reading' or stop ends the quiz", async () => {
    for (const said of ["Jump to chapter two", "keep reading", "stop the quiz", "cancel"]) {
      const h = setup({ playing: false });
      await startQuiz(h);
      await ask(h, said);
      expect(responds(h).at(-1), said).toBe("INSTR");
      await finishSpeaking(h);
      await ask(h, "answer");
      expect(responds(h).at(-1), said).toBe("INSTR");
    }
  });

  it("another tool call or a speaker switch ends it", async () => {
    const h = setup({ playing: false });
    await startQuiz(h);
    await ask(h, "answer one");
    h.session.emit({ type: "tool-call", calls: [{ callId: "c2", name: "bookmark", args: {} }] });
    await finishSpeaking(h);
    await ask(h, "answer two");
    expect(responds(h).at(-1)).toBe("INSTR");

    const g = setup({ playing: false });
    await startQuiz(g);
    await g.talk.setSpeaker("someone-else");
    await ask(g, "answer one");
    expect(responds(g).at(-1)).toBe("INSTR");
  });

  it("reading on (Play, or a mic tap that resumes the story) ends the quiz: the next question is a plain one", async () => {
    const h = setup({ playing: false });
    await startQuiz(h);
    h.reader.play(); // the reader pressed Play and reads on
    await ask(h, "Who is the knight?");
    expect(responds(h).at(-1)).toBe("INSTR");

    const g = setup({ playing: false });
    await startQuiz(g);
    await g.talk.pttDown();
    await tick(50);
    await g.talk.pttUp(); // a tap: the story plays again
    expect(g.reader.log.at(-1)).toBe("play");
    g.reader.pause(); // even if it is paused again before the next question
    await ask(g, "Who is the knight?");
    expect(responds(g).at(-1)).toBe("INSTR");
  });

  it("endQuiz (the chat is cleared) ends it", async () => {
    const h = setup({ playing: false });
    await startQuiz(h);
    h.talk.endQuiz();
    await ask(h, "answer one");
    expect(responds(h).at(-1)).toBe("INSTR");
  });

  it("only completed answers count: an answer cut off by a barge-in is asked again as the same question", async () => {
    const h = setup({ playing: false });
    await startQuiz(h);
    await ask(h, "answer one");
    h.session.emit({ type: "audio-start" });
    await tick(0);
    await ask(h, "answer one, again"); // barge-in before the grading finished
    expect(responds(h).at(-1)).toMatch(/question 1 of 3/);
    await finishSpeaking(h);
    await ask(h, "answer two");
    expect(responds(h).at(-1)).toMatch(/question 2 of 3/);
    await finishSpeaking(h);
    await ask(h, "answer three");
    expect(responds(h).at(-1)).toMatch(/question 3 of 3/);
    await finishSpeaking(h);
    await ask(h, "What happens next?");
    expect(responds(h).at(-1)).toBe("INSTR");
  });

  it("Gemini path: the answers go to Gemini wrapped with the quiz progress, then plain again", async () => {
    const h = setup({ gemini: true, playing: false });
    await ask(h, "quiz me");
    await tick(0);
    expect(h.asked[0]).toMatch(/Quiz me on "Two"/);
    for (const n of [1, 2, 3]) { await ask(h, `answer ${n}`); await tick(0); }
    expect(h.asked[1]).toMatch(/question 1 of 3/);
    expect(h.asked[1]).toContain("answer 1");
    expect(h.asked[3]).toMatch(/final score/);
    expect(h.asked.every((q) => q.length <= 500)).toBe(true);
    await ask(h, "What happens next?");
    await tick(0);
    expect(h.asked[4]).toBe("What happens next?");
  });
});
