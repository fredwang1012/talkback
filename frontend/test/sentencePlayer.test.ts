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

describe("speed and face signals", () => {
  const evAudio = () => {
    const l: Record<string, (() => void)[]> = {};
    const a = {
      onended: null as (() => void) | null, onerror: null, paused: false,
      play: () => Promise.resolve(), pause() { a.paused = true; },
      addEventListener(t: string, f: () => void) { (l[t] ||= []).push(f); },
      removeEventListener(t: string, f: () => void) { l[t] = (l[t] || []).filter((x) => x !== f); },
      fire(t: string) { (l[t] || []).forEach((f) => f()); },
    };
    return a;
  };

  it("preloads the audio of a prefetched sentence before it is played", async () => {
    const created: string[] = [];
    const s = createSentenceSpeaker(async (t) => ({ audio_url: `/m/${t}` }), (u) => { created.push(u); return evAudio(); });
    s.prefetch("a", 1);
    s.prefetch("b", 2);
    await vi.waitFor(() => expect(created).toEqual(["/m/a", "/m/b"]));
  });

  it("opens the lip-sync stream only for rank 0 prefetches", async () => {
    const open = vi.fn(async () => ({ url: "u", release() {} }));
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x", openVideo: open }), () => evAudio());
    s.prefetch("a", 0);
    s.prefetch("b", 1);
    await vi.waitFor(() => expect(open).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 5));
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("reports playing from media events, ignores a pause the sync caused, and logs stage timings", async () => {
    const a = evAudio();
    const playing: boolean[] = [];
    const stages: string[] = [];
    const sync = { holding: false, join() {}, leave() {} };
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), () => a, {
      sync, onPlaying: (v) => playing.push(v), onTiming: (e) => stages.push(e.stage),
    });
    const done = s.play("hi");
    await vi.waitFor(() => expect(stages).toContain("tts"));
    a.fire("playing");
    sync.holding = true;
    a.fire("pause"); // the video is catching up
    sync.holding = false;
    a.fire("pause"); // a real pause
    a.fire("playing");
    a.onended?.();
    await done;
    expect(playing).toEqual([true, false, true, false]);
    expect(stages).toEqual(["tts", "audio-start"]);
  });
});
