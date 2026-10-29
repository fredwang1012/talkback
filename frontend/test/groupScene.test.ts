// @vitest-environment jsdom
/** #79 group scene: one question answered in turn by up to three characters (round logic, chat, history, picker). */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { resetChatMemory, type ChatMessage } from "../src/talk/chatLog";
import {
  MAX_GROUP, TalkController, type ReaderControl, type SessionEvent, type TalkSession,
} from "../src/talk/TalkController";
import { addRoundAnswer, historyFor, roundLines, MAX_HISTORY_LINES } from "../src/talk/groupRound";
import { groupChoices, togglePick, validPicks } from "../src/reader/groupScene";

// ---- the round in TalkController (fakes only) --------------------------------------------------------------

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); }
  async startMic() { this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(instructions: string) { this.calls.push(`respond:${instructions}`); }
  async interrupt() { this.calls.push("interrupt"); }
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}

const reader = (): ReaderControl => ({
  isPlaying: () => false, currentIndex: () => 4, currentPara: () => 2, pause() {}, play() {}, resumeAt() {},
});

class FakeSpeaker {
  log: string[] = [];
  gates: Record<string, () => void> = {};
  play = (text: string) => new Promise<void>((resolve) => { this.log.push(`play:${text}`); this.gates[text] = resolve; });
  prefetch = () => {};
  stop = () => { this.log.push("stop"); Object.values(this.gates).forEach((g) => g()); };
}

const VOICES: Record<string, string> = { alice: "belinda", hatter: "marcus", cat: "oliver", queen: "zara" };
const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);

function gemini() {
  const session = new FakeSession();
  const speaker = new FakeSpeaker();
  const asked: { speaker: string; finish: () => void; emit: (t: string) => void }[] = [];
  const turns: [string, number][] = [];
  const talk = new TalkController({
    session, reader: reader(), speaker,
    retrieve: async () => ({ instructions: "unused" }),
    voiceFor: (id) => VOICES[id] ?? "narr",
    onRoundTurn: (id, i) => turns.push([id, i]),
    answer: (_q, _p, speakerId, onSentence, signal) => new Promise<void>((resolve, reject) => {
      asked.push({ speaker: speakerId, finish: resolve, emit: onSentence });
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }),
  });
  return { session, speaker, talk, asked, turns };
}

/** The current answer says one sentence, finishes, and the sentence finishes playing. */
async function answerWith(h: ReturnType<typeof gemini>, text: string) {
  const a = h.asked[h.asked.length - 1];
  a.emit(text);
  await tick();
  a.finish();
  await tick();
  h.speaker.gates[text]?.();
  await tick();
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("group round (Gemini answer path)", () => {
  it("answers in the order picked, each as its own speaker, after the one talked to", async () => {
    const h = gemini();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["cat", "hatter"]);
    void h.talk.ask("Who is the Queen?");
    await tick();
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice"]);
    expect(h.talk.answerSpeaker).toBe("alice");
    await answerWith(h, "Alice speaks.");
    expect(h.turns).toEqual([["cat", 1]]);
    expect(h.talk.answerSpeaker).toBe("cat");
    await answerWith(h, "Cat speaks.");
    expect(h.turns).toEqual([["cat", 1], ["hatter", 2]]);
    await answerWith(h, "Hatter speaks.");
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice", "cat", "hatter"]);
    expect(h.speaker.log.filter((l) => l.startsWith("play:"))).toEqual(["play:Alice speaks.", "play:Cat speaks.", "play:Hatter speaks."]);
    expect(h.talk.state).toBe("talk-idle"); // the round is over: one end, after the last answer
    expect(h.talk.answerSpeaker).toBe("alice");
    expect(h.session.calls.filter((c) => c.startsWith("voice:"))).toEqual(["voice:belinda"]); // Gemini path: no session voice switches
  });

  it("caps a round at MAX_GROUP and drops duplicates and the one talked to", async () => {
    const h = gemini();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["alice", "cat", "cat", "hatter", "queen"]);
    void h.talk.ask("Hello all");
    await tick();
    for (const t of ["A.", "B.", "C."]) await answerWith(h, t);
    expect(MAX_GROUP).toBe(3);
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice", "cat", "hatter"]);
  });

  it("Stop ends the whole round: the rest never answer", async () => {
    const h = gemini();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["cat", "hatter"]);
    void h.talk.ask("Why?");
    await tick();
    await answerWith(h, "Because.");
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice", "cat"]);
    await h.talk.stop();
    await tick(20000);
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice", "cat"]);
    expect(h.turns).toEqual([["cat", 1]]);
    expect(h.talk.answerSpeaker).toBe("alice");
  });

  it("a barge-in (or a new question) ends the round; the new question starts its own", async () => {
    const h = gemini();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["cat"]);
    void h.talk.ask("First?");
    await tick();
    h.asked[0].emit("Alice talks.");
    await tick();
    void h.talk.pttDown(); // cut in while Alice speaks
    await tick();
    h.speaker.gates["Alice talks."]?.();
    await tick(20000);
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice"]);
    expect(h.turns).toEqual([]);
  });

  it("no group: a normal one-to-one answer", async () => {
    const h = gemini();
    await h.talk.setSpeaker("alice");
    void h.talk.ask("Hi");
    await tick();
    await answerWith(h, "Hello.");
    expect(h.asked.map((a) => a.speaker)).toEqual(["alice"]);
    expect(h.turns).toEqual([]);
    expect(h.talk.state).toBe("talk-idle");
  });
});

describe("group round (Realtime path)", () => {
  function realtime() {
    const session = new FakeSession();
    const retrieved: string[] = [];
    const turns: string[] = [];
    const talk = new TalkController({
      session, reader: reader(),
      retrieve: async (_q, _p, speakerId) => { retrieved.push(speakerId); return { instructions: `PERSONA:${speakerId}` }; },
      voiceFor: (id) => VOICES[id] ?? "narr",
      onRoundTurn: (id) => turns.push(id),
    });
    return { session, talk, retrieved, turns };
  }
  async function done(h: ReturnType<typeof realtime>) {
    h.session.emit({ type: "audio-start" });
    await tick();
    h.session.emit({ type: "response-done", status: "completed" });
    h.session.emit({ type: "playback-drained" });
    await tick();
  }

  it("switches the session voice and the persona instructions per character, then restores the voice", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter", "cat"]);
    h.session.calls.length = 0;
    void h.talk.ask("Tea?");
    await tick();
    await done(h);
    await done(h);
    await done(h);
    expect(h.retrieved).toEqual(["alice", "hatter", "cat"]);
    const order = h.session.calls.filter((c) => c.startsWith("voice:") || c.startsWith("respond:"));
    expect(order).toEqual([
      "respond:PERSONA:alice", "voice:marcus", "respond:PERSONA:hatter", "voice:oliver", "respond:PERSONA:cat", "voice:belinda",
    ]);
    expect(h.talk.state).toBe("talk-idle");
  });

  it("Stop mid-round restores the voice of the one talked to and asks no one else", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter", "cat"]);
    void h.talk.ask("Tea?");
    await tick();
    await done(h);
    expect(h.retrieved).toEqual(["alice", "hatter"]);
    h.session.calls.length = 0;
    await h.talk.stop();
    await tick(20000);
    expect(h.retrieved).toEqual(["alice", "hatter"]);
    expect(h.session.calls).toContain("voice:belinda");
  });
});

// ---- chat pieces ------------------------------------------------------------------------------------------

describe("group round chat and history", () => {
  const names = (id: string) => ({ alice: "Alice", cat: "Cheshire Cat", hatter: "Hatter" } as Record<string, string>)[id] ?? id;
  const base: ChatMessage[] = [
    { id: 1, role: "user", speakerId: "cat", text: "Earlier to the cat", via: "typed" },
    { id: 2, role: "character", speakerId: "cat", text: "Cat's earlier reply" },
    { id: 3, role: "user", speakerId: "alice", text: "Who is the Queen?", via: "typed" },
    { id: 4, role: "character", speakerId: "alice", text: "A fierce ruler.", pending: true },
  ];

  it("each answer gets its own attributed bubble; an empty one is dropped", () => {
    const m = addRoundAnswer(base, "cat");
    expect(m.slice(-2)).toEqual([
      { id: 4, role: "character", speakerId: "alice", text: "A fierce ruler.", pending: false },
      { id: 5, role: "character", speakerId: "cat", text: "", via: "typed", pending: true },
    ]);
    const skipped = addRoundAnswer(m, "hatter"); // the cat said nothing
    expect(skipped.slice(-2).map((x) => [x.speakerId, x.pending])).toEqual([["alice", false], ["hatter", true]]);
  });

  it("later answers see the earlier answers of the round, named, after their own chat", () => {
    const m = addRoundAnswer(base, "cat");
    expect(roundLines(m, [4, 5], "cat", names)).toEqual([{ role: "character", name: "Alice", text: "A fierce ruler." }]);
    expect(historyFor(m, "cat", [4, 5], names)).toEqual([
      { role: "user", text: "Earlier to the cat" },
      { role: "character", text: "Cat's earlier reply" },
      { role: "character", name: "Alice", text: "A fierce ruler." },
    ]);
    // the first speaker sees nothing of the round (it has not been answered yet), and no one sees their own line named
    expect(historyFor(base, "alice", [4], names)).toEqual([]);
  });

  it("round lines are short and the history stays within the server's 8 lines", () => {
    const long = "x".repeat(900);
    const m: ChatMessage[] = [];
    for (let i = 1; i <= 14; i += 2) {
      m.push({ id: i, role: "user", speakerId: "hatter", text: `q${i}` }, { id: i + 1, role: "character", speakerId: "hatter", text: `a${i}` });
    }
    m.push({ id: 20, role: "character", speakerId: "alice", text: long }, { id: 21, role: "character", speakerId: "cat", text: "Grin." },
      { id: 22, role: "character", speakerId: "hatter", text: "", pending: true });
    const h = historyFor(m, "hatter", [20, 21, 22], names);
    expect(h).toHaveLength(MAX_HISTORY_LINES);
    expect(h.slice(-2).map((t) => t.name)).toEqual(["Alice", "Cheshire Cat"]);
    expect(h[h.length - 2].text).toHaveLength(300);
  });
});

// ---- the picker: spoiler gating, cap, order ---------------------------------------------------------------

const para = (idx: number) => ({ idx, text: "x", section: 0, tts_chunks: [] }) as unknown as Book["paragraphs"][number];
const novel = {
  id: "alice", title: "Alice", author: "Carroll", kind: "novel", license: "", attribution: null,
  sections: [{ id: "s1", title: "Down the Rabbit-Hole", start_para: 0 }], paragraphs: [para(0)],
  characters: [
    { id: "alice", name: "Alice", first_para: 0, voice: "belinda", voice_tags: "", persona: "", portrait: null },
    { id: "cat", name: "Cheshire Cat", first_para: 5, voice: "oliver", voice_tags: "", persona: "", portrait: null },
    { id: "queen", name: "Queen of Hearts", first_para: 80, voice: "zara", voice_tags: "", persona: "", portrait: null },
  ],
  narrator: null,
} as unknown as Book;

describe("group picker logic", () => {
  it("offers only characters met so far (everyone with spoilers on), never the one talked to", () => {
    expect(groupChoices(novel, 10, false, "alice").map((c) => c.id)).toEqual(["narrator", "cat"]);
    expect(groupChoices(novel, 10, true, "alice").map((c) => c.id)).toEqual(["narrator", "cat", "queen"]);
    expect(groupChoices({ ...novel, kind: "textbook" } as Book, 10, true, "narrator")).toEqual([]);
  });

  it("keeps the order picked, caps at two others, and drops picks no longer allowed", () => {
    let p = togglePick([], "cat");
    p = togglePick(p, "narrator");
    expect(p).toEqual(["cat", "narrator"]);
    expect(togglePick(p, "queen")).toEqual(["cat", "narrator"]); // full
    expect(togglePick(p, "cat")).toEqual(["narrator"]);
    // spoilers turned off: the Queen (not met yet) drops out
    expect(validPicks(["queen", "cat"], groupChoices(novel, 10, false, "alice"))).toEqual(["cat"]);
    // the one picked is now the one talked to: it drops out of the others
    expect(validPicks(["cat"], groupChoices(novel, 10, false, "cat"))).toEqual([]);
  });
});

// ---- the Talkback view and useTalk, end to end (api and players faked) ------------------------------------

const spoken = vi.hoisted(() => [] as string[]);
const clips = vi.hoisted(() => [] as [string, string, string | null][]);
const answered = vi.hoisted(() => [] as { speaker: string; history: unknown[] }[]);
vi.mock("../src/audio/playback", () => ({ RingPlayer: class { async close() {} } }));
vi.mock("../src/audio/capture", () => ({ MicCapture: class { async stop() {} } }));
vi.mock("../src/talk/sentencePlayer", () => ({
  createSentenceSpeaker: (synth: (t: string) => Promise<unknown>) => ({
    prefetch() {}, play: async (t: string) => { spoken.push(t); await synth(t); }, stop() {}, pause() {}, resume() { return true; },
  }),
}));
vi.mock("../src/talk/clipVideo", async (orig) => ({
  ...(await orig<typeof import("../src/talk/clipVideo")>()),
  answerClip: vi.fn(async (_s: unknown, text: string, voice: string, o: { faceId: string | null }) => { clips.push([text, voice, o.faceId]); return { audio_url: "/media/tts/x.wav" }; }),
}));
vi.mock("../src/api", async (orig) => ({
  ...(await orig<typeof import("../src/api")>()),
  speak: vi.fn(async () => ({ audio_url: "/media/tts/x.wav", cached: true })),
  streamAnswer: vi.fn(async (_b: string, _q: string, _p: number, speaker: string, onSentence: (t: string) => void, _sig: unknown, _sp: unknown, history: unknown[]) => {
    answered.push({ speaker, history });
    onSentence(`I am ${speaker}.`);
  }),
}));

import TalkbackView from "../src/reader/TalkbackView";
import { useTalk, type TalkUi } from "../src/talk/useTalk";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("group scene in the Talkback view", () => {
  let root: Root;
  let host: HTMLElement;
  beforeEach(() => { vi.useRealTimers(); resetChatMemory(); sessionStorage.clear(); host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
  afterEach(() => { act(() => root.unmount()); host.remove(); });

  it("asks the picked characters in turn: own bubble, voice, face and history each", async () => {
    spoken.length = 0; clips.length = 0; answered.length = 0;
    let latest: TalkUi | null = null;
    const faces: Record<string, string | null> = { alice: "f-alice", cat: null, narrator: "f-narr" };
    function App() {
      const talk = useTalk({
        bookId: "alice-group", enabled: true, reader: reader(), speakerId: "alice", voiceFor: (id) => VOICES[id] ?? "narr", autoResume: false,
        faceIdFor: (id) => faces[id] ?? null, health: { ok: true, boson_key_set: true, access_required: false, answer_backend: "gemini" }, talkback: true,
      });
      latest = talk;
      return createElement(TalkbackView, {
        book: novel, para: 10, speakerId: "alice", onSpeaker: () => {}, pinned: null, talk, faces: [], faceFor: () => undefined,
        autoVoiceFor: () => "belinda", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
        onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
      });
    }
    await act(async () => { root.render(createElement(App)); });
    const toggles = () => [...host.querySelectorAll<HTMLButtonElement>(".group-pick button")];
    expect(toggles().map((b) => b.textContent)).toEqual(["Narrator", "Cheshire Cat"]); // the Queen is not met yet
    await act(async () => { toggles()[1].click(); });
    await act(async () => { toggles()[0].click(); });
    expect(toggles().map((b) => b.textContent)).toEqual(["3Narrator", "2Cheshire Cat"]);
    expect(host.querySelector(".group-note")!.textContent).toContain("3 answers per question");
    expect(host.querySelector<HTMLInputElement>("#tb-q")!.placeholder).toBe("Ask Alice, Cheshire Cat, Narrator…");
    expect(host.querySelector<HTMLButtonElement>(".tb-text button[type=submit]")!.textContent).toBe("Send to 3");

    await act(async () => { latest!.ask("Who is the Queen?"); await new Promise((r) => setTimeout(r, 0)); });
    for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(answered.map((a) => a.speaker)).toEqual(["alice", "cat", "narrator"]);
    expect(answered[0].history).toEqual([]);
    expect(answered[1].history).toEqual([{ role: "character", name: "Alice", text: "I am alice." }]);
    expect(answered[2].history).toEqual([
      { role: "character", name: "Alice", text: "I am alice." }, { role: "character", name: "Cheshire Cat", text: "I am cat." },
    ]);
    expect(clips).toEqual([["I am alice.", "belinda", "f-alice"], ["I am cat.", "oliver", null], ["I am narrator.", "narr", "f-narr"]]);
    expect(latest!.messages.map((m) => [m.role, m.speakerId, m.text])).toEqual([
      ["user", "alice", "Who is the Queen?"], ["character", "alice", "I am alice."], ["character", "cat", "I am cat."], ["character", "narrator", "I am narrator."],
    ]);
    const who = [...host.querySelectorAll(".bubble .who-line")].map((w) => w.textContent);
    expect(who).toEqual(["You typed", "Alice", "Cheshire Cat", "Narrator"]);
    for (const m of latest!.messages.slice(1)) expect(latest!.replayable.has(m.id)).toBe(true); // each answer replays on its own
  });
});
