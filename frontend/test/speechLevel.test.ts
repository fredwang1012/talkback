import { describe, expect, it } from "vitest";
import { SPEECH_IDLE, stepSpeech, type SpeechState } from "../src/reader/speechLevel";

const FRAME = 16.67;
/** Feed `levels` (one per ~60 Hz tick) and return the state after each. */
function run(levels: number[], s: SpeechState = SPEECH_IDLE): SpeechState[] {
  const out: SpeechState[] = [];
  for (const l of levels) { s = stepSpeech(s, l, FRAME); out.push(s); }
  return out;
}
const rep = (v: number, ms: number) => Array<number>(Math.round(ms / FRAME)).fill(v);
/** word-like speech: a loud syllable, a soft one, with level scaled by `gain` */
const speech = (gain: number, ms: number) => {
  const out: number[] = [];
  for (let t = 0; t < ms; t += 200) out.push(...rep(0.2 * gain, 120), ...rep(0.05 * gain, 80));
  return out;
};

describe("stepSpeech adaptive threshold", () => {
  it("opens within a few frames on loud and on 10x quieter speech alike", () => {
    for (const gain of [1, 0.25, 0.1, 0.05]) {
      const states = run([...rep(0, 300), ...speech(gain, 600)]);
      const first = states.findIndex((s) => s.speaking);
      const onsetMs = (first - rep(0, 300).length) * FRAME;
      expect(first, `gain ${gain}`).toBeGreaterThanOrEqual(0);
      expect(onsetMs, `gain ${gain}`).toBeLessThanOrEqual(50);
    }
  });

  it("does not flicker through soft syllables or short word gaps at any volume", () => {
    for (const gain of [1, 0.25, 0.1]) {
      const states = run([...rep(0, 100), ...speech(gain, 2000), ...rep(0.02 * gain, 60), ...speech(gain, 1000)]);
      const toggles = states.filter((s, i) => i > 0 && s.speaking !== states[i - 1].speaking).length;
      expect(toggles, `gain ${gain}`).toBe(1);
    }
  });

  it("releases after a real pause within ~150 ms at any volume", () => {
    for (const gain of [1, 0.25, 0.1]) {
      const talk = run([...rep(0, 100), ...speech(gain, 1500)]);
      const after = run(rep(0.001 * gain, 400), talk[talk.length - 1]);
      const first = after.findIndex((s) => !s.speaking);
      expect(first, `gain ${gain}`).toBeGreaterThanOrEqual(0);
      expect((first + 1) * FRAME, `gain ${gain}`).toBeLessThanOrEqual(150);
    }
  });

  it("never opens on digital silence or a faint noise floor", () => {
    const states = run([...rep(0, 500), ...rep(0.002, 500)]);
    expect(states.some((s) => s.speaking)).toBe(false);
  });

  it("forgets a loud passage: the threshold follows a quieter voice later", () => {
    let s = run(speech(1, 1000))[run(speech(1, 1000)).length - 1];
    s = run(rep(0, 300), s).pop()!;
    const quiet = run(speech(0.1, 12000), s);
    const tail = quiet.slice(-30);
    expect(tail.every((x) => x.speaking)).toBe(true);
    const gap = run(rep(0.001, 200), tail[tail.length - 1]);
    expect(gap[gap.length - 1].speaking).toBe(false);
  });
});
