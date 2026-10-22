import { describe, expect, it, vi } from "vitest";
import { createPlayingGate, createStopwatch, needsResync } from "../src/talk/speechGate";

describe("createPlayingGate", () => {
  it("goes speaking at once and rests only after a real silence", () => {
    vi.useFakeTimers();
    const seen: boolean[] = [];
    const g = createPlayingGate((v) => seen.push(v), 200);
    g.playing(true);
    g.playing(false);
    vi.advanceTimersByTime(100);
    g.playing(true); // the next prefetched sentence starts: no flash of the idle face
    vi.advanceTimersByTime(500);
    expect(seen).toEqual([true]);
    g.playing(false);
    vi.advanceTimersByTime(200);
    expect(seen).toEqual([true, false]);
    vi.useRealTimers();
  });

  it("reset drops speaking immediately", () => {
    const seen: boolean[] = [];
    const g = createPlayingGate((v) => seen.push(v));
    g.playing(true);
    g.reset();
    expect(seen).toEqual([true, false]);
  });
});

describe("drift", () => {
  it("resyncs only beyond 80 ms either way", () => {
    expect(needsResync(1.05, 1)).toBe(false);
    expect(needsResync(1.09, 1)).toBe(true);
    expect(needsResync(0.9, 1)).toBe(true);
  });
});

describe("stopwatch", () => {
  it("laps each key once", () => {
    let t = 100;
    const w = createStopwatch(() => t);
    w.start("a");
    t = 350;
    expect(w.lap("a")).toBe(250);
    expect(w.lap("a")).toBeNull();
    expect(w.lap("b")).toBeNull();
  });
});
