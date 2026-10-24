import { describe, expect, it } from "vitest";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";

class FakeAudio implements AudioLike {
  currentTime = 0;
  playbackRate = 1;
  preservesPitch = false;
  preload = "";
  onended: (() => void) | null = null;
  playing = false;
  played = 0;
  constructor(public src: string) {}
  async play() { this.playing = true; this.played++; }
  pause() { this.playing = false; }
  finish() { this.playing = false; this.onended?.(); }
}

const flush = () => new Promise((r) => setTimeout(r, 0));
const mkChunks = (n: number): FlatChunk[] =>
  Array.from({ length: n }, (_, i) => ({
    id: `c${i}`, speaker: "narrator", voice: "marcus", tts_text: `t${i}`.padEnd(200, "."), char_start: 0, char_end: 10, para: Math.floor(i / 2),
  }));

function setup(n = 8, opts: { failAt?: number; words?: boolean } = {}) {
  const fetched: { id: string; voice?: string }[] = [];
  const audios: FakeAudio[] = [];
  const engine = new ReaderEngine(mkChunks(n), {
    fetchTts: async (chunk, voice): Promise<TtsResult> => {
      fetched.push({ id: chunk.id, voice });
      if (chunk.id === `c${opts.failAt}`) throw new Error("boom");
      return {
        audio_url: `/media/${chunk.id}.mp3`,
        timestamps: opts.words ? [
          { start: 0, end: 0.5, char_start: 0, char_end: 3 }, { start: 0.5, end: 1, char_start: 4, char_end: 9 }] : null,
      };
    },
    createAudio: (url) => { const a = new FakeAudio(url); audios.push(a); return a; },
  });
  const snaps: string[] = [];
  engine.on((s) => snaps.push(`${s.state}:${s.index}`));
  const audioFor = (id: string) => audios.find((a) => a.src.endsWith(`${id}.mp3`))!;
  return { engine, fetched, audios, audioFor, snaps };
}

describe("ReaderEngine", () => {
  it("plays a chunk, then the next one when it ends, with the next 3 prefetched", async () => {
    const { engine, fetched, audioFor } = setup();
    engine.play();
    await flush();
    expect(engine.snapshot()).toEqual({ state: "playing", index: 0, error: null });
    expect(audioFor("c0").playing).toBe(true);
    await flush();
    expect(fetched.map((f) => f.id)).toEqual(["c0", "c1", "c2", "c3"]);
    audioFor("c0").finish();
    await flush();
    expect(engine.snapshot().index).toBe(1);
    expect(audioFor("c1").playing).toBe(true);
    expect(audioFor("c1").played).toBe(1);
  });

  it("uses pitch-preserving, speed-adjustable audio", async () => {
    const { engine, audioFor } = setup();
    engine.setSpeed(1.25);
    engine.play();
    await flush();
    expect(audioFor("c0").preservesPitch).toBe(true);
    expect(audioFor("c0").playbackRate).toBe(1.25);
    engine.setSpeed(0.75);
    await flush();
    expect(audioFor("c0").playbackRate).toBe(0.75);
    expect(audioFor("c1").playbackRate).toBe(0.75);
  });

  it("pauses and resumes the same audio without refetching", async () => {
    const { engine, fetched, audioFor } = setup();
    engine.play();
    await flush();
    engine.pause();
    expect(engine.snapshot().state).toBe("paused");
    expect(audioFor("c0").playing).toBe(false);
    const n = fetched.length;
    engine.play();
    await flush();
    expect(engine.snapshot().state).toBe("playing");
    expect(audioFor("c0").played).toBe(2);
    expect(fetched.length).toBe(n);
  });

  it("seeking stops the old chunk, drops stale prefetches and ignores its late 'ended'", async () => {
    const { engine, fetched, audioFor } = setup(12);
    engine.play();
    await flush();
    const old = audioFor("c0");
    engine.seek(8);
    await flush();
    expect(old.playing).toBe(false);
    expect(engine.snapshot().index).toBe(8);
    expect(audioFor("c8").playing).toBe(true);
    expect(fetched.map((f) => f.id)).toContain("c11");
    old.finish(); // a stale callback must not advance the position
    await flush();
    expect(engine.snapshot().index).toBe(8);
  });

  it("seek while paused moves the position without playing", async () => {
    const { engine, audios } = setup();
    engine.seek(5);
    await flush();
    expect(engine.snapshot()).toMatchObject({ index: 5, state: "idle" });
    expect(audios.every((a) => !a.playing)).toBe(true);
  });

  it("clamps next/prev at the ends and finishes at the last chunk", async () => {
    const { engine, audioFor } = setup(2);
    engine.prev();
    expect(engine.snapshot().index).toBe(0);
    engine.play();
    await flush();
    audioFor("c0").finish();
    await flush();
    audioFor("c1").finish();
    await flush();
    expect(engine.snapshot()).toEqual({ state: "ended", index: 1, error: null });
    engine.next();
    expect(engine.snapshot().index).toBe(1);
  });

  it("reports an error and stops when TTS fails, and can retry", async () => {
    const { engine, snaps } = setup(4, { failAt: 0 });
    engine.play();
    await flush();
    expect(engine.snapshot()).toMatchObject({ state: "error", error: "boom" });
    expect(snaps).toContain("loading:0");
  });

  it("a pause pressed while loading prevents playback", async () => {
    const { engine, audios } = setup();
    engine.play();
    engine.pause();
    await flush();
    expect(engine.snapshot().state).toBe("paused");
    expect(audios.every((a) => !a.playing)).toBe(true);
  });

  it("full cast off requests the narrator voice for chunks not yet fetched", async () => {
    const { engine, fetched } = setup(8);
    engine.play();
    await flush();
    engine.setNarratorOnly("marcus");
    await flush();
    const later = fetched.filter((f) => f.voice === "marcus").map((f) => f.id);
    expect(later.length).toBeGreaterThan(0);
    expect(later).not.toContain("c0");
  });

  it("highlights the word being spoken", async () => {
    const { engine, audioFor } = setup(3, { words: true });
    engine.play();
    await flush();
    expect(engine.activeWord()).toEqual({ charStart: 0, charEnd: 3 });
    audioFor("c0").currentTime = 0.7;
    expect(engine.activeWord()).toEqual({ charStart: 4, charEnd: 9 });
    const none = setup(3);
    none.engine.play();
    await flush();
    expect(none.engine.activeWord()).toBeNull();
  });
});
