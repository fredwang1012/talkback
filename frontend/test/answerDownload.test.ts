// @vitest-environment jsdom
/** #80: "Download clip" on a finished answer: what is sent, the free still fallback, the picture conversion. */
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { ClipError, clipPlan, dispositionName, fetchAnswerClip, makeAnswerClip, rgbaToI420, toBase64, STILL_SIDE } from "../src/talk/answerDownload";
import type { ReplayItem } from "../src/talk/replay";
import TalkbackView from "../src/reader/TalkbackView";
import type { TalkUi } from "../src/talk/useTalk";

const A = `/media/tts/${"a".repeat(64)}.mp3`;
const B = `/media/tts/${"b".repeat(64)}.mp3`;
const faced: ReplayItem = { kind: "sentences", parts: [
  { text: "One.", clip: { audio_url: A, lipsync: { voice: "belinda", faceId: "f1" } } },
  { text: "Two.", clip: { audio_url: B, lipsync: { voice: "belinda", faceId: "f1" } } },
] };

describe("clipPlan", () => {
  it("sends each sentence's audio, voice and face when every sentence was lip-synced", () => {
    expect(clipPlan(faced)).toEqual({ faced: true, sentences: [
      { audio_url: A, voice: "belinda", face_id: "f1" }, { audio_url: B, voice: "belinda", face_id: "f1" },
    ] });
  });

  it("no face (or not on every sentence): the voice over a still", () => {
    const mixed: ReplayItem = { kind: "sentences", parts: [faced.parts[0], { text: "Two.", clip: { audio_url: B } }] };
    expect(clipPlan(mixed)).toEqual({ faced: false, sentences: [{ audio_url: A, voice: "" }, { audio_url: B, voice: "" }] });
  });

  it("nothing to download: a Realtime voice reply, an empty one, audio not from the cache", () => {
    expect(clipPlan({ kind: "pcm", chunks: ["AAAA"] })).toBeNull();
    expect(clipPlan({ kind: "sentences", parts: [] })).toBeNull();
    expect(clipPlan(undefined)).toBeNull();
    expect(clipPlan({ kind: "sentences", parts: [{ text: "x", clip: { audio_url: "blob:http://x/1" } }] })).toBeNull();
  });
});

describe("makeAnswerClip", () => {
  const blob = new Blob(["mp4"]);
  const clip = { blob, name: "talkback-alice-Alice.mp4" };
  it("asks for the joined face videos, and never draws a still for them", async () => {
    const fetchClip = vi.fn(async () => clip);
    const still = vi.fn(async () => new Uint8Array(4));
    const r = await makeAnswerClip("alice", "Alice", clipPlan(faced)!, still, fetchClip);
    expect(r).toEqual({ ...clip, note: null });
    expect(fetchClip).toHaveBeenCalledWith({ book_id: "alice", speaker: "Alice", sentences: clipPlan(faced)!.sentences });
    expect(still).not.toHaveBeenCalled();
  });

  it("a face video never made (409): the free still version, with a note; no render is asked for", async () => {
    const fetchClip = vi.fn(async (b: { still?: string }) => { if (!b.still) throw new ClipError("1 of 2 never made", 409); return clip; });
    const r = await makeAnswerClip("alice", "Alice", clipPlan(faced)!, async () => new Uint8Array([1, 2, 3]), fetchClip);
    expect(r.blob).toBe(blob);
    expect(r.name).toBe(clip.name);
    expect(r.note).toMatch(/never made.*nothing was rendered or charged/);
    expect(fetchClip).toHaveBeenLastCalledWith({ book_id: "alice", speaker: "Alice", still: "AQID",
      sentences: [{ audio_url: A, voice: "belinda" }, { audio_url: B, voice: "belinda" }] });
  });

  it("a deleted face (404) or clips that do not join (422): the still version too, each with its reason", async () => {
    for (const [status, why] of [[404, /face is no longer available/], [422, /could not be joined/]] as const) {
      const fetchClip = vi.fn(async (b: { still?: string }) => { if (!b.still) throw new ClipError("no", status); return clip; });
      const r = await makeAnswerClip("alice", "Alice", clipPlan(faced)!, async () => new Uint8Array(1), fetchClip);
      expect(r.blob).toBe(blob);
      expect(r.note).toMatch(why);
      expect(fetchClip).toHaveBeenCalledTimes(2);
    }
  });

  it("other errors are reported, not retried (busy, rate limited, too long, not a server answer)", async () => {
    for (const err of [new ClipError("Too many", 429), new ClipError("Too long", 413), new Error("offline")]) {
      const fetchClip = vi.fn(async () => { throw err; });
      await expect(makeAnswerClip("alice", "Alice", clipPlan(faced)!, async () => new Uint8Array(1), fetchClip)).rejects.toThrow(err.message);
      expect(fetchClip).toHaveBeenCalledTimes(1);
    }
  });
});

describe("fetchAnswerClip", () => {
  it("names the file as the server's Content-Disposition does", async () => {
    const fetchMock = vi.fn(async () => new Response("mp4", { headers: {
      "Content-Type": "video/mp4", "Content-Disposition": 'attachment; filename="talkback-alice-The-Cat.mp4"' } }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const r = await fetchAnswerClip({ book_id: "alice", speaker: "The Cat", sentences: [] });
      expect(r.name).toBe("talkback-alice-The-Cat.mp4");
      expect(await r.blob.text()).toBe("mp4");
      fetchMock.mockImplementationOnce(async () => new Response("mp4"));
      expect((await fetchAnswerClip({ book_id: "alice", speaker: "", sentences: [] })).name).toBe("talkback-answer.mp4");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("reads quoted and bare file names", () => {
    expect(dispositionName('attachment; filename="a b.mp4"')).toBe("a b.mp4");
    expect(dispositionName("attachment; filename=x.mp4")).toBe("x.mp4");
    expect(dispositionName(null)).toBeNull();
  });
});

describe("picture and file name", () => {
  it("RGBA to I420: black, white and red", () => {
    const px = (r: number, g: number, b: number) => new Uint8Array(Array.from({ length: 4 }, () => [r, g, b, 255]).flat());
    expect([...rgbaToI420(px(0, 0, 0), 2, 2)]).toEqual([16, 16, 16, 16, 128, 128]);
    expect([...rgbaToI420(px(255, 255, 255), 2, 2)]).toEqual([235, 235, 235, 235, 128, 128]);
    const red = rgbaToI420(px(255, 0, 0), 2, 2);
    expect(red[0]).toBe(81);
    expect(red[4]).toBeLessThan(128); // U
    expect(red[5]).toBe(240); // V
    expect(rgbaToI420(new Uint8Array(STILL_SIDE * STILL_SIDE * 4), STILL_SIDE, STILL_SIDE)).toHaveLength(STILL_SIDE * STILL_SIDE * 1.5);
  });

  it("base64 of large buffers", () => {
    expect(toBase64(new Uint8Array([104, 105]))).toBe("aGk=");
    expect(toBase64(new Uint8Array(100000)).length).toBe(Math.ceil(100000 / 3) * 4);
  });
});

describe("Download clip button", () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  it("shows on finished character replies that have cached sentences only", () => {
    const book = { id: "alice", title: "A", kind: "novel", sections: [], paragraphs: [], characters: [], narrator: null } as unknown as Book;
    const items: Record<number, ReplayItem> = { 2: faced, 4: { kind: "pcm", chunks: ["AA"] } };
    const talk = {
      state: "talk-idle", caption: "", toast: null, level: 0, latency: null, speaking: false, question: "", language: null, logs: [],
      pttDown() {}, pttUp() {}, setSpeaker() {}, dismissToast() {}, ask() {}, stop() {}, clearMessages() {}, replay() {},
      isPaused: () => false, replayable: new Set([2, 4]), replayingId: null, liveId: null,
      replayItem: (id: number) => items[id],
      messages: [
        { id: 1, role: "user", speakerId: "narrator", text: "q" }, { id: 2, role: "character", speakerId: "narrator", text: "One. Two." },
        { id: 3, role: "user", speakerId: "narrator", text: "q2" }, { id: 4, role: "character", speakerId: "narrator", text: "Hi." },
      ],
    } as unknown as TalkUi;
    const host = document.createElement("div");
    const root = createRoot(host);
    act(() => root.render(createElement(TalkbackView, {
      book, para: 0, speakerId: "narrator", onSpeaker: () => {}, pinned: null, talk, faces: [], faceFor: () => undefined,
      autoVoiceFor: () => "marcus", choice: { voice: null, face: null }, onChoice: () => {}, voices: [], spoilers: false,
      onSpoilers: () => {}, portrait: null, onBack: () => {}, onRecap: () => {}, onVoices: () => {},
    })));
    const buttons = [...host.querySelectorAll(".clip-btn")];
    expect(buttons.map((b) => b.getAttribute("aria-label"))).toEqual(["Download Narrator's reply as a video"]);
    act(() => root.unmount());
  });
});
