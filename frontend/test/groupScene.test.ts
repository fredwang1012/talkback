// @vitest-environment jsdom
/** #79 group scene: one question answered in turn by up to three characters (round logic, chat, history, picker). */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { MAX_MESSAGES, resetChatMemory, type ChatMessage } from "../src/talk/chatLog";
import {
  MAX_GROUP, TalkController, type ReaderControl, type SessionEvent, type TalkSession,
} from "../src/talk/TalkController";
import { addRoundAnswer, historyFor, roundLines, MAX_HISTORY_LINES, MAX_NAME_CHARS } from "../src/talk/groupRound";
import { groupChoices, togglePick, validPicks } from "../src/reader/groupScene";

// ---- the round in TalkController (fakes only) --------------------------------------------------------------

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  textMode = false;
  hold: string | null = null; // the call `open:<voice>` or `voice:<voice>` waits for release()
  private held: (() => void)[] = [];
  release() { this.held.splice(0).forEach((r) => r()); }
  private wait(call: string) { return call === this.hold ? new Promise<void>((r) => this.held.push(r)) : Promise.resolve(); }
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); await this.wait(`open:${voice}`); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); await this.wait(`voice:${voice}`); }
  returnTools(results: { callId: string; output: string }[]) { this.calls.push(...results.map((r) => `tools:${r.output}`)); }
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

const VOICES: Record<string, string> = { alice: "belinda", hatter: "marcus", cat: "oliver", queen: "zara", twin: "belinda" };
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
    const ran: string[] = [];
    const talk = new TalkController({
      session, reader: reader(),
      retrieve: async (_q, _p, speakerId) => { retrieved.push(speakerId); return { instructions: `PERSONA:${speakerId}` }; },
      voiceFor: (id) => VOICES[id] ?? "narr",
      onRoundTurn: (id) => turns.push(id),
      tools: (name) => { ran.push(name); return { ok: true, output: "Turned the page.", chip: null }; },
    });
    return { session, talk, retrieved, turns, ran };
  }
  const responds = (h: ReturnType<typeof realtime>) => h.session.calls.filter((c) => c.startsWith("respond:"));
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
    const order = h.session.calls.filter((c) => c.startsWith("open:") || c.startsWith("voice:") || c.startsWith("respond:"));
    expect(order).toEqual([
      "open:belinda", "respond:PERSONA:alice", "open:marcus", "respond:PERSONA:hatter", "open:oliver", "respond:PERSONA:cat", "voice:belinda",
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

  it("an answer with no audio advances the round once (its response-done and playback-drained both end it)", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter", "cat"]);
    void h.talk.ask("Tea?");
    await tick();
    h.session.emit({ type: "response-done", status: "completed" }); // TalkClient: no audio, so playback-drained at once
    h.session.emit({ type: "playback-drained" });
    await tick();
    expect(h.turns).toEqual(["hatter"]);
    expect(h.retrieved).toEqual(["alice", "hatter"]);
    expect(responds(h)).toEqual(["respond:PERSONA:alice", "respond:PERSONA:hatter"]);
    await done(h);
    await done(h);
    expect(h.retrieved).toEqual(["alice", "hatter", "cat"]);
    expect(h.talk.state).toBe("talk-idle");
  });

  it("a spoken command runs once: it ends the round, and a later character never runs a tool again", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter", "cat"]);
    void h.talk.ask("Skip ahead a page");
    await tick();
    h.session.emit({ type: "tool-call", calls: [{ callId: "c1", name: "turn_page", args: {} }] });
    await done(h);
    await tick(20000);
    expect(h.ran).toEqual(["turn_page"]);
    expect(h.retrieved).toEqual(["alice"]);
    expect(h.turns).toEqual([]);

    const g = realtime(); // a question, but the second character's model calls a tool anyway: it is not run
    await g.talk.setSpeaker("alice");
    g.talk.setGroup(["hatter"]);
    void g.talk.ask("What is tea?");
    await tick();
    await done(g);
    expect(g.turns).toEqual(["hatter"]);
    g.session.emit({ type: "tool-call", calls: [{ callId: "c2", name: "turn_page", args: {} }] });
    expect(g.ran).toEqual([]);
    expect(g.session.calls.filter((c) => c.startsWith("tools:"))).toEqual([expect.stringMatching(/^tools:Not run/)]);
  });

  it("the next question waits for the voice of the one talked to to be put back before it is sent", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter"]);
    void h.talk.ask("Tea?");
    await tick();
    await done(h);
    h.session.hold = "voice:belinda"; // the restore is slow (refused: a reconnect)
    await done(h);
    expect(h.session.calls.at(-1)).toBe("voice:belinda");
    void h.talk.ask("And now?");
    await tick();
    expect(h.retrieved).toEqual(["alice", "hatter", "alice"]);
    expect(responds(h)).toEqual(["respond:PERSONA:alice", "respond:PERSONA:hatter"]); // not on the socket being replaced
    h.session.release();
    await tick();
    expect(responds(h)).toEqual(["respond:PERSONA:alice", "respond:PERSONA:hatter", "respond:PERSONA:alice"]);
  });

  it("switches the voice only when it changes, alongside the retrieval, and never in text mode", async () => {
    const h = realtime();
    await h.talk.setSpeaker("alice");
    h.talk.setGroup(["hatter", "twin"]);
    h.session.calls.length = 0;
    h.session.hold = "open:marcus";
    void h.talk.ask("Tea?");
    await tick();
    await done(h);
    expect(h.retrieved).toEqual(["alice", "hatter"]); // fetched while the voice switches
    expect(responds(h)).toEqual(["respond:PERSONA:alice"]);
    h.session.release();
    await tick();
    expect(responds(h)).toEqual(["respond:PERSONA:alice", "respond:PERSONA:hatter"]);
    await done(h);
    await done(h);
    // the twin has the voice of the one talked to: switched back for them, so nothing to restore after the round
    expect(h.session.calls.filter((c) => !c.startsWith("respond:"))).toEqual(["open:belinda", "open:marcus", "open:belinda"]);

    const t = realtime();
    t.session.textMode = true; // Higgs TTS voices each answer: the session voice is never heard
    await t.talk.setSpeaker("alice");
    t.talk.setGroup(["hatter"]);
    t.session.calls.length = 0;
    void t.talk.ask("Tea?");
    await tick();
    await done(t);
    await done(t);
    expect(t.retrieved).toEqual(["alice", "hatter"]);
    expect(t.session.calls.filter((c) => !c.startsWith("respond:"))).toEqual(["open:belinda"]); // only ask's own ensureOpen
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
    expect(skipped.at(-1)!.id).toBe(6); // never the dropped answer's id (5 is already in the round's list)
  });

  it("a new answer's id is above every id (a tool chip sits before the pending answer) and the chat stays capped", () => {
    const withChip: ChatMessage[] = [...base.slice(0, 3), { id: 5, role: "tool", speakerId: "tool", text: "Looked it up on the web" }, base[3]];
    expect(addRoundAnswer(withChip, "cat").at(-1)!.id).toBe(6);
    const full: ChatMessage[] = Array.from({ length: MAX_MESSAGES }, (_, i) => ({ id: i + 1, role: "character", speakerId: "alice", text: `t${i}` }));
    expect(addRoundAnswer(full, "cat")).toHaveLength(MAX_MESSAGES);
  });

  it("a character's replies in earlier rounds keep the question they answered", () => {
    const m: ChatMessage[] = [
      { id: 1, role: "user", speakerId: "alice", text: "Who is the Queen?", via: "typed" },
      { id: 2, role: "character", speakerId: "alice", text: "A fierce ruler." },
      { id: 3, role: "character", speakerId: "cat", text: "Mad, like all of us." },
      { id: 4, role: "user", speakerId: "alice", text: "And the King?", via: "typed" },
      { id: 5, role: "character", speakerId: "alice", text: "Quiet.", pending: true },
    ];
    expect(historyFor(m, "cat")).toEqual([{ role: "user", text: "Who is the Queen?" }, { role: "character", text: "Mad, like all of us." }]);
    expect(historyFor(m, "alice")).toEqual([{ role: "user", text: "Who is the Queen?" }, { role: "character", text: "A fierce ruler." }]);
  });

  it("a long character name is cut to what the server takes", () => {
    const m = addRoundAnswer(base, "cat");
    expect(roundLines(m, [4], "cat", () => "N".repeat(120))[0].name).toHaveLength(MAX_NAME_CHARS);
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
const holdAnswer = vi.hoisted(() => ({ speaker: null as string | null })); // this speaker's answer never comes (until aborted)
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
  streamAnswer: vi.fn(async (_b: string, _q: string, _p: number, speaker: string, onSentence: (t: string) => void, sig: AbortSignal | undefined, _sp: unknown, history: unknown[]) => {
    answered.push({ speaker, history });
    if (speaker === holdAnswer.speaker) {
      await new Promise((_r, reject) => sig?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    }
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

  it("a question typed over a round is answered under the face and name of the one talked to", async () => {
    answered.length = 0;
    let latest: TalkUi | null = null;
    function App() {
      const talk = useTalk({
        bookId: "alice-group-2", enabled: true, reader: reader(), speakerId: "alice", voiceFor: (id) => VOICES[id] ?? "narr", autoResume: false,
        health: { ok: true, boson_key_set: true, access_required: false, answer_backend: "gemini" }, talkback: true,
      });
      latest = talk;
      return createElement(TalkbackView, {
        book: novel, para: 10, speakerId: "alice", onSpeaker: () => {}, pinned: null, talk, faces: [], faceFor: () => undefined,
        autoVoiceFor: () => "belinda", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
        onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
      });
    }
    const flush = async () => { for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
    await act(async () => { root.render(createElement(App)); });
    await act(async () => { host.querySelectorAll<HTMLButtonElement>(".group-pick button")[1].click(); }); // + Cheshire Cat
    try {
      holdAnswer.speaker = "cat";
      await act(async () => { latest!.ask("First?"); });
      await flush();
      expect(answered.map((a) => a.speaker)).toEqual(["alice", "cat"]);
      expect(latest!.roundSpeaker).toBe("cat");
      holdAnswer.speaker = "alice";
      await act(async () => { latest!.ask("Second?"); });
      await flush();
      expect(answered.map((a) => a.speaker)).toEqual(["alice", "cat", "alice"]);
      expect(latest!.roundSpeaker).toBeNull();
      expect(host.querySelector(".tb-name")!.textContent).toBe("Alice");
    } finally {
      holdAnswer.speaker = null;
      await act(async () => { await latest!.stop(); });
    }
  });
});
