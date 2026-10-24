/**
 * Issue #104: short sentences back to back. Each sentence's voice must start with its own lip-synced face from 0
 * (bounded wait still applies). Runs the real sentence speaker + FaceSync, driven like TalkController's pump
 * (play the sentence, then prefetch the next few with ranks), against Chrome-like fake media on a fake clock.
 * Talkback already leaves the sync when a sentence ends and waits up to START_WAIT_MS for every sentence; these guard
 * that (the read-mode cause is covered in readSync.test.ts).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FaceSync } from "../src/talk/faceSync";
import { createSentenceSpeaker } from "../src/talk/sentencePlayer";
import { FakeStreamVideo, FakeVoice } from "./fakeMedia";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const now = () => Date.now();

type Sample = { sentence: number; sounding: boolean; shown: boolean; drift: number; voiceT: number };

async function answer(voiceS: number[], { ttsMs = 300, firstMs = 1300, rtf = 1, depth = 3 } = {}) {
  const t0 = now();
  const video = new FakeStreamVideo(now);
  let shown = false;
  const sync = new FaceSync((s) => { shown = s; }, { now });
  sync.setVideo(video);
  const voices: FakeVoice[] = [];
  const texts = voiceS.map((_, i) => `Sentence ${i}.`);
  const opens: { i: number; at: number }[] = [];
  const speaker = createSentenceSpeaker(
    (text) => new Promise((res) => setTimeout(() => {
      const i = texts.indexOf(text);
      res({
        audio_url: `/tts/${i}.mp3`,
        openVideo: () => {
          opens.push({ i, at: now() - t0 });
          const url = `blob:${i}`;
          video.addStream(url, voiceS[i] + 0.6, firstMs, rtf);
          return new Promise((r) => setTimeout(() => r({ url, release() {} }), firstMs));
        },
      });
    }, ttsMs)),
    (url) => { const v = new FakeVoice(now, voiceS[Number(url.match(/(\d+)\.mp3/)![1])]); voices.push(v); return v; },
    { sync, now },
  );
  let current = -1;
  const samples: Sample[] = [];
  let done = false;
  void (async () => {
    for (let i = 0; i < texts.length; i++) {
      current = i;
      const p = speaker.play(texts[i]);
      texts.slice(i + 1, i + 1 + depth).forEach((t, r) => speaker.prefetch(t, r));
      await p;
    }
    done = true;
  })();
  for (let k = 0; k < 6000 && !done; k++) {
    await vi.advanceTimersByTimeAsync(10);
    video.advance(10);
    for (const v of voices) v.advance(10);
    const v = voices.find((x) => x.sounding);
    samples.push({ sentence: current, sounding: !!v, shown, drift: v ? Math.abs(video.currentTime - v.currentTime) : 0, voiceT: v?.currentTime ?? 0 });
  }
  expect(done).toBe(true);
  const per = texts.map((_, i) => {
    const s = samples.filter((x) => x.sentence === i && x.sounding);
    const firstShown = s.findIndex((x) => x.shown);
    return { soundMs: s.length * 10, shownMs: s.filter((x) => x.shown).length * 10, firstShownMs: firstShown < 0 ? null : firstShown * 10,
      maxDrift: Math.max(0, ...s.filter((x) => x.shown).map((x) => x.drift)) };
  });
  return { per, opens, video };
}

describe("short sentences back to back (#104)", () => {
  it("each short sentence starts with its own face from 0", async () => {
    const r = await answer([4, 0.9, 0.8, 1.1, 3]);
    for (const p of r.per) {
      expect(p.firstShownMs).not.toBeNull();
      expect(p.firstShownMs!).toBeLessThanOrEqual(100);
      expect(p.maxDrift).toBeLessThan(0.1);
    }
  });

  it("a very short first sentence followed by short ones, with slow render starts", async () => {
    const r = await answer([0.7, 0.6, 0.9, 0.7], { firstMs: 3500 });
    for (const p of r.per) {
      expect(p.firstShownMs).not.toBeNull();
      expect(p.firstShownMs!).toBeLessThanOrEqual(100);
    }
  });
});
