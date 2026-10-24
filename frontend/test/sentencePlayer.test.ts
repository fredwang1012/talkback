import { describe, expect, it, vi } from "vitest";
import { createSentenceSpeaker, type AudioLike } from "../src/talk/sentencePlayer";

function fakeAudio() {
  const clips: (AudioLike & { url: string; paused: boolean })[] = [];
  const create = (url: string) => {
    const a = { url, paused: false, onended: null, onerror: null, play: () => Promise.resolve(), pause() { a.paused = true; } } as AudioLike & { url: string; paused: boolean };
    clips.push(a);
    return a;
  };
  return { clips, create };
}

describe("createSentenceSpeaker", () => {
  it("resolves on ended and reuses a prefetch", async () => {
    const speak = vi.fn(async (t: string) => ({ audio_url: `/m/${t}.wav` }));
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(speak, create);
    s.prefetch("a");
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onended?.();
    await done;
    expect(speak).toHaveBeenCalledTimes(1);
    expect(clips[0].url).toBe("/m/a.wav");
  });

  it("rejects on audio error", async () => {
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), create);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onerror?.();
    await expect(done).rejects.toThrow();
  });

  it("stop pauses the clip and resolves the pending play", async () => {
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), create);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    s.stop();
    await done;
    expect(clips[0].paused).toBe(true);
  });

  it("stop while still synthesising means the clip never starts", async () => {
    const { clips, create } = fakeAudio();
    let release!: () => void;
    const s = createSentenceSpeaker(() => new Promise((r) => { release = () => r({ audio_url: "/x" }); }), create);
    const done = s.play("a");
    s.stop();
    release();
    await done;
    expect(clips).toHaveLength(0);
  });
});
