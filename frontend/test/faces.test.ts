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
  it("joins the video to the voice when it is ready, and detaches when the sentence ends", async () => {
    const { clips, create } = fakeAudio();
    const sync = { holding: false, join: vi.fn(), leave: vi.fn() };
    const release = vi.fn();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3", openVideo: async () => ({ url: "/v.mp4", release }) }), create, { sync });
    const done = s.play("a");
    await vi.waitFor(() => expect(sync.join).toHaveBeenCalledTimes(1));
    expect(sync.join.mock.calls[0][1].url).toBe("/v.mp4");
    clips[0].onended?.();
    await done;
    expect(sync.leave).toHaveBeenCalled();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
  });

  it("starts the voice without waiting for a slow video, and releases it if it arrives after the end", async () => {
    const { clips, create } = fakeAudio();
    const sync = { holding: false, join: vi.fn(), leave: vi.fn() };
    const release = vi.fn();
    let open!: () => void;
    const s = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3", openVideo: () => new Promise((r) => { open = () => r({ url: "/v", release }); }) }), create, { sync });
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1)); // audio is up while the stream is still opening
    clips[0].onended?.();
    await done;
    open();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(sync.join).not.toHaveBeenCalled();
  });

  it("plays on without a video when the stream fails or there is none", async () => {
    const { clips, create } = fakeAudio();
    const sync = { holding: false, join: vi.fn(), leave: vi.fn() };
    const s = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3", openVideo: async () => { throw new Error("boson"); } }), create, { sync });
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onended?.();
    await done;
    expect(sync.join).not.toHaveBeenCalled();
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
