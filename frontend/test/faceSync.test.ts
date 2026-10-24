import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { FaceSync, type VideoLike } from "../src/talk/faceSync";
import type { AudioLike } from "../src/talk/sentencePlayer";

class FakeVideo extends EventTarget implements VideoLike {
  src = ""; currentTime = 0; readyState = 4; plays = 0; paused = false;
  play() { this.plays++; return Promise.resolve(); }
  pause() { this.paused = true; }
  load() {}
  removeAttribute(n: string) { if (n === "src") this.src = ""; }
  fire(t: string) { this.dispatchEvent(new Event(t)); }
}
class FakeAudio implements AudioLike {
  onended = null; onerror = null; currentTime = 0; pauses = 0; resumes = 0;
  play() { this.resumes++; return Promise.resolve(); }
  pause() { this.pauses++; }
}

class EventedAudio extends FakeAudio {
  private t = new EventTarget();
  addEventListener(type: string, fn: () => void) { this.t.addEventListener(type, fn); }
  removeEventListener(type: string, fn: () => void) { this.t.removeEventListener(type, fn); }
  fire(type: string) { this.t.dispatchEvent(new Event(type)); }
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function setup() {
  const shown: boolean[] = [];
  const sync = new FaceSync((s) => shown.push(s), { stallMs: 1000, checkMs: 100, seekCooldownMs: 0 });
  const v = new FakeVideo();
  const a = new FakeAudio();
  sync.setVideo(v);
  return { sync, v, a, shown };
}

describe("FaceSync", () => {
  it("shows the video only after it is playing, and seeks it to the voice when joining late", () => {
    const { sync, v, a, shown } = setup();
    a.currentTime = 2.5; // the clip arrives mid-sentence
    sync.join(a, { url: "blob:x", release: vi.fn() });
    expect(v.src).toBe("blob:x");
    expect(shown).toEqual([]); // keep the talk loop visible: no black flash
    v.fire("loadeddata");
    expect(v.currentTime).toBe(2.5);
    expect(v.plays).toBe(1);
    expect(shown).toEqual([]);
    v.fire("playing");
    expect(shown).toEqual([true]);
  });

  it("leaves small drift alone, nudges the rate for medium drift, seeks only for big drift", () => {
    const { sync, v, a } = setup();
    v.playbackRate = 1;
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    a.currentTime = 1; v.currentTime = 1.03; // dead-band
    vi.advanceTimersByTime(100);
    expect(v.currentTime).toBe(1.03);
    expect(v.playbackRate).toBe(1);
    // medium drift: video 150 ms ahead of the (wall-clock advancing) voice -> slower, no seek
    const t0 = a.currentTime;
    a.currentTime = t0 + 0.1; v.currentTime = a.currentTime + 0.15;
    const before = v.currentTime;
    vi.advanceTimersByTime(100);
    expect(v.currentTime).toBe(before);
    expect(v.playbackRate).toBeLessThan(1);
    expect(v.playbackRate).toBeGreaterThanOrEqual(0.92);
    // big drift: seek
    a.currentTime = 6; v.currentTime = 4;
    vi.advanceTimersByTime(1500);
    expect(Math.abs(v.currentTime - 6)).toBeLessThan(1.6);
    expect(v.currentTime).toBeGreaterThan(4);
  });

  it("does not start the video before the voice is playing", () => {
    const sync = new FaceSync(() => {}, { checkMs: 100 });
    const v = new FakeVideo(); sync.setVideo(v);
    const a = new EventedAudio();
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata");
    expect(v.plays).toBe(0); // voice has not started: no mouthing yet
    a.fire("playing");
    expect(v.plays).toBe(1);
  });

  it("pauses the voice while the video waits and resumes both together", () => {
    const { sync, v, a } = setup();
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    a.currentTime = 3; v.currentTime = 3;
    v.fire("waiting");
    expect(a.pauses).toBe(1);
    expect(sync.holding).toBe(true);
    v.currentTime = 3.2;
    v.fire("playing");
    expect(sync.holding).toBe(false);
    expect(a.currentTime).toBe(3.2); // voice realigned to the video
    expect(a.resumes).toBe(1);
  });

  it("falls back to the talk loop and frees the voice when the video stalls too long, then rejoins", () => {
    const { sync, v, a, shown } = setup();
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    a.currentTime = 1; v.currentTime = 1;
    v.fire("waiting");
    vi.advanceTimersByTime(1000);
    expect(shown).toEqual([true, false]);
    expect(sync.holding).toBe(false);
    expect(a.resumes).toBe(1);
    a.currentTime = 5; // the voice carried on
    vi.advanceTimersByTime(2000); // back-off, then (no buffer info) the face tries again: seek, show on playing
    expect(Math.abs(v.currentTime - 5)).toBeLessThan(0.5);
    expect(shown).toEqual([true, false]);
    v.fire("playing");
    expect(shown).toEqual([true, false, true]);
  });

  it("after giving up it never pauses the voice again, and rejoins only once buffered ahead", () => {
    const { sync, v, a, shown } = setup();
    let bufferedEnd = 0;
    (v as FakeVideo & { buffered: unknown }).buffered = { get length() { return 1; }, end: () => bufferedEnd };
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    a.currentTime = 1; v.currentTime = 1; bufferedEnd = 1;
    v.fire("waiting");
    vi.advanceTimersByTime(1000); // gave up
    expect(a.pauses).toBe(1);
    a.currentTime = 4; bufferedEnd = 4.3; // rendering is slower than real time
    v.fire("playing"); // the hidden video plays a bit
    vi.advanceTimersByTime(3000);
    expect(shown).toEqual([true, false]); // not enough buffered ahead of the voice: stay on the talk loop
    v.fire("waiting");
    expect(a.pauses).toBe(1); // the voice is never held again
    expect(sync.holding).toBe(false);
    a.currentTime = 6; bufferedEnd = 9;
    for (let i = 0; i < 80 && Math.abs(v.currentTime - a.currentTime) > 1; i++) vi.advanceTimersByTime(100);
    expect(Math.abs(v.currentTime - a.currentTime)).toBeLessThan(1); // rejoined: seeked to the voice
    v.fire("playing");
    expect(shown).toEqual([true, false, true]);
    v.fire("waiting"); // stalls again while shown: hide, but still no hold
    expect(a.pauses).toBe(1);
    expect(shown).toEqual([true, false, true, false]);
  });

  it("a rejoin that never gets going falls back again instead of waiting forever", () => {
    const { sync, v, a, shown } = setup();
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    a.currentTime = 1; v.currentTime = 1;
    v.fire("waiting");
    vi.advanceTimersByTime(1000);
    a.currentTime = 5;
    vi.advanceTimersByTime(2000); // rejoin attempt (seek to 5), no 'playing' follows
    const plays = v.plays;
    vi.advanceTimersByTime(1000 + 4000 + 100); // gives up on it, waits the doubled back-off, tries again
    expect(v.plays).toBeGreaterThan(plays);
    expect(a.pauses).toBe(1);
    expect(shown).toEqual([true, false]);
  });

  it("leave hides the video, empties it and never touches the voice", () => {
    const { sync, v, a, shown } = setup();
    sync.join(a, { url: "u", release: vi.fn() });
    v.fire("loadeddata"); v.fire("playing");
    sync.leave();
    expect(shown).toEqual([true, false]);
    expect(v.src).toBe("");
    v.fire("waiting"); // stale event after leaving
    expect(a.pauses).toBe(0);
  });

  it("releases the clip when no video element is mounted", () => {
    const sync = new FaceSync(() => {});
    const release = vi.fn();
    sync.join(new FakeAudio(), { url: "u", release });
    expect(release).toHaveBeenCalled();
  });
});
