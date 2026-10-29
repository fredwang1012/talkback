// @vitest-environment jsdom
/** #33: the face as a podcast-style player (mini, expanded, hidden), and #34's answer bubble. */
import { act, createElement, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book, Face } from "../src/api";
import AvatarPlayer from "../src/reader/AvatarPlayer";
import { isSwipeDown, latestExchange, leavingStopsAnswer, loadPlayerMode, playerKey, playerPortrait, savePlayerMode, sentenceText, useChatSince, type PlayerMode } from "../src/reader/playerLogic";
import type { ChatMessage } from "../src/talk/chatLog";
import type { FaceSync } from "../src/talk/faceSync";
import type { TalkUi } from "../src/talk/useTalk";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
HTMLMediaElement.prototype.play = vi.fn(async () => {});
HTMLMediaElement.prototype.pause = vi.fn();

const book = {
  id: "b", kind: "novel", narrator: null, sections: [],
  paragraphs: [{ idx: 0, text: "Alice was beginning to get very tired. So she was considering.", tts_chunks: [] }],
  characters: [{ id: "alice", name: "Alice", aliases: [], first_para: 0, persona: "", portrait: null, voice: "v", voice_tags: "" }],
} as unknown as Book;
const face = { id: "f1", name: "Alice", image: "/f1.png", talk: "/f1-talk.mp4", idle: "/f1-idle.mp4" } as unknown as Face;
const msg = (id: number, role: ChatMessage["role"], text: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
  ({ id, role, speakerId: "alice", text, ...extra });
const talk = (over: Partial<TalkUi> = {}) => ({
  state: "reading", speaking: false, caption: "", question: "", language: null, latency: null, toast: null, level: 0, logs: [], messages: [],
  faceSync: { setVideo: vi.fn() } as unknown as FaceSync, clipShown: false,
  pttDown() {}, pttUp() {}, setSpeaker() {}, dismissToast() {}, ask: vi.fn(), stop() {}, clearMessages() {}, ...over,
}) as unknown as TalkUi;

let root: Root; let host: HTMLElement;
beforeEach(() => { localStorage.clear(); host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); vi.useRealTimers(); });

type Opts = { t?: TalkUi; faceFor?: () => Face | undefined; readingFace?: Face | null; sinceId?: number; start?: PlayerMode; onToggle?: () => void };
/** The player as the Reader drives it: the mode is state, remembered on change. */
function mount({ t = talk(), faceFor = () => face, readingFace = null, sinceId = 0, start = "mini", onToggle = () => {} }: Opts = {}) {
  const readSync = { setVideo: vi.fn() } as unknown as FaceSync;
  function Host() {
    const [mode, setMode] = useState<PlayerMode>(start);
    return createElement(AvatarPlayer, {
      mode, onMode: (m: PlayerMode) => { setMode(m); savePlayerMode(m); }, book, name: "Alice", talk: t,
      face: { portrait: null, speakerId: "alice", faceFor, avatarSrc: null, avatarPlaying: false, readingFace, readSpeaking: false,
        levelRef: { current: 0 }, readLipSync: { sync: readSync, shown: false } },
      audioAvailable: true, playing: true, loading: false, onToggle, onPrev: () => {}, onNext: () => {}, atStart: false, atEnd: false,
      caption: "Alice was beginning to get very tired.", sinceId,
    });
  }
  act(() => root.render(createElement(Host)));
  return { readSync };
}
const button = (label: RegExp) => [...host.querySelectorAll<HTMLButtonElement>("button")].find((b) => label.test(b.getAttribute("aria-label") ?? b.textContent ?? ""));
const press = (key: string) => act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });

describe("player logic (#33)", () => {
  it("remembers the mode on this device; mini by default, also when storage is blocked or holds junk", () => {
    expect(loadPlayerMode()).toBe("mini");
    savePlayerMode("hidden");
    expect(loadPlayerMode()).toBe("hidden");
    localStorage.setItem("talkback:player", "huge");
    expect(loadPlayerMode()).toBe("mini");
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(loadPlayerMode()).toBe("mini");
    expect(() => savePlayerMode("expanded")).not.toThrow();
    get.mockRestore(); set.mockRestore();
  });

  it("Esc collapses the expanded view only", () => {
    expect(playerKey("expanded", "Escape")).toBe("mini");
    expect(playerKey("mini", "Escape")).toBeNull();
    expect(playerKey("hidden", "Escape")).toBeNull();
    expect(playerKey("expanded", "Enter")).toBeNull();
  });

  it("a swipe down is long, mostly vertical and quick", () => {
    expect(isSwipeDown({ x: 0, y: 0, t: 0 }, { x: 10, y: 120, t: 300 })).toBe(true);
    expect(isSwipeDown({ x: 0, y: 0, t: 0 }, { x: 0, y: 40, t: 300 })).toBe(false); // too short
    expect(isSwipeDown({ x: 0, y: 0, t: 0 }, { x: 150, y: 100, t: 300 })).toBe(false); // sideways
    expect(isSwipeDown({ x: 0, y: 100, t: 0 }, { x: 0, y: 0, t: 300 })).toBe(false); // up
    expect(isSwipeDown({ x: 0, y: 0, t: 0 }, { x: 0, y: 200, t: 2000 })).toBe(false); // a slow drag
  });

  it("the answer bubble shows the latest question asked this visit and its reply so far", () => {
    const m = [msg(1, "user", "old"), msg(2, "character", "old answer"), msg(3, "user", "Alice, why?"), msg(4, "character", "Because", { pending: true })];
    expect(latestExchange(m, 0)).toEqual({ replyId: 4, speakerId: "alice", question: "Alice, why?", answer: "Because", pending: true });
    expect(latestExchange(m, 4)).toBeNull(); // all from before this visit
    expect(latestExchange([...m, msg(5, "tool", "Jumped")], 2)?.replyId).toBe(4);
    expect(latestExchange([msg(1, "narrator", "recap")], 0)).toBeNull();
  });

  it("the player's portrait: a character's own (or none, for the letter circle), the narrator's only for the narrator", () => {
    const b = { ...book, narrator: { portrait: "/narrator.png" },
      characters: [{ id: "alice", portrait: null }, { id: "hatter", portrait: "/hatter.png" }] } as unknown as Book;
    expect(playerPortrait(b, "alice")).toBeNull();
    expect(playerPortrait(b, "hatter")).toBe("/hatter.png");
    expect(playerPortrait(b, "narrator")).toBe("/narrator.png");
    expect(playerPortrait({ ...b, narrator: null } as unknown as Book, "narrator")).toBeNull();
  });

  it("leaving the expanded view stops a live-face answer, except into Talkback (which keeps the live face)", () => {
    const on = { live: true, answering: true };
    expect(leavingStopsAnswer("expanded", "mini", on)).toBe(true);
    expect(leavingStopsAnswer("expanded", "hidden", on)).toBe(true);
    expect(leavingStopsAnswer("expanded", "talkback", on)).toBe(false);
    expect(leavingStopsAnswer("expanded", "expanded", on)).toBe(false);
    expect(leavingStopsAnswer("mini", "hidden", on)).toBe(false);
    expect(leavingStopsAnswer("expanded", "mini", { live: false, answering: true })).toBe(false); // clips: the answer goes on
    expect(leavingStopsAnswer("expanded", "mini", { live: true, answering: false })).toBe(false);
  });

  it("the caption is the sentence being read", () => {
    const chunk = { id: "c", speaker: "narrator", voice: "v", tts_text: "[calm] Alice was beginning", char_start: 0, char_end: 38, para: 0 };
    expect(sentenceText(book, chunk)).toBe("Alice was beginning to get very tired.");
    expect(sentenceText(book, { ...chunk, para: 9 })).toBe("Alice was beginning"); // no paragraph: the spoken text without tags
    expect(sentenceText(book, undefined)).toBe("");
  });
});

describe("AvatarPlayer (#33)", () => {
  it("mini: face, name, play/pause, expand and hide; no face assigned shows the letter circle", () => {
    const onToggle = vi.fn();
    mount({ faceFor: () => undefined, onToggle });
    expect(host.querySelector("[role=region]")?.getAttribute("aria-label")).toBe("Face player: Alice");
    expect(host.querySelector(".ap-name")?.textContent).toBe("Alice");
    expect(host.querySelector(".medallion .mono")?.textContent).toBe("A");
    act(() => button(/Pause reading/)?.click());
    expect(onToggle).toHaveBeenCalled();
    expect(button(/Expand the player/)).toBeTruthy();
    expect(button(/Hide the player/)).toBeTruthy();
  });

  it("expand moves focus in, Esc collapses and gives focus back to the expand button; the face is never remounted", () => {
    const { readSync } = mount({ readingFace: face });
    const clip = host.querySelector("video.rf-clip");
    act(() => button(/Expand the player/)?.click());
    const dialog = host.querySelector("[role=dialog]");
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(dialog); // the view itself, so Hold Space still talks (see the test below)
    expect(host.querySelector(".ap-caption")?.textContent).toContain("Alice was beginning");
    expect(host.querySelector("video.rf-clip")).toBe(clip); // the same lip-sync video: the sync is untouched
    press("Escape");
    expect(host.querySelector("[role=dialog]")).toBeNull();
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Expand the player");
    expect(host.querySelector("video.rf-clip")).toBe(clip);
    expect(readSync.setVideo).toHaveBeenCalledTimes(1);
    expect(loadPlayerMode()).toBe("mini");
  });

  it("the collapse button and a swipe down collapse too", () => {
    mount({ start: "expanded" });
    act(() => button(/Collapse the player/)?.click());
    expect(host.querySelector(".avatar-player.mini")).toBeTruthy();
    act(() => button(/Expand the player/)?.click());
    const el = host.querySelector<HTMLElement>(".avatar-player.expanded") as HTMLElement;
    const face = el.querySelector(".ap-face") as HTMLElement;
    act(() => { face.dispatchEvent(Object.assign(new MouseEvent("pointerdown", { bubbles: true, clientX: 10, clientY: 50 }))); });
    act(() => { face.dispatchEvent(Object.assign(new MouseEvent("pointerup", { bubbles: true, clientX: 14, clientY: 220 }))); });
    expect(host.querySelector(".avatar-player.mini")).toBeTruthy();
  });

  it("hide leaves a small tab that brings it back; the hidden face is unmounted (no lip-sync video)", () => {
    const { readSync } = mount();
    act(() => button(/Hide the player/)?.click());
    expect(host.querySelector(".medallion")).toBeNull();
    expect(readSync.setVideo).toHaveBeenLastCalledWith(null);
    const tab = button(/Show the face player/);
    expect(document.activeElement).toBe(tab);
    expect(loadPlayerMode()).toBe("hidden");
    act(() => tab?.click());
    expect(host.querySelector(".avatar-player.mini .medallion")).toBeTruthy();
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Expand the player");
  });

  it("expanding leaves the focus off the buttons: Hold Space talks (the Reader skips buttons) and never collapses", () => {
    mount();
    act(() => button(/Expand the player/)?.click());
    const focused = document.activeElement as HTMLElement;
    expect(["BUTTON", "INPUT", "SELECT", "TEXTAREA"]).not.toContain(focused.tagName); // the Reader's Hold-Space guard
    expect(host.querySelector("[role=dialog]")?.contains(focused)).toBe(true);
    act(() => {
      focused.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", bubbles: true, cancelable: true }));
      focused.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", bubbles: true, cancelable: true }));
    });
    expect(host.querySelector(".avatar-player.expanded")).toBeTruthy();
  });

  it("Shift+Tab from the expanded view itself wraps to its last control", () => {
    mount();
    act(() => button(/Expand the player/)?.click());
    const dialog = host.querySelector<HTMLElement>("[role=dialog]") as HTMLElement;
    const items = [...dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])")];
    act(() => { dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true })); });
    expect(document.activeElement).toBe(items[items.length - 1]);
  });

  it("Tab stays inside the expanded view", () => {
    mount({ start: "expanded" });
    const dialog = host.querySelector<HTMLElement>("[role=dialog]") as HTMLElement;
    const items = [...dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])")];
    items[items.length - 1].focus();
    act(() => { dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })); });
    expect(document.activeElement).toBe(items[0]);
  });

  it("the expanded view has the conversation and an ask box that asks through the same talk loop", () => {
    const t = talk({ messages: [msg(1, "user", "Who are you?"), msg(2, "character", "I'm Alice.")] });
    mount({ t, start: "expanded" });
    expect(host.querySelector("[role=log]")?.textContent).toContain("I'm Alice.");
    const input = host.querySelector<HTMLInputElement>(".ask-text input") as HTMLInputElement;
    act(() => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      set?.call(input, "Alice, how tall are you?");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => { (host.querySelector(".ask-text") as HTMLFormElement).requestSubmit(); });
    expect(t.ask).toHaveBeenCalledWith("Alice, how tall are you?");
  });
});

describe("answer bubble while reading (#34)", () => {
  it("shows the answer as it comes, can be dismissed, and goes by itself a while after reading resumed", () => {
    vi.useFakeTimers();
    const live = [msg(3, "user", "Alice, why?"), msg(4, "character", "Because I was curious.", { pending: true })];
    mount({ t: talk({ state: "answering", messages: live }), sinceId: 2 });
    expect(host.querySelector(".ap-bubble")?.textContent).toContain("Because I was curious.");
    act(() => button(/Dismiss the answer/)?.click());
    expect(host.querySelector(".ap-bubble")).toBeNull();
    act(() => root.unmount());
    root = createRoot(host);
    mount({ t: talk({ state: "reading", messages: [live[0], { ...live[1], pending: false }] }), sinceId: 2 });
    expect(host.querySelector(".ap-bubble")).toBeTruthy();
    act(() => { vi.advanceTimersByTime(12_000); });
    expect(host.querySelector(".ap-bubble")).toBeNull();
  });

  it("goes by itself even while the Reader re-renders on every word", () => {
    vi.useFakeTimers();
    const t = talk({ state: "reading", messages: [msg(3, "user", "Alice, why?"), msg(4, "character", "Because.")] });
    let rerender = () => {};
    function Host() {
      const [n, setN] = useState(0);
      rerender = () => setN(n + 1);
      return createElement(AvatarPlayer, {
        mode: "mini", onMode: () => {}, book, name: "Alice", talk: t,
        face: { portrait: null, speakerId: "alice", faceFor: () => face, avatarSrc: null, avatarPlaying: false, readingFace: null, readSpeaking: false,
          levelRef: { current: 0 }, readLipSync: { sync: { setVideo: vi.fn() } as unknown as FaceSync, shown: false } },
        audioAvailable: true, playing: true, loading: false, onToggle: () => {}, onPrev: () => {}, onNext: () => {}, atStart: false, atEnd: false,
        caption: `word ${n}`, sinceId: 2,
      });
    }
    act(() => root.render(createElement(Host)));
    expect(host.querySelector(".ap-bubble")).toBeTruthy();
    for (let i = 0; i < 30; i++) { act(() => { vi.advanceTimersByTime(500); }); act(() => rerender()); } // a new word every 500 ms
    expect(host.querySelector(".ap-bubble")).toBeNull();
  });

  it("after Clear chat (ids start again at 1) a new question's answer shows again", () => {
    let setMsgs: (m: ChatMessage[]) => void = () => {};
    function Host() {
      const [messages, set] = useState<ChatMessage[]>([msg(1, "user", "old"), msg(2, "character", "old answer")]);
      setMsgs = set;
      const since = useChatSince(messages);
      const ex = latestExchange(messages, since);
      return createElement("output", null, ex ? ex.answer : "none");
    }
    act(() => root.render(createElement(Host)));
    expect(host.textContent).toBe("none"); // from before this visit
    act(() => setMsgs([])); // Clear chat
    act(() => setMsgs([msg(1, "user", "Alice, why?"), msg(2, "character", "Because.")]));
    expect(host.textContent).toBe("Because.");
  });

  it("older chat from before this visit never pops up", () => {
    mount({ t: talk({ messages: [msg(1, "user", "q"), msg(2, "character", "a")] }), sinceId: 2 });
    expect(host.querySelector(".ap-bubble")).toBeNull();
  });
});