/** Pure helpers that turn a stream of audio levels into a stable "is someone speaking" flag (read-mode face). */

export type SpeechConfig = {
  openAbove: number; // level that must be exceeded to start counting towards "speaking"
  closeBelow: number; // level that must be undercut to start counting towards "resting"
  openMs: number; // how long the level must stay above openAbove (short: attack fast)
  closeMs: number; // how long it must stay below closeBelow (longer: release slower, so word gaps do not flicker)
};

export const DEFAULT_SPEECH: SpeechConfig = { openAbove: 0.02, closeBelow: 0.012, openMs: 16, closeMs: 100 };

export type SpeechState = { speaking: boolean; aboveMs: number; belowMs: number };
export const SPEECH_IDLE: SpeechState = { speaking: false, aboveMs: 0, belowMs: 0 };

/** Advance the hysteresis by one frame of `dtMs`. Levels between closeBelow and openAbove hold the current state. */
export function stepSpeech(s: SpeechState, level: number, dtMs: number, cfg: SpeechConfig = DEFAULT_SPEECH): SpeechState {
  const dt = Math.max(0, Math.min(dtMs, 250)); // a backgrounded tab must not flip state in one jump
  let aboveMs = level > cfg.openAbove ? s.aboveMs + dt : 0;
  let belowMs = level < cfg.closeBelow ? s.belowMs + dt : 0;
  let speaking = s.speaking;
  if (!speaking && aboveMs >= cfg.openMs) { speaking = true; belowMs = 0; }
  else if (speaking && belowMs >= cfg.closeMs) { speaking = false; aboveMs = 0; }
  return { speaking, aboveMs, belowMs };
}

/** Root-mean-square of a block of samples (-1..1). */
export function rms(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/** Level (RMS) to a 0..1 drive for a still-image pulse. */
export function pulseAmount(level: number): number {
  return Math.max(0, Math.min(1, level / 0.12));
}
