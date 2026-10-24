// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import Stage from "../src/reader/Stage";
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
  it("offers Talk to buttons for main met characters; clicking opens Talkback with that speaker", () => {
    const onSpeaker = vi.fn(); const onMode = vi.fn();
    act(() => root.render(createElement(Stage, {
      book, para: 3, speakerId: "narrator", onSpeaker, health: null, talk, audioAvailable: true, avatarSrc: null, avatarPlaying: false,
      onMode, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => undefined,
      readingFace: null, spoilers: false,
    })));
    const labels = [...host.querySelectorAll(".talk-to strong")].map((e) => e.textContent);
    expect(labels).toEqual(["Talk to the Narrator", "Talk to Ron"]); // no minor Peeves; Snape is locked
    act(() => { [...host.querySelectorAll<HTMLButtonElement>(".talk-to")][1].click(); });
    expect(onSpeaker).toHaveBeenCalledWith("ron");
    expect(onMode).toHaveBeenCalledWith("talk");
  });
});

describe("Stage read-mode lip-sync toggle (#80)", () => {
  const render = (available: boolean, on = false) => {
    const onToggle = vi.fn();
    const sync = { setVideo: vi.fn() } as unknown as FaceSync;
    act(() => root.render(createElement(Stage, {
      book, para: 3, speakerId: "narrator", onSpeaker: () => {}, health: null, talk, audioAvailable: true, avatarSrc: null, avatarPlaying: false,
      onMode: () => {}, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => undefined,
      readingFace: null, spoilers: false, readSpeaking: false, levelRef: { current: 0 },
      readLipSync: { available, on, onToggle, sync, shown: false },
    })));
    return { onToggle, sync };
  };

  it("is offered with its cost only when a face on screen can lip-sync, and keeps one persistent clip video", () => {
    const { sync } = render(false);
    expect(host.querySelector(".stage .lipsync")).toBeNull();
    expect(sync.setVideo).toHaveBeenCalledWith(host.querySelector("video.rf-clip")); // the sync owns the clip video
    const { onToggle } = render(true);
    const label = host.querySelector(".stage .lipsync")!;
    expect(label.textContent).toContain("Lip-sync while reading");
    expect(label.textContent).toContain("$0.09 per minute");
    const box = label.querySelector<HTMLInputElement>("input[type=checkbox]")!;
    expect(box.checked).toBe(false); // off by default
    act(() => { box.click(); });
    expect(onToggle).toHaveBeenCalledWith(true);
  });
});
