// @vitest-environment jsdom
// Follow-up questions keep the conversation (Bug "is the context missing my previous questions?"): a spoken question
// asked from READ mode, and its Realtime answer, land in the chat log and are given back to the character, both by the
// open session and by a fresh one (seeded history), and to the Gemini answer path.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetChatMemory } from "../src/talk/chatLog";

const players = vi.hoisted(() => [] as { onSpeaking: (s: boolean) => void }[]);
vi.mock("../src/audio/playback", () => ({
  RingPlayer: class {
    onSpeaking: (s: boolean) => void;
    constructor(cb: (s: boolean) => void) { this.onSpeaking = cb; players.push(this); }
    async resume() {} enqueueBase64() {} async flush() { return 0; } reset() {} async close() {}
  },
}));
vi.mock("../src/audio/capture", () => ({ MicCapture: class { async start() {} async stop() {} } }));
vi.mock("../src/talk/sentencePlayer", () => ({
  createSentenceSpeaker: () => ({ prefetch() {}, play: () => Promise.resolve(), stop() {}, pause() {}, resume() { return true; } }),
}));
vi.mock("../src/api", async (orig) => ({
  ...(await orig<typeof import("../src/api")>()),
  fetchRealtimeKey: vi.fn(async () => "bai-eph"),
  fetchRetrieve: vi.fn(async () => ({ instructions: "Answer from the passages." })),
  speak: vi.fn(async () => ({ audio_url: "/media/tts/x.wav", cached: true })),
  streamAnswer: vi.fn(async (_b: string, _q: string, _p: number, _s: string, onSentence: (t: string) => void) => { onSentence("A rabbit."); }),
}));

import { useTalk, type TalkUi } from "../src/talk/useTalk";
import TalkbackView from "../src/reader/TalkbackView";
import { HISTORY_TURNS, type ChatMessage } from "../src/talk/chatLog";
import type { Book } from "../src/api";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  constructor() { FakeSocket.all.push(this); queueMicrotask(() => { this.readyState = 1; this.onopen?.(); }); }
  send(d: string) { this.sent.push(JSON.parse(d)); }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
}

const flush = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0)); };
let now = 1_000_000;
let root: Root; let host: HTMLElement; let latest: TalkUi;
let harness: (p: { speakerId: string }) => null;
const switchTo = (speakerId: string) => act(async () => { root.render(createElement(harness, { speakerId })); await flush(); });
const seededOf = (ws: FakeSocket) => ws.sent.filter((e) => e.type === "conversation.item.create").map((e) => [e.item.role, e.item.content[0].text]);
const reader = { isPlaying: vi.fn(() => true), currentIndex: () => 3, currentPara: () => 3, pause: vi.fn(), play: vi.fn(), resumeAt: vi.fn() };

beforeEach(() => {
  resetChatMemory(); sessionStorage.clear(); FakeSocket.all = []; players.length = 0;
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.spyOn(Date, "now").mockImplementation(() => now);
  host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function mount(backend: "realtime" | "gemini", speakerId = "alice") {
  function Harness({ speakerId }: { speakerId: string }) {
    latest = useTalk({ bookId: "alice", enabled: true, reader, speakerId, voiceFor: () => "marcus", autoResume: true,
      health: { ok: true, boson_key_set: true, access_required: false, answer_backend: backend === "gemini" ? "gemini" : "realtime" } as never,
      talkback: false }); // READ mode
    return null;
  }
  harness = Harness;
  return act(async () => { root.render(createElement(Harness, { speakerId })); });
}

/** One spoken push-to-talk question from read mode, answered by Realtime (transcript, spoken reply, playback drained). */
async function speakRealtime(question: string, answer: string) {
  await act(async () => { latest.pttDown(); await flush(); });
  now += 1000; // held long enough to be a question, not a tap
  await act(async () => { latest.pttUp(); await flush(); });
  const ws = FakeSocket.all[FakeSocket.all.length - 1];
  await act(async () => { ws.recv({ type: "conversation.item.input_audio_transcription.completed", transcript: question }); await flush(); });
  await act(async () => {
    ws.recv({ type: "response.created" });
    ws.recv({ type: "response.output_audio.delta", delta: "AAAA", item_id: "it1" });
    ws.recv({ type: "response.output_audio_transcript.delta", delta: answer });
    ws.recv({ type: "response.output_audio_transcript.done", transcript: answer });
    ws.recv({ type: "response.done", response: { status: "completed" } });
    players[players.length - 1].onSpeaking(false); // the reply finished playing
    await flush();
  });
}

describe("read-mode mic questions keep the conversation", () => {
  it("Realtime: the spoken Q and the answer transcript are logged, one session holds both turns, and a new session is seeded with them", async () => {
    await mount("realtime");
    await speakRealtime("Who is the White Rabbit?", "A rabbit in a waistcoat.");
    expect(latest.messages.map((m) => [m.role, m.text, m.via, !!m.pending])).toEqual([
      ["user", "Who is the White Rabbit?", "voice", false],
      ["character", "A rabbit in a waistcoat.", "voice", false],
    ]);
    await speakRealtime("Why is he late?", "He lost track of time.");
    expect(FakeSocket.all).toHaveLength(1); // the follow-up went to the same session, which holds the first turn
    expect(latest.messages.map((m) => m.text)).toEqual(["Who is the White Rabbit?", "A rabbit in a waistcoat.", "Why is he late?", "He lost track of time."]);

    // the session times out between questions: the next one opens a fresh session seeded with the chat so far
    act(() => FakeSocket.all[0].close(1000, "idle"));
    await act(async () => { latest.pttDown(); await flush(); });
    const seeded = FakeSocket.all[1].sent.filter((e) => e.type === "conversation.item.create").map((e) => [e.item.role, e.item.content[0].text]);
    expect(seeded).toEqual([
      ["user", "Who is the White Rabbit?"], ["assistant", "A rabbit in a waistcoat."],
      ["user", "Why is he late?"], ["assistant", "He lost track of time."],
    ]);
  });

  it("Gemini: a spoken follow-up from read mode sends the earlier spoken Q/A as history", async () => {
    const { streamAnswer } = await import("../src/api");
    const sa = streamAnswer as ReturnType<typeof vi.fn>;
    sa.mockClear();
    await mount("gemini");
    const ask = async (q: string) => {
      await act(async () => { latest.pttDown(); await flush(); });
      now += 1000;
      await act(async () => { latest.pttUp(); await flush(); });
      const ws = FakeSocket.all[FakeSocket.all.length - 1];
      await act(async () => { ws.recv({ type: "conversation.item.input_audio_transcription.completed", transcript: q }); await flush(); });
    };
    await ask("Who is the White Rabbit?");
    await ask("Why is he late?");
    expect(sa).toHaveBeenCalledTimes(2);
    expect(sa.mock.calls[1][7]).toEqual([{ role: "user", text: "Who is the White Rabbit?" }, { role: "character", text: "A rabbit." }]);
  });

  it("Realtime: each character has their own memory; switching speaker starts that character's session with their own turns", async () => {
    await mount("realtime", "alice");
    await speakRealtime("Who is the White Rabbit?", "A rabbit in a waistcoat.");
    await switchTo("hatter");
    expect(FakeSocket.all[0].readyState).toBe(3); // Alice's session is not handed to the Hatter
    await speakRealtime("Why is a raven like a writing desk?", "I haven't the slightest idea.");
    expect(FakeSocket.all).toHaveLength(2);
    expect(seededOf(FakeSocket.all[1])).toEqual([]); // the Hatter never heard Alice's chat
    await switchTo("alice");
    await act(async () => { latest.pttDown(); await flush(); });
    expect(seededOf(FakeSocket.all[2])).toEqual([["user", "Who is the White Rabbit?"], ["assistant", "A rabbit in a waistcoat."]]);
  });

  it("Realtime: a speaker switch during an answer waits for the answer to end before dropping the session", async () => {
    await mount("realtime", "alice");
    await act(async () => { latest.pttDown(); await flush(); });
    now += 1000;
    await act(async () => { latest.pttUp(); await flush(); });
    const ws = FakeSocket.all[0];
    await act(async () => { ws.recv({ type: "conversation.item.input_audio_transcription.completed", transcript: "Hi?" }); await flush(); });
    await act(async () => { ws.recv({ type: "response.created" }); ws.recv({ type: "response.output_audio.delta", delta: "AAAA", item_id: "i" }); await flush(); });
    expect(latest.state).toBe("answering");
    await switchTo("hatter");
    expect(ws.readyState).toBe(1); // the answer plays on
    await act(async () => { ws.recv({ type: "response.done", response: { status: "completed" } }); players[0].onSpeaking(false); await flush(); });
    expect(ws.readyState).toBe(3);
  });
});

describe("the remembered count in Talkback", () => {
  it("counts only the lines that are given back to the character (the last HISTORY_TURNS of this speaker)", () => {
    const messages: ChatMessage[] = [];
    for (let i = 0; i < 5; i++) messages.push(
      { id: 4 * i + 1, role: "user", speakerId: "alice", text: `Q${i}` }, { id: 4 * i + 2, role: "character", speakerId: "alice", text: `A${i}` },
      { id: 4 * i + 3, role: "user", speakerId: "hatter", text: `H${i}` }, { id: 4 * i + 4, role: "character", speakerId: "hatter", text: `R${i}` });
    const book = { id: "b", kind: "novel", narrator: null, sections: [], paragraphs: [], characters: [
      { id: "alice", name: "Alice", first_para: 0, voice: "marcus", voice_tags: "", persona: "", portrait: null }] } as unknown as Book;
    const talk = { state: "talk-idle", speaking: false, caption: "", toast: null, level: 0, latency: null, question: "", language: null, logs: [], messages,
      pttDown() {}, pttUp() {}, setSpeaker() {}, dismissToast() {}, ask() {}, stop() {}, clearMessages() {} } as unknown as TalkUi;
    act(() => root.render(createElement(TalkbackView, {
      book, para: 0, speakerId: "alice", onSpeaker: () => {}, pinned: null, talk, faces: [], faceFor: () => undefined,
      autoVoiceFor: () => "marcus", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
      onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
    })));
    expect(host.querySelector(".remembers")?.textContent).toBe(`Alice remembers this chat (${HISTORY_TURNS} lines)`);
  });
});
