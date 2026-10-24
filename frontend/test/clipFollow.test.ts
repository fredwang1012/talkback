import { describe, expect, it } from "vitest";
import { audioSounding, createClipFollower, type FollowAudio, type FollowVideo } from "../src/reader/clipFollow";

class V implements FollowVideo {
  currentTime = 0; playbackRate = 1; paused = true; plays = 0;
  play() { this.paused = false; this.plays++; return Promise.resolve(); }
  pause() { this.paused = true; }
}
const mk = () => {
  let t = 0;
  const v = new V();
  const a: FollowAudio = { currentTime: 0, paused: true, ended: false };
  const f = createClipFollower(v, () => t);
  return { v, a, f, adv: (ms: number) => { t += ms; if (!a.paused) a.currentTime += ms / 1000; v.currentTime += v.paused ? 0 : (ms / 1000) * v.playbackRate; } };
};

describe("clip follower", () => {
  it("audioSounding needs a playing, non-ended element past the start", () => {
    expect(audioSounding(null)).toBe(false);
    expect(audioSounding({ currentTime: 0, paused: false })).toBe(false);
    expect(audioSounding({ currentTime: 0.2, paused: false })).toBe(true);
    expect(audioSounding({ currentTime: 0.2, paused: true })).toBe(false);
    expect(audioSounding({ currentTime: 0.2, paused: false, ended: true })).toBe(false);
  });
  it("does not start the clip before the voice, then starts it with the voice", () => {
    const { v, a, f, adv } = mk();
    f.tick(a); adv(100); f.tick(a);
    expect(v.plays).toBe(0);
    a.paused = false; a.currentTime = 0.05;
    f.tick(a);
    expect(v.plays).toBe(1);
    expect(v.currentTime).toBeCloseTo(0.05);
  });
  it("pauses with the voice and resumes aligned to it", () => {
    const { v, a, f, adv } = mk();
    a.paused = false; a.currentTime = 0.1; f.tick(a);
    adv(500); f.tick(a);
    a.paused = true; f.tick(a);
    expect(v.paused).toBe(true);
    a.paused = false; a.currentTime = 0.6; v.currentTime = 0.6; f.tick(a);
    expect(v.paused).toBe(false);
  });
  it("stops when the chunk ends", () => {
    const { v, a, f } = mk();
    a.paused = false; a.currentTime = 0.1; f.tick(a);
    a.ended = true; f.tick(a);
    expect(v.paused).toBe(true);
  });
  it("nudges the rate for moderate drift without seeking", () => {
    const { v, a, f, adv } = mk();
    a.paused = false; a.currentTime = 0.1; f.tick(a);
    v.currentTime += 0.15; // clip jumped ahead 150 ms
    for (let i = 0; i < 3; i++) { adv(100); f.tick(a); }
    expect(v.playbackRate).toBeLessThan(1);
    expect(v.currentTime).toBeGreaterThan(0.3); // never seeked back
  });
  it("seeks on a big drift", () => {
    const { v, a, f, adv } = mk();
    a.paused = false; a.currentTime = 0.1; f.tick(a);
    v.currentTime += 1.5;
    for (let i = 0; i < 5; i++) { adv(100); f.tick(a); }
    expect(Math.abs(v.currentTime - a.currentTime)).toBeLessThan(0.3);
  });
  it("a clip that ends before its audio stays on its last frame: no restart, no seek loop", () => {
    let t = 0;
    const v = new V() as V & { duration: number; ended: boolean };
    v.duration = 1; v.ended = false;
    const a: FollowAudio = { currentTime: 0, paused: false, ended: false };
    const f = createClipFollower(v, () => t);
    a.currentTime = 0.05; f.tick(a);
    const seen: number[] = [];
    for (let i = 0; i < 30; i++) { // 3 s of audio against a 1 s clip
      t += 100; a.currentTime += 0.1;
      if (!v.paused) v.currentTime += 0.1 * v.playbackRate;
      if (v.currentTime >= v.duration) { v.currentTime = v.duration; v.ended = true; v.paused = true; }
      f.tick(a);
      seen.push(v.currentTime);
    }
    expect(v.plays).toBe(1);
    expect(seen.slice(-15).every((x) => x === 1)).toBe(true);
  });
});
