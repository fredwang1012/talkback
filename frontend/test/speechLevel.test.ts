import { describe, expect, it } from "vitest";
import { DEFAULT_SPEECH, SPEECH_IDLE, pulseAmount, rms, stepSpeech, type SpeechState } from "../src/reader/speechLevel";

const run = (levels: number[], dt = 16, from: SpeechState = SPEECH_IDLE) =>
  levels.reduce((s, l) => stepSpeech(s, l, dt), from);
const rep = (v: number, n: number) => Array<number>(n).fill(v);

describe("stepSpeech", () => {
  it("stays closed on silence", () => {
    expect(run(rep(0, 50)).speaking).toBe(false);
  });
  it("attacks fast: opens on the first loud frame, a rest below the open threshold does not", () => {
    expect(run(rep(0.1, 1)).speaking).toBe(true); // one 16 ms frame
    expect(run(rep(0.015, 20)).speaking).toBe(false);
  });
  it("releases slower: stays open through a ~80 ms dip, closes after 100 ms of quiet", () => {
    const open = run(rep(0.1, 10));
    expect(run(rep(0.001, 5), 16, open).speaking).toBe(true); // 80 ms
    expect(run(rep(0.001, 7), 16, open).speaking).toBe(false); // 112 ms
  });
  it("a loud frame resets the closing timer", () => {
    const open = run(rep(0.1, 10));
    expect(run([...rep(0.001, 5), 0.1, ...rep(0.001, 5)], 16, open).speaking).toBe(true);
  });
  it("levels between the thresholds hold the state", () => {
    const mid = (DEFAULT_SPEECH.openAbove + DEFAULT_SPEECH.closeBelow) / 2;
    expect(run(rep(mid, 40)).speaking).toBe(false);
    expect(run(rep(mid, 40), 16, run(rep(0.1, 10))).speaking).toBe(true);
  });
  it("caps a huge frame gap", () => {
    expect(stepSpeech(SPEECH_IDLE, 0.1, 10_000).aboveMs).toBe(250);
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
