/**
 * Issue #66: "the voice starts while the face is out of sync, then the voice stutters and rewinds ~0.5 s, then they
 * are in sync". These run the real sentence speaker + FaceSync against Chrome-like fake media on a fake clock.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FaceSync } from "../src/talk/faceSync";
import { createSentenceSpeaker } from "../src/talk/sentencePlayer";
import { FakeStreamVideo, FakeVoice } from "./fakeMedia";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const now = () => Date.now();

type Run = {
  voice: FakeVoice; video: FakeStreamVideo;
  shownDrift: number[]; // |video - voice| on every step the face is shown
  shownMs: number; soundMs: number;
  startMs: number; // when the first word was heard, from TTS ready
};

/** One answer sentence: TTS is ready at t=0, its lip-sync stream opens then; the first fragment arrives at firstMs. */
async function speakOne({ voiceS = 8.4, clipS = 9.04, firstMs = 1300, rtf = 1, prefetchedMs = 0, startWaitMs }:
  { voiceS?: number; clipS?: number; firstMs?: number; rtf?: number; prefetchedMs?: number; startWaitMs?: number }): Promise<Run> {
  const t0 = now();
  const voice = new FakeVoice(now, voiceS);
  const video = new FakeStreamVideo(now);
  let shown = false;
  const sync = new FaceSync((s) => { shown = s; }, { now });
  sync.setVideo(video);
  // a prefetched (rank 0) stream started rendering prefetchedMs before this sentence plays
  video.startStream(clipS, firstMs - prefetchedMs, rtf);
  const openVideo = () => new Promise<{ url: string; release: () => void }>((res) =>
    setTimeout(() => res({ url: "blob:clip", release() {} }), Math.max(0, firstMs - prefetchedMs)));
  const speaker = createSentenceSpeaker(async () => ({ audio_url: "/a.mp3", openVideo }), () => voice,
    { sync, now, ...(startWaitMs !== undefined ? { startWaitMs } : {}) });
  const run: Run = { voice, video, shownDrift: [], shownMs: 0, soundMs: 0, startMs: NaN };
  const done = speaker.play("A long answer sentence.");
  let finished = false;
  void done.then(() => { finished = true; });
  for (let i = 0; i < 2500 && !finished; i++) {
    await vi.advanceTimersByTimeAsync(10);
    video.advance(10);
    voice.advance(10);
    if (voice.sounding) run.soundMs += 10;
    if (shown) { run.shownMs += 10; run.shownDrift.push(Math.abs(video.currentTime - voice.currentTime)); }
  }
  expect(finished).toBe(true);
  run.startMs = (voice.startedAt ?? NaN) - t0;
  if (process.env.SYNC_DEBUG) console.log(JSON.stringify({ firstMs, rtf, prefetchedMs, startMs: run.startMs, shownMs: run.shownMs, soundMs: run.soundMs, maxDrift: Math.max(0, ...run.shownDrift).toFixed(3), seeks: video.seeks.length }));
  return run;
}

describe("Talkback face start-up (#66)", () => {
  it("first sentence: the voice is never paused or rewound to meet the video", async () => {
    // stream opens with the voice; first 0.79 s fragment after 1.3 s, then real time (Boson's measured pace)
    const r = await speakOne({ firstMs: 1300 });
    expect(r.voice.backJumps).toEqual([]); // was [~0.5]: seek clamped to the buffered end, voice moved back to it
    expect(r.voice.pauses).toBe(0);        // was 1: the clamped seek's 'waiting' paused the voice
  });

  it("first sentence: voice and lip-synced face start together and stay in step", async () => {
    const r = await speakOne({ firstMs: 1300 });
    expect(r.shownMs).toBeGreaterThan(0.9 * r.soundMs); // the lip-synced face is up from the first word
    expect(Math.max(...r.shownDrift)).toBeLessThan(0.1);
    expect(r.video.seeks.every((s) => s.landed === s.asked)).toBe(true); // never asked for unrendered video
    expect(r.startMs).toBeLessThan(1300 + 750 * 2 + 300); // bounded pre-roll: two fragments
  });

  it("a prefetched clip that is already well buffered does not delay the voice", async () => {
    const r = await speakOne({ firstMs: 1300, prefetchedMs: 6000 });
    expect(r.startMs).toBeLessThan(200);
    expect(r.shownMs).toBeGreaterThan(0.9 * r.soundMs);
    expect(Math.max(...r.shownDrift)).toBeLessThan(0.1);
  });

  it("a render slower than real time drops the face to the talk loop instead of stuttering the voice", async () => {
    const r = await speakOne({ voiceS: 12, clipS: 12.8, firstMs: 1300, rtf: 1.3 });
    expect(r.voice.pauses).toBe(0);
    expect(r.voice.backJumps).toEqual([]);
    expect(r.shownMs).toBeGreaterThan(0);
    expect(Math.max(...r.shownDrift)).toBeLessThan(0.1); // whenever the clip is visible it is in step
  });

  it("a stream that is too slow to start: the voice starts after the cap and is left alone (talk loop)", async () => {
    const r = await speakOne({ firstMs: 3000, startWaitMs: 1000 });
    expect(r.startMs).toBeLessThan(1200);
    expect(r.voice.pauses).toBe(0);
    expect(r.voice.backJumps).toEqual([]);
    expect(r.video.seeks).toEqual([]); // at real-time render it never gets far enough ahead to join
    expect(r.shownMs).toBe(0);
  });

  it("a late clip that renders faster than real time joins by rendezvous: in step, never seeking past the render", async () => {
    const r = await speakOne({ firstMs: 3000, startWaitMs: 1000, rtf: 0.5 });
    expect(r.voice.pauses).toBe(0);
    expect(r.voice.backJumps).toEqual([]);
    expect(r.video.seeks.length).toBeGreaterThan(0);
    expect(r.video.seeks.every((s) => s.landed === s.asked)).toBe(true);
    expect(r.shownMs).toBeGreaterThan(2000);
    expect(Math.max(...r.shownDrift)).toBeLessThan(0.1);
  });
});
