// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { resetChatMemory } from "../src/talk/chatLog";

vi.mock("../src/audio/playback", () => ({ RingPlayer: class { async close() {} } }));
vi.mock("../src/audio/capture", () => ({ MicCapture: class { async stop() {} } }));
vi.mock("../src/talk/sentencePlayer", () => ({
  createSentenceSpeaker: () => ({ prefetch() {}, play: async () => {}, stop() {} }),
}));
vi.mock("../src/api", async (orig) => ({
  ...(await orig<typeof import("../src/api")>()),
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
    onSpoilers: () => {}, lipSyncable: false, lipSync: false, onLipSync: () => {}, portrait: null, onBack: () => {},
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
    act(() => { [...host.querySelectorAll("button")].find((b) => b.textContent === "Clear")!.click(); });
    expect(talk.clearMessages).toHaveBeenCalled();
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
