import { describe, expect, it } from "vitest";
import { DEFAULT_SPEECH, SPEECH_IDLE, pulseAmount, rms, stepSpeech, type SpeechState } from "../src/reader/speechLevel";

const run = (levels: number[], dt = 16, from: SpeechState = SPEECH_IDLE) =>
  levels.reduce((s, l) => stepSpeech(s, l, dt), from);
const rep = (v: number, n: number) => Array<number>(n).fill(v);

describe("stepSpeech", () => {
  it("stays closed on silence and brief blips", () => {
    expect(run(rep(0, 50)).speaking).toBe(false);
    expect(run([0.1, 0.1, 0.1, 0, 0, 0]).speaking).toBe(false); // 48 ms < 60 ms
  });
  it("opens after about 60 ms above the threshold", () => {
    expect(run(rep(0.1, 3)).speaking).toBe(false);
    expect(run(rep(0.1, 4)).speaking).toBe(true);
  });
  it("stays open through short dips and closes after 150 ms of quiet", () => {
    const open = run(rep(0.1, 10));
    expect(run(rep(0.001, 5), 16, open).speaking).toBe(true); // 80 ms
    expect(run(rep(0.001, 9), 16, open).speaking).toBe(true);
    expect(run(rep(0.001, 10), 16, open).speaking).toBe(false);
  });
  it("a loud frame resets the closing timer", () => {
    const open = run(rep(0.1, 10));
    const s = run([...rep(0.001, 8), 0.1, ...rep(0.001, 8)], 16, open);
    expect(s.speaking).toBe(true);
  });
  it("levels between the thresholds hold the state", () => {
    const mid = (DEFAULT_SPEECH.openAbove + DEFAULT_SPEECH.closeBelow) / 2;
    expect(run(rep(mid, 40)).speaking).toBe(false);
    expect(run(rep(mid, 40), 16, run(rep(0.1, 10))).speaking).toBe(true);
  });
  it("caps a huge frame gap", () => {
    expect(stepSpeech(SPEECH_IDLE, 0.1, 10_000).speaking).toBe(true); // 250 ms cap still exceeds openMs
    const open = run(rep(0.1, 10));
    expect(stepSpeech(open, 0, 100).speaking).toBe(true);
  });
});

describe("rms / pulseAmount", () => {
  it("computes rms", () => {
    expect(rms([])).toBe(0);
    expect(rms([0.5, -0.5, 0.5, -0.5])).toBeCloseTo(0.5);
  });
  it("clamps the pulse", () => {
    expect(pulseAmount(0)).toBe(0);
    expect(pulseAmount(5)).toBe(1);
    expect(pulseAmount(0.06)).toBeCloseTo(0.5);
  });
});
