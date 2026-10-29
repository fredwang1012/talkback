// @vitest-environment jsdom
import { Fragment, act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import Stage from "../src/reader/Stage";
import { StageFace } from "../src/reader/StageFace";
import type { FaceSync } from "../src/talk/faceSync";
import type { TalkUi } from "../src/talk/useTalk";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const ch = (id: string, name: string, first_para: number, main?: boolean) => ({ id, name, first_para, persona: `${name} persona`, portrait: null, voice: "marcus", voice_tags: "", aliases: [], main });
const book = { id: "b", kind: "novel", narrator: null, sections: [], paragraphs: [],
  characters: [ch("ron", "Ron", 0), ch("peeves", "Peeves", 0, false), ch("snape", "Snape", 40)] } as unknown as Book;
const talk = { state: "reading", speaking: false, caption: "", question: "", language: null, latency: null, toast: null, level: 0, clip: null, logs: [], messages: [],
  pttDown() {}, pttUp() {}, setSpeaker() {}, dismissToast() {}, ask() {}, stop() {}, clearMessages() {} } as unknown as TalkUi;

let root: Root; let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

describe("Stage cast list", () => {
  it("offers Talk to buttons for main met characters; clicking picks who answers and stays in reading (#34)", () => {
    const onSpeaker = vi.fn(); const onMode = vi.fn();
    act(() => root.render(createElement(Stage, {
      book, para: 3, speakerId: "narrator", onSpeaker, health: null, talk, audioAvailable: true,
      onMode, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => undefined,
    })));
    const labels = [...host.querySelectorAll(".talk-to strong")].map((e) => e.textContent);
    expect(labels).toEqual(["Talk to the Narrator", "Talk to Ron"]); // no minor Peeves; Snape is locked
    act(() => { [...host.querySelectorAll<HTMLButtonElement>(".talk-to")][1].click(); });
    expect(onSpeaker).toHaveBeenCalledWith("ron");
    expect(onMode).not.toHaveBeenCalled(); // no switch to Talkback needed to talk while reading
  });
});

describe("Stage read-mode lip-sync (#80, #92: always on, no toggle)", () => {
  it("has no lip-sync toggle, price or delay note, and keeps one persistent clip video", () => {
    const sync = { setVideo: vi.fn() } as unknown as FaceSync;
    act(() => root.render(createElement(Fragment, null,
      createElement(Stage, {
        book, para: 3, speakerId: "narrator", onSpeaker: () => {}, health: null, talk, audioAvailable: true,
        onMode: () => {}, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => undefined,
      }),
      // the face (and its clip video) is in the avatar player since #33
      createElement(StageFace, {
        name: "Narrator", portrait: null, speakerId: "narrator", talk, avatarSrc: null, avatarPlaying: false, faceFor: () => undefined,
        readingFace: null, readSpeaking: false, levelRef: { current: 0 }, readLipSync: { sync, shown: false },
      }))));
    expect(host.querySelector(".lipsync")).toBeNull();
    expect(host.querySelector("input[type=checkbox]")).toBeNull();
    expect(host.querySelector(".hands-free")).toBeNull(); // the talk stub has no hands-free
    expect(host.textContent).not.toMatch(/lip-?sync|\$0\.0|per minute|seconds later|moment later/i);
    expect(host.querySelectorAll("video.rf-clip")).toHaveLength(1);
    expect(sync.setVideo).toHaveBeenCalledWith(host.querySelector("video.rf-clip")); // the sync owns the clip video
  });
});

describe("Stage in read mode (#94)", () => {
  const render = (speakerId = "narrator") => act(() => root.render(createElement(Stage, {
    book, para: 3, speakerId, onSpeaker: () => {}, health: null, talk, audioAvailable: true,
    onMode: () => {}, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => undefined,
  })));

  it("never shows the Talkback spoiler note; unmet characters stay locked (the spoiler setting is Talkback's)", () => {
    render();
    expect(host.textContent).not.toMatch(/spoiler|whole book|full book|may reveal/i);
    expect(host.querySelector(".spoilers-on")).toBeNull();
    expect(host.textContent).toContain("Story position: paragraph 4.");
    expect(host.textContent).not.toContain("Talk to Snape");
  });

  it("shows the reading position, not what a character knows", () => {
    render("ron");
    expect(host.textContent).not.toContain("knows");
    expect(host.textContent).toContain("Story position: paragraph 4.");
    expect(host.textContent).toContain("1 more character appears later.");
  });
});
