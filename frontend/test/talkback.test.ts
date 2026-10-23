// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { resetChatMemory } from "../src/talk/chatLog";

vi.mock("../src/audio/playback", () => ({ RingPlayer: class { async close() {} } }));
vi.mock("../src/audio/capture", () => ({ MicCapture: class { async stop() {} } }));
const spoken = vi.hoisted(() => [] as string[]);
const ctl = vi.hoisted(() => ({ hold: false, release: [] as (() => void)[], log: [] as string[] }));
const made = vi.hoisted(() => [] as { synth: (t: string) => Promise<unknown>; hooks?: { sync?: unknown } }[]);
vi.mock("../src/talk/sentencePlayer", () => ({
  createSentenceSpeaker: (synth: (t: string) => Promise<unknown>, _c: unknown, hooks?: { sync?: unknown }) => (made.push({ synth, hooks }), {
    prefetch() {},
    play: (t: string) => { spoken.push(t); return ctl.hold ? new Promise<void>((r) => ctl.release.push(r)) : Promise.resolve(); },
    stop() { ctl.log.push("stop"); ctl.release.splice(0).forEach((r) => r()); },
    pause() { ctl.log.push("pause"); },
    resume() { ctl.log.push("resume"); return true; },
  }),
}));
vi.mock("../src/api", async (orig) => ({
  ...(await orig<typeof import("../src/api")>()),
  speak: vi.fn(async () => ({ audio_url: "/media/tts/x.wav", cached: true })),
  streamAnswer: vi.fn(async (_b: string, _q: string, _p: number, _s: string, onSentence: (t: string) => void) => {
    onSentence("Hagrid is the keeper of keys.");
    onSentence("He is kind.");
  }),
}));

import TalkbackView, { ModeToggle } from "../src/reader/TalkbackView";
import { useTalk, type TalkUi } from "../src/talk/useTalk";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const para = (idx: number) => ({ idx, text: "x", section: 0, tts_chunks: [] }) as unknown as Book["paragraphs"][number];
const novel = {
  id: "hp1", title: "T", author: "A", kind: "novel", license: "", attribution: null,
  sections: [{ id: "s1", title: "The Boy Who Lived", start_para: 0 }, { id: "s2", title: "The Vanishing Glass", start_para: 10 }],
  paragraphs: [para(0)],
  characters: [
    { id: "hagrid", name: "Hagrid", first_para: 5, voice: "marcus", voice_tags: "", persona: "", portrait: null },
    { id: "snape", name: "Snape", first_para: 50, voice: "marcus", voice_tags: "", persona: "", portrait: null },
  ],
  narrator: null,
} as unknown as Book;
const textbook = { ...novel, kind: "textbook", characters: [] } as Book;

let root: Root;
let host: HTMLElement;
beforeEach(() => { resetChatMemory(); sessionStorage.clear(); host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

const fakeTalk = (over: Partial<TalkUi> = {}): TalkUi => ({
  state: "talk-idle", caption: "", toast: null, level: 0, latency: null, speaking: false, question: "", language: null, clip: null, logs: [],
  pttDown: vi.fn(), pttUp: vi.fn(), setSpeaker: vi.fn(), dismissToast: vi.fn(), messages: [], ask: vi.fn(), stop: vi.fn(), clearMessages: vi.fn(),
  ...over,
});

function view(book: Book, talk: TalkUi, spoilers = false, p = 7) {
  act(() => root.render(createElement(TalkbackView, {
    book, para: p, speakerId: "narrator", onSpeaker: () => {}, pinned: null, talk, faces: [], faceFor: () => undefined,
    autoVoiceFor: () => "marcus", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers,
    onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
  })));
}

describe("TalkbackView", () => {
  it("shows the chapter badge, only met characters, and a spoiler toggle for novels", () => {
    view(novel, fakeTalk());
    expect(host.textContent).toContain("You're at The Boy Who Lived.");
    const opts = [...host.querySelectorAll("#tb-who option")].map((o) => o.textContent);
    expect(opts).toEqual(["Narrator", "Hagrid"]);
    expect(host.querySelector(".spoiler-toggle")).not.toBeNull();
  });

  it("with spoilers on lists everyone and says whole book", () => {
    view(novel, fakeTalk(), true);
    expect([...host.querySelectorAll("#tb-who option")].map((o) => o.textContent)).toEqual(["Narrator", "Hagrid", "Snape"]);
    expect(host.textContent).toContain("Spoilers on — whole book");
  });

  it("textbooks: narrator only (no character dropdown) and no spoiler toggle", () => {
    view(textbook, fakeTalk());
    expect(host.querySelector("#tb-who")).toBeNull();
    expect(host.querySelector(".spoiler-toggle")).toBeNull();
    expect(host.querySelector("#tb-voice")).not.toBeNull();
    expect(host.querySelector(".tb-name")!.textContent).toBe("Tutor");
  });

  it("sends a typed question and clears the box; Clear calls clearMessages", () => {
    const talk = fakeTalk({ messages: [{ id: 1, role: "user", speakerId: "narrator", text: "hi", via: "typed" }] });
    view(novel, talk);
    const input = host.querySelector<HTMLInputElement>("#tb-q")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Who is Hagrid?");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => { host.querySelector<HTMLFormElement>(".tb-text")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(talk.ask).toHaveBeenCalledWith("Who is Hagrid?");
    expect(host.querySelector<HTMLInputElement>("#tb-q")!.value).toBe("");
    act(() => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Clear chat")!.click(); });
    expect(talk.clearMessages).toHaveBeenCalled();
  });

  it("renders a tool chip, an interrupted reply, and the hands-free toggle (off by default)", () => {
    const setHandsFree = vi.fn();
    const messages = [
      { id: 1, role: "user", speakerId: "narrator", text: "Read chapter two" },
      { id: 2, role: "tool", speakerId: "tool", text: "Jumped to Chapter II" },
      { id: 3, role: "character", speakerId: "narrator", text: "Here we go", interrupted: true },
    ] as TalkUi["messages"];
    view(novel, fakeTalk({ messages, handsFree: false, setHandsFree }));
    expect(host.querySelector(".tool-chip")!.textContent).toBe("Jumped to Chapter II");
    expect(host.querySelector(".bubble.interrupted")!.textContent).toContain("interrupted");
    const hf = host.querySelector<HTMLButtonElement>(".hands-free")!;
    expect(hf.getAttribute("aria-pressed")).toBe("false");
    expect(hf.textContent).toMatch(/headphones/i); // #103: the label says what it needs
    expect(hf.title).toMatch(/headphones/i);
    act(() => hf.click());
    expect(setHandsFree).toHaveBeenCalledWith(true);
  });

  it("shows a labelled replay button only on finished replies that can be replayed", () => {
    const replay = vi.fn();
    const messages = [
      { id: 1, role: "user", speakerId: "narrator", text: "hi" },
      { id: 2, role: "character", speakerId: "narrator", text: "Hello." },
      { id: 3, role: "character", speakerId: "narrator", text: "Old.", },
    ] as TalkUi["messages"];
    view(novel, fakeTalk({ messages, replayable: new Set([2]), replayingId: null, replay }));
    const btns = host.querySelectorAll<HTMLButtonElement>(".replay-btn");
    expect(btns).toHaveLength(1);
    expect(btns[0].getAttribute("aria-label")).toBe("Play Narrator's reply again");
    act(() => btns[0].click());
    expect(replay).toHaveBeenCalledWith(2);
    view(novel, fakeTalk({ messages, replayable: new Set([2]), replayingId: 2, replay }));
    expect(host.querySelector(".replay-btn")!.getAttribute("aria-label")).toBe("Pause Narrator's reply again");
  });

  it("posts narrator recaps with a heading and play/pause; no lip-sync toggle or delay note (#92)", () => {
    const replay = vi.fn();
    const messages = [
      { id: 1, role: "narrator", speakerId: "narrator", text: "Alice fell.", recap: "Last chapter: I" },
      { id: 2, role: "narrator", speakerId: "narrator", text: "", recap: "This book so far", loading: true },
    ] as TalkUi["messages"];
    view(novel, fakeTalk({ messages, replayable: new Set([1]), replayingId: null, isPaused: (id) => id === 1, replay }));
    const b = host.querySelectorAll(".bubble.narrator");
    expect(b).toHaveLength(2);
    expect(b[0].querySelector(".who-line")!.textContent).toBe("Narrator · Recap: Last chapter: I");
    expect(b[1].querySelector("p")!.textContent).toBe("…");
    const btn = host.querySelector<HTMLButtonElement>(".replay-btn")!;
    expect(btn.getAttribute("aria-label")).toBe("Resume the narrator's recap");
    act(() => btn.click());
    expect(replay).toHaveBeenCalledWith(1);
    expect(host.querySelector("button[aria-haspopup='menu']")!.textContent).toBe("Recap");
    act(() => root.render(createElement(TalkbackView, {
      book: novel, para: 7, speakerId: "narrator", onSpeaker: () => {}, pinned: null, talk: fakeTalk(), faces: [], faceFor: () => ({ id: "f" }) as never,
      autoVoiceFor: () => "marcus", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
      onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
    })));
    expect(host.querySelector(".lipsync")).toBeNull();
    expect(host.querySelector("input[type=checkbox]")).toBeNull();
    expect(host.textContent).not.toMatch(/lip-?sync|\$0\.0|per minute|seconds later/i);
  });

  it("shows the narrator's face while a recap is read aloud (#92)", () => {
    const faceFor = (id: string) => ({ id: `f-${id}`, name: id, image: `/${id}.png`, voice: null, talk: null, idle: null });
    const render = (narrating: boolean) => act(() => root.render(createElement(TalkbackView, {
      book: novel, para: 7, speakerId: "hagrid", onSpeaker: () => {}, pinned: null, talk: fakeTalk({ narrating }), faces: [], faceFor,
      autoVoiceFor: () => "marcus", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
      onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
    })));
    render(false);
    expect(host.querySelector(".tb-face img")!.getAttribute("src")).toBe("/hagrid.png");
    render(true);
    expect(host.querySelector(".tb-face img")!.getAttribute("src")).toBe("/narrator.png");
  });

  it("the answer being spoken has play/pause from its first sentence", () => {
    const replay = vi.fn();
    const messages = [
      { id: 1, role: "user", speakerId: "narrator", text: "hi" },
      { id: 2, role: "character", speakerId: "narrator", text: "Hello.", pending: true },
    ] as TalkUi["messages"];
    view(novel, fakeTalk({ messages, liveId: 2, replayable: new Set([2]), replayingId: 2, isPaused: () => false, replay }));
    const btn = host.querySelector<HTMLButtonElement>(".replay-btn")!;
    expect(btn.getAttribute("aria-label")).toBe("Pause Narrator's reply");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    act(() => btn.click());
    expect(replay).toHaveBeenCalledWith(2);
    view(novel, fakeTalk({ messages, liveId: 2, replayable: new Set(), replayingId: null, isPaused: (id) => id === 2, replay }));
    expect(host.querySelector(".replay-btn")!.getAttribute("aria-label")).toBe("Resume Narrator's reply");
    const waiting = [messages[0], { ...messages[1], text: "" }] as TalkUi["messages"];
    view(novel, fakeTalk({ messages: waiting, liveId: 2, replayable: new Set(), replayingId: 2, replay }));
    expect(host.querySelector(".replay-btn")).toBeNull(); // nothing to pause before the first sentence
  });

  it("hold-to-talk with the keyboard presses and releases", () => {
    const talk = fakeTalk();
    view(novel, talk);
    const mic = host.querySelector<HTMLButtonElement>(".mic-button")!;
    act(() => { mic.dispatchEvent(new KeyboardEvent("keydown", { code: "Space", bubbles: true })); });
    expect(talk.pttDown).toHaveBeenCalledTimes(1);
    act(() => { mic.dispatchEvent(new KeyboardEvent("keyup", { code: "Space", bubbles: true })); });
    expect(talk.pttUp).toHaveBeenCalledTimes(1);
  });
});

describe("ModeToggle", () => {
  it("switches mode from the buttons and is disabled without audio", () => {
    const onMode = vi.fn();
    act(() => root.render(createElement(ModeToggle, { mode: "read", onMode, talkAvailable: true })));
    const [read, tb] = [...host.querySelectorAll("button")];
    expect(read.getAttribute("aria-pressed")).toBe("true");
    act(() => tb.click());
    expect(onMode).toHaveBeenCalledWith("talk");
    act(() => root.render(createElement(ModeToggle, { mode: "read", onMode, talkAvailable: false })));
    expect(host.querySelectorAll("button")[1].disabled).toBe(true);
  });
});

describe("useTalk transcript", () => {
  const reader = { isPlaying: () => true, currentIndex: () => 4, currentPara: () => 7, pause: vi.fn(), play: vi.fn(), resumeAt: vi.fn() };
  let latest: TalkUi;
  function Harness({ bookId }: { bookId: string }) {
    latest = useTalk({ bookId, enabled: true, reader, speakerId: "hagrid", voiceFor: () => "marcus", autoResume: true, health: { ok: true, boson_key_set: true, access_required: false, answer_backend: "gemini" }, talkback: true });
    return null;
  }

  it("a recap is posted by the narrator and read aloud, never asked of the model or remembered", async () => {
    const { streamAnswer } = await import("../src/api");
    (streamAnswer as ReturnType<typeof vi.fn>).mockClear();
    spoken.length = 0;
    function NarratorHarness() {
      latest = useTalk({ bookId: "hp9", enabled: true, reader, speakerId: "hagrid", voiceFor: () => "marcus", autoResume: true, narratorVoice: "belinda",
        health: { ok: true, boson_key_set: true, access_required: false, answer_backend: "gemini" }, talkback: true });
      return null;
    }
    await act(async () => { root.render(createElement(NarratorHarness)); });
    let release: (v: { text: string; heading: string }) => void = () => {};
    let done: Promise<void> = Promise.resolve();
    act(() => { done = latest.narrate("Last chapter", () => new Promise((r) => { release = r; })); });
    expect(latest.messages.map((m) => [m.role, m.text, m.loading])).toEqual([["narrator", "", true]]); // shown at once
    await act(async () => { release({ text: "Harry met Hagrid. They went to London.", heading: "Last chapter: The Keeper of the Keys" }); await done; });
    expect(latest.messages[0]).toMatchObject({ role: "narrator", text: "Harry met Hagrid. They went to London.", recap: "Last chapter: The Keeper of the Keys", loading: false });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(spoken).toEqual(["Harry met Hagrid. They went to London."]);
    expect(streamAnswer).not.toHaveBeenCalled();
    expect(latest.replayable.has(latest.messages[0].id)).toBe(true);
    // a failed recap is dropped with a toast
    await act(async () => { await latest.narrate("This book so far", async () => { throw new Error("offline"); }); });
    expect(latest.messages).toHaveLength(1);
    expect(latest.toast).toBe("Could not load the recap: offline");
    // nothing to recap: a quiet note, not read aloud
    await act(async () => { await latest.narrate("Last chapter", async () => ({ text: "", empty: "You're at the start of this book." })); });
    expect(latest.messages[1]).toMatchObject({ text: "You're at the start of this book.", quiet: true });
    expect(latest.replayable.has(latest.messages[1].id)).toBe(false);
  });

  it("a recap read aloud is lip-synced on the narrator's face, like answers, whenever a face is shown (#92, #113)", async () => {
    made.length = 0;
    const { speak } = await import("../src/api");
    let shown: string | null = "f-narrator";
    function NarratorHarness() {
      latest = useTalk({ bookId: "hp-lip", enabled: true, reader, speakerId: "hagrid", voiceFor: () => "marcus", autoResume: true, narratorVoice: "belinda",
        narratorFaceId: () => shown,
        health: { ok: true, boson_key_set: true, access_required: false, answer_backend: "gemini" }, talkback: true });
      return null;
    }
    await act(async () => { root.render(createElement(NarratorHarness)); });
    const before = made.length;
    await act(async () => { await latest.narrate("Last chapter", async () => ({ text: "Alice fell." })); });
    const narrator = made[before]; // the read-aloud makes its speaker when it starts reading
    expect(narrator.hooks?.sync).toBe(latest.faceSync); // same face sync (start gate, drift) as Talkback answers
    const clip = await narrator.synth("Alice fell.") as { audio_url: string; openVideo?: unknown };
    expect(speak).toHaveBeenLastCalledWith("Alice fell.", "belinda");
    expect(typeof clip.openVideo).toBe("function");
    shown = null; // no face shown for the narrator: plain audio, the talk loop stands in
    expect((await narrator.synth("Alice fell.") as { openVideo?: unknown }).openVideo).toBeUndefined();
  });

  it("the live answer pauses and resumes in place from its button, then becomes a replay", async () => {
    ctl.hold = true; ctl.log.length = 0; ctl.release.length = 0;
    try {
      await act(async () => { root.render(createElement(Harness, { bookId: "live" })); });
      await act(async () => { latest.ask("Who is Hagrid?"); await new Promise((r) => setTimeout(r, 0)); });
      const id = latest.messages[1].id;
      expect(latest.state).toBe("answering");
      expect(latest.liveId).toBe(id);
      expect(latest.replayingId).toBe(id);
      await act(async () => { latest.replay(id); });
      expect(ctl.log).toEqual(["pause"]);
      expect(latest.isPaused(id)).toBe(true);
      expect(latest.replayingId).toBeNull();
      await act(async () => { latest.replay(id); });
      expect(ctl.log).toEqual(["pause", "resume"]);
      expect(latest.isPaused(id)).toBe(false);
      expect(latest.replayingId).toBe(id);
      await act(async () => { ctl.release.shift()?.(); await new Promise((r) => setTimeout(r, 0)); });
      await act(async () => { ctl.release.shift()?.(); await new Promise((r) => setTimeout(r, 0)); });
      expect(latest.liveId).toBeNull();
      expect(latest.state).not.toBe("answering");
    } finally { ctl.hold = false; ctl.release.splice(0).forEach((r) => r()); }
  });

  it("a typed question appends You and the character, pauses the reader, and survives remounting; Clear empties it", async () => {
    await act(async () => { root.render(createElement(Harness, { bookId: "hp1" })); });
    await act(async () => { latest.ask("Who is Hagrid?"); });
    expect(reader.pause).toHaveBeenCalled();
    expect(reader.play).not.toHaveBeenCalled();
    expect(latest.messages.map((m) => [m.role, m.speakerId, m.text])).toEqual([
      ["user", "hagrid", "Who is Hagrid?"],
      ["character", "hagrid", "Hagrid is the keeper of keys. He is kind."],
    ]);
    expect(latest.messages[0].via).toBe("typed");

    // switching to Read mode and back unmounts and remounts the view and hook: the conversation is still there
    act(() => root.unmount());
    root = createRoot(host);
    await act(async () => { root.render(createElement(Harness, { bookId: "hp1" })); });
    expect(latest.messages).toHaveLength(2);

    // another book has its own conversation
    await act(async () => { root.render(createElement(Harness, { bookId: "other" })); });
    expect(latest.messages).toHaveLength(0);
    await act(async () => { root.render(createElement(Harness, { bookId: "hp1" })); });
    expect(latest.messages).toHaveLength(2);

    await act(async () => { latest.clearMessages(); });
    expect(latest.messages).toHaveLength(0);
  });
});
