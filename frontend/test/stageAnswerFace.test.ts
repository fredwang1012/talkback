// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book, Face } from "../src/api";
import { answerOnStage, stageFace } from "../src/reader/readFace";
import Stage from "../src/reader/Stage";
import type { FaceSync } from "../src/talk/faceSync";
import type { TalkUi } from "../src/talk/useTalk";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
HTMLMediaElement.prototype.play = vi.fn(async () => {});
HTMLMediaElement.prototype.pause = vi.fn();

const book = { id: "b", kind: "novel", narrator: null, sections: [], paragraphs: [], characters: [] } as unknown as Book;
const face = { id: "f1", name: "Alice", image: "/f1.png", talk: "/f1-talk.mp4", idle: "/f1-idle.mp4" } as unknown as Face;
const talkSync = { setVideo: vi.fn() } as unknown as FaceSync;
const readSync = { setVideo: vi.fn() } as unknown as FaceSync;
const talk = (over: Partial<TalkUi> = {}) => ({
  state: "reading", speaking: false, caption: "", question: "", language: null, latency: null, toast: null, level: 0, logs: [], messages: [],
  faceSync: talkSync, clipShown: false,
  pttDown() {}, pttUp() {}, setSpeaker() {}, dismissToast() {}, ask() {}, stop() {}, clearMessages() {}, ...over,
}) as unknown as TalkUi;

let root: Root; let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

function render(t: TalkUi, { readingFace = null as Face | null, readClipShown = false, avatarSrc = null as string | null } = {}) {
  act(() => root.render(createElement(Stage, {
    book, para: 0, speakerId: "narrator", onSpeaker: () => {}, health: null, talk: t, audioAvailable: true, avatarSrc, avatarPlaying: false,
    onMode: () => {}, pinned: null, needsCode: false, onCode: () => {}, onOpenLab: () => {}, faceFor: () => face,
    readingFace, readSpeaking: false, levelRef: { current: 0 }, readLipSync: { sync: readSync, shown: readClipShown },
  })));
}
const talkLoop = () => host.querySelector<HTMLVideoElement>(`video.rf-talk[src="/f1-talk.mp4"]`);
const readClip = () => host.querySelector<HTMLVideoElement>("video.rf-clip");

describe("which face the read-mode stage shows", () => {
  it("an answer in progress (asked, thinking or speaking) takes the stage", () => {
    expect(answerOnStage("reading", false)).toBe(false);
    expect(answerOnStage("talk-idle", false)).toBe(false);
    expect(answerOnStage("resuming", false)).toBe(false);
    for (const s of ["listening", "transcribing", "retrieving", "answering"] as const) expect(answerOnStage(s, false)).toBe(true);
    expect(answerOnStage("talk-idle", true)).toBe(true); // still sounding
  });
  it("the answer's face beats the reading face and the chunk's pre-rendered clip", () => {
    expect(stageFace({ answering: true, avatar: true, reading: true, face: true })).toBe("talk");
    expect(stageFace({ answering: false, avatar: true, reading: true, face: true })).toBe("avatar");
    expect(stageFace({ answering: false, avatar: false, reading: true, face: true })).toBe("read");
    expect(stageFace({ answering: false, avatar: false, reading: false, face: true })).toBe("talk");
    expect(stageFace({ answering: true, avatar: true, reading: false, face: false })).toBe("portrait");
  });
});

describe("Stage: a spoken answer in read mode animates the face like Talkback", () => {
  it("the paused reading's frozen lip-sync frame does not cover the answering face", () => {
    // the reader was lip-synced when the mic was pressed: its clip froze on the pause and still reports shown
    render(talk({ state: "answering", speaking: true }), { readClipShown: true });
    expect(talkLoop()?.className).toContain("on"); // the talk loop moves with the answer
    expect(readClip()?.className).not.toContain("on"); // the frozen reading clip is down
  });

  it("the face goes back to idle when the answer ends, and the reading's clip comes back when reading resumes", () => {
    render(talk({ state: "answering", speaking: true }), { readClipShown: true });
    render(talk({ state: "talk-idle", speaking: false }), { readClipShown: true });
    expect(talkLoop()?.className).not.toContain("on");
    render(talk({ state: "reading", speaking: false }), { readingFace: face, readClipShown: true });
    expect(readClip()?.className).toContain("on");
  });

  it("an answer over a chunk with a pre-rendered clip shows the answering face, not the paused clip", () => {
    render(talk({ state: "answering", speaking: true }), { avatarSrc: "/media/avatar/x.mp4" });
    expect(host.querySelector(`video[src="/media/avatar/x.mp4"]`)).toBeNull();
    expect(talkLoop()?.className).toContain("on");
  });

  it("an answer while the reading face is still up (reader not yet paused) uses the answer's face and sync", () => {
    render(talk({ state: "answering", speaking: true }), { readingFace: face });
    expect(talkLoop()?.className).toContain("on");
    expect(talkSync.setVideo).toHaveBeenCalled(); // the answer's lip-sync video is mounted (Gemini path clips)
  });
});
