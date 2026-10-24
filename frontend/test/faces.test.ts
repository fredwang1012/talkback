import { describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import { sectionIndexAt, sectionRange } from "../src/reader/BookText";
import { createSentenceSpeaker, type AudioLike } from "../src/talk/sentencePlayer";

function fakeAudio() {
  const clips: AudioLike[] = [];
  const create = (_url: string) => {
    const a = { onended: null, onerror: null, play: () => Promise.resolve(), pause() {} } as AudioLike;
    clips.push(a);
    return a;
  };
  return { clips, create };
}

describe("lip-synced clips", () => {
  it("reports the sentence's video as its audio starts and clears it when it ends", async () => {
    const { clips, create } = fakeAudio();
    const onClip = vi.fn();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3", video_url: "/v.mp4" }), create, onClip);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    expect(onClip).toHaveBeenLastCalledWith("/v.mp4");
    clips[0].onended?.();
    await done;
    expect(onClip).toHaveBeenLastCalledWith(null);
  });

  it("clears the clip on barge-in and when there is no video", async () => {
    const { clips, create } = fakeAudio();
    const onClip = vi.fn();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3" }), create, onClip);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    expect(onClip).toHaveBeenLastCalledWith(null);
    onClip.mockClear();
    s.stop();
    await done;
    expect(onClip).toHaveBeenCalledWith(null);
  });
});

describe("chapter helpers", () => {
  const book = {
    sections: [{ id: "s1", title: "One", start_para: 0 }, { id: "s2", title: "Two", start_para: 5 }, { id: "s3", title: "Three", start_para: 9 }],
    paragraphs: Array.from({ length: 12 }, (_, i) => ({ idx: i })),
  } as unknown as Book;

  it("finds the chapter of a paragraph and its range", () => {
    expect([0, 4, 5, 8, 9, 11].map((p) => sectionIndexAt(book, p))).toEqual([0, 0, 1, 1, 2, 2]);
    expect(sectionRange(book, 0)).toEqual([0, 5]);
    expect(sectionRange(book, 2)).toEqual([9, 12]);
  });
});
