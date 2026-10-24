import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FaceSync, startLeadFor } from "../src/talk/faceSync";
import { FakeStreamVideo, FakeVoice } from "./fakeMedia";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const now = () => Date.now();

function setup({ voiceS = 8, clipS = 8.5, firstMs = 0, rtf = 1 } = {}) {
  const shown: boolean[] = [];
  const sync = new FaceSync((s) => shown.push(s), { now });
  const video = new FakeStreamVideo(now);
  const voice = new FakeVoice(now, voiceS);
  sync.setVideo(video);
  video.startStream(clipS, firstMs, rtf);
  const isShown = () => shown.length > 0 && shown[shown.length - 1];
  const drive = async (ms: number, each?: () => void) => {
    for (let i = 0; i < ms / 10; i++) {
      await vi.advanceTimersByTimeAsync(10);
      video.advance(10);
      voice.advance(10);
      each?.();
    }
  };
  return { sync, video, voice, shown, isShown, drive };
}

describe("startLeadFor", () => {
  it("buffers a fragment of headroom plus ~9% of the sentence, never more than the sentence", () => {
    expect(startLeadFor(8)).toBeCloseTo(1.42);
    expect(startLeadFor(0.6)).toBe(0.6);
    expect(startLeadFor(NaN)).toBeCloseTo(0.7 + 0.09 * 6);
  });
});

describe("FaceSync", () => {
  it("start gate: resolves once the clip is buffered enough, then starts with the voice from 0 and shows on playing", async () => {
    const { sync, video, voice, shown, isShown, drive } = setup({ firstMs: 500 });
    let ready = false;
    void sync.join(voice, { url: "blob:x", release: vi.fn() }).then(() => { ready = true; });
    expect(video.src).toBe("blob:x");
    await drive(600); // first fragment (0.79 s) in: not enough for an 8 s sentence
    expect(ready).toBe(false);
    await drive(800); // second fragment (1.54 s) in
    expect(ready).toBe(true);
    expect(shown).toEqual([]); // nothing is shown before the voice
    expect(video.paused).toBe(true);
    void voice.play();
    await drive(100);
    expect(isShown()).toBe(true);
    expect(video.seeks).toEqual([]); // no seek: both started at 0
    await drive(2000);
    expect(Math.abs(video.currentTime - voice.currentTime)).toBeLessThan(0.05);
  });

  it("with no face element the gate opens at once and the clip is released", async () => {
    const sync = new FaceSync(() => {});
    expect(sync.canShow).toBe(false);
    const release = vi.fn();
    await sync.join(new FakeVoice(now, 1), { url: "u", release });
    expect(release).toHaveBeenCalled();
  });

  it("nudges the rate for medium drift and never seeks a visible clip", async () => {
    const { sync, video, voice, isShown, drive } = setup();
    void sync.join(voice, { url: "u", release: vi.fn() });
    await drive(200);
    void voice.play();
    await drive(1000);
    expect(isShown()).toBe(true);
    voice.currentTime += 0.15; // the voice is 150 ms ahead
    const rates: number[] = [];
    await drive(600, () => rates.push(video.playbackRate));
    expect(Math.max(...rates)).toBeGreaterThan(1);
    expect(Math.max(...rates)).toBeLessThanOrEqual(1.08);
    expect(video.seeks).toEqual([]);
    expect(isShown()).toBe(true);
  });

  it("a big drift hides the face and rejoins by rendezvous: seeked ahead while hidden, shown in step", async () => {
    const { sync, video, voice, shown, isShown, drive } = setup({ firstMs: -10000 }); // clip already fully rendered
    void sync.join(voice, { url: "u", release: vi.fn() });
    await drive(200);
    void voice.play();
    await drive(1000);
    voice.currentTime += 1; // e.g. a hiccup: the face is a second behind
    await drive(100);
    expect(isShown()).toBe(false);
    let worst = 0;
    await drive(1500, () => { if (isShown()) worst = Math.max(worst, Math.abs(video.currentTime - voice.currentTime)); });
    expect(shown).toEqual([true, false, true]);
    expect(worst).toBeLessThan(0.08);
    expect(video.seeks.length).toBe(1);
    expect(video.seeks[0].asked).toBeGreaterThan(voice.currentTime - 1.5); // to where the voice was going, not back
    expect(voice.pauses).toBe(0);
  });

  it("a stall hides the face and never pauses or moves the voice; it rejoins only once rendered ahead", async () => {
    // render slower than real time: the clip runs out mid-sentence
    const { sync, video, voice, shown, isShown, drive } = setup({ voiceS: 12, clipS: 12.8, rtf: 1.25 });
    void sync.join(voice, { url: "u", release: vi.fn() });
    await drive(1600);
    void voice.play();
    let worst = 0;
    await drive(11000, () => { if (isShown()) worst = Math.max(worst, Math.abs(video.currentTime - voice.currentTime)); });
    expect(shown[0]).toBe(true);
    expect(shown).toContain(false);
    expect(voice.pauses).toBe(0);
    expect(voice.backJumps).toEqual([]);
    expect(worst).toBeLessThan(0.08);
    expect(video.seeks.every((s) => s.landed === s.asked)).toBe(true);
  });

  it("a late join never seeks past what has been rendered (the browser would clamp it)", async () => {
    const { sync, video, voice, isShown, drive } = setup({ firstMs: 0, rtf: 1 });
    void voice.play();
    await drive(1300); // the voice is 1.3 s in, only the first 0.79 s fragment exists
    void sync.join(voice, { url: "u", release: vi.fn() }, true);
    await drive(4000);
    expect(video.seeks.every((s) => s.landed === s.asked)).toBe(true);
    expect(isShown()).toBe(false); // real-time render never gets 1 s ahead of this voice: talk loop
    expect(voice.pauses).toBe(0);
    expect(voice.backJumps).toEqual([]);
  });

  it("freezes with a paused voice and resumes with it", async () => {
    const { sync, video, voice, isShown, drive } = setup();
    void sync.join(voice, { url: "u", release: vi.fn() });
    await drive(200);
    void voice.play();
    await drive(1000);
    voice.pause();
    await drive(500);
    expect(video.paused).toBe(true);
    expect(isShown()).toBe(true);
    void voice.play();
    await drive(1000);
    expect(video.paused).toBe(false);
    expect(Math.abs(video.currentTime - voice.currentTime)).toBeLessThan(0.05);
  });

  it("leave hides and empties the video, releases a waiting gate, and never touches the voice", async () => {
    const { sync, video, voice, shown, drive } = setup({ firstMs: 5000 });
    let ready = false;
    void sync.join(voice, { url: "u", release: vi.fn() }).then(() => { ready = true; });
    await drive(100);
    sync.leave();
    await drive(10);
    expect(ready).toBe(true);
    expect(video.src).toBe("");
    expect(shown).toEqual([]);
    expect(voice.pauses).toBe(0);
  });
});
