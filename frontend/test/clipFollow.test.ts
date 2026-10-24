import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FakeFileVideo } from "./fakeMedia";
import { audioSounding, createClipFollower, type FollowAudio, type FollowVideo } from "../src/reader/clipFollow";

class V implements FollowVideo {
  currentTime = 0; playbackRate = 1; paused = true; plays = 0;
  play() { this.paused = false; this.plays++; return Promise.resolve(); }
  pause() { this.paused = true; }
}
/** Fake clock: `adv` moves time in 10 ms steps (voice and clip advance) and polls the follower every 100 ms. */
const mk = () => {
  const v = new V();
  const a: FollowAudio = { currentTime: 0, paused: true, ended: false };
  const f = createClipFollower(v, () => Date.now());
  let n = 0;
  const adv = async (ms: number) => {
    for (let i = 0; i < ms / 10; i++) {
      await vi.advanceTimersByTimeAsync(10);
      if (!a.paused) a.currentTime += 0.01;
      if (!v.paused) v.currentTime += 0.01 * v.playbackRate;
      if (++n % 10 === 0) f.tick(a);
    }
  };
  return { v, a, f, adv };
};

describe("clip follower", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("audioSounding needs a playing, non-ended element past the start", () => {
    expect(audioSounding(null)).toBe(false);
    expect(audioSounding({ currentTime: 0, paused: false })).toBe(false);
    expect(audioSounding({ currentTime: 0.2, paused: false })).toBe(true);
    expect(audioSounding({ currentTime: 0.2, paused: true })).toBe(false);
    expect(audioSounding({ currentTime: 0.2, paused: false, ended: true })).toBe(false);
  });
  it("does not start the clip before the voice, then starts it in step with the voice", async () => {
    const { v, a, adv } = mk();
    await adv(300);
    expect(v.plays).toBe(0);
    a.paused = false; a.currentTime = 0.01;
    await adv(500);
    expect(v.plays).toBe(1);
    expect(Math.abs(v.currentTime - a.currentTime)).toBeLessThan(0.03);
  });
  it("pauses with the voice and resumes in step with it", async () => {
    const { v, a, adv } = mk();
    a.paused = false; a.currentTime = 0.01;
    await adv(1000);
    a.paused = true;
    await adv(200);
    expect(v.paused).toBe(true);
    a.paused = false;
    await adv(500);
    expect(v.paused).toBe(false);
    expect(Math.abs(v.currentTime - a.currentTime)).toBeLessThan(0.03);
  });
  it("stops when the chunk ends", async () => {
    const { v, a, adv } = mk();
    a.paused = false; a.currentTime = 0.01;
    await adv(1000);
    a.ended = true;
    await adv(100);
    expect(v.paused).toBe(true);
  });
  it("nudges the rate for moderate drift without seeking", async () => {
    const { v, a, adv } = mk();
    a.paused = false; a.currentTime = 0.01;
    await adv(1000);
    v.currentTime += 0.15; // clip jumped ahead 150 ms
    const before = v.currentTime;
    await adv(300);
    expect(v.playbackRate).toBeLessThan(1);
    expect(v.currentTime).toBeGreaterThan(before); // never seeked back
  });
  it("a big drift lines the clip up again: in step afterwards", async () => {
    const { v, a, adv } = mk();
    a.paused = false; a.currentTime = 0.01;
    await adv(1000);
    v.currentTime += 1.5;
    await adv(1000);
    expect(Math.abs(v.currentTime - a.currentTime)).toBeLessThan(0.03);
    expect(v.paused).toBe(false);
  });
  it("a clip that ends before its audio stays on its last frame: no restart, no seek loop", async () => {
    const v = new V() as V & { duration: number; ended: boolean };
    v.duration = 1; v.ended = false;
    const a: FollowAudio = { currentTime: 0.01, paused: false, ended: false };
    const f = createClipFollower(v, () => Date.now());
    const seen: number[] = [];
    for (let i = 0; i < 300; i++) { // 3 s of audio against a 1 s clip
      await vi.advanceTimersByTimeAsync(10);
      a.currentTime += 0.01;
      if (!v.paused) v.currentTime += 0.01 * v.playbackRate;
      if (v.currentTime >= v.duration) { v.currentTime = v.duration; v.ended = true; v.paused = true; }
      if (i % 10 === 9) { f.tick(a); seen.push(v.currentTime); }
    }
    expect(v.plays).toBe(1);
    expect(seen.slice(-15).every((x) => x === 1)).toBe(true);
  });
});

describe("clip follower start-up (#66)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());
  const now = () => Date.now();

  /** The chunk's voice starts at t=0 while its clip element is still loading (loadMs). Returns the worst offset seen
   * while the clip was actually moving, and how long it moved. */
  async function run(loadMs: number, seekMs = 60) {
    const v = new FakeFileVideo(now, 6, loadMs);
    v.seekMs = seekMs;
    const a: FollowAudio = { currentTime: 0, paused: false, ended: false };
    const f = createClipFollower(v, now);
    let worst = 0, movingMs = 0, last = v.currentTime;
    for (let i = 0; i < 400; i++) { // 4 s
      await vi.advanceTimersByTimeAsync(10);
      a.currentTime += 0.01;
      v.advance(10);
      if (i % 10 === 9) f.tick(a); // the 100 ms poll
      const moving = v.currentTime !== last && !v.paused && !v.seeking && v.readyState >= 3; // playing, not a seek jump
      last = v.currentTime;
      if (moving && i > 0) { movingMs += 10; worst = Math.max(worst, Math.abs(v.currentTime - a.currentTime)); }
    }
    return { worst, movingMs };
  }

  it("a clip that is still loading when the voice starts never plays out of step", async () => {
    const r = await run(400);
    expect(r.movingMs).toBeGreaterThan(3000);
    expect(r.worst).toBeLessThan(0.08);
  });

  it("slow seeks are absorbed by a bigger lead, still in step", async () => {
    const r = await run(150, 400);
    expect(r.movingMs).toBeGreaterThan(2000);
    expect(r.worst).toBeLessThan(0.08);
  });
});
