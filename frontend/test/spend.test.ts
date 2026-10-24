import { describe, expect, it } from "vitest";
import { RATES, SpendTracker } from "../src/spend";

describe("SpendTracker", () => {
  it("prices TTS characters at the published rate", () => {
    const s = new SpendTracker();
    s.addTts(45000); // a whole story
    expect(s.snapshot().usd).toBeCloseTo(0.675, 3);
  });

  it("prices one minute of audio in (with transcription) and out", () => {
    const s = new SpendTracker();
    const minuteB64 = (24000 * 2 * 60 * 4) / 3; // base64 length of one minute of PCM16 @ 24 kHz
    s.addAudioIn(minuteB64);
    s.addAudioOut(minuteB64);
    const snap = s.snapshot();
    expect(snap.inSeconds).toBeCloseTo(60, 3);
    expect(snap.usd).toBeCloseTo(RATES.audioInPerMin + RATES.transcriptionPerMin + RATES.audioOutPerMin, 6);
  });

  it("notifies subscribers and can reset", () => {
    const s = new SpendTracker();
    const seen: number[] = [];
    const off = s.subscribe((x) => seen.push(x.ttsChars));
    s.addTts(100);
    s.addTts(50);
    off();
    s.addTts(1);
    expect(seen).toEqual([100, 150]);
    s.reset();
    expect(s.snapshot().usd).toBe(0);
  });
});
