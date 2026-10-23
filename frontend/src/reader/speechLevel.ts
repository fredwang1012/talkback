/** Pure helpers that turn a stream of audio levels into a stable "is someone speaking" flag (read-mode face). */

export type SpeechConfig = {
  openRatio: number; // level must exceed this fraction of the running peak level to count towards "speaking"
  closeRatio: number; // level must undercut this fraction of the peak to count towards "resting" (below openRatio: hysteresis)
  floor: number; // absolute minimum for the open threshold, so a noise floor or digital silence never opens (close uses 0.6x)
  peakDecayMs: number; // time constant at which the running peak forgets a louder passage, so quiet voices adapt
  openMs: number; // how long the level must stay above the open threshold (short: attack fast)
  closeMs: number; // how long it must stay below the close threshold (longer: release slower, so word gaps do not flicker)
};

/**
 * Thresholds are relative to the voice's own running peak, so a quiet cloned voice and a loud preset behave alike.
 * Measured (frontend scratch sim over the real voice-clip.wav at 0.05x..3x gain): the old fixed 0.02/0.012 stayed
 * "speaking" through pauses on loud audio and lagged 90-430 ms (up to 67% of speech missed) on quiet audio.
 */
export const DEFAULT_SPEECH: SpeechConfig = { openRatio: 0.2, closeRatio: 0.1, floor: 0.003, peakDecayMs: 4000, openMs: 16, closeMs: 100 };

export type SpeechState = { speaking: boolean; aboveMs: number; belowMs: number; peak: number };
export const SPEECH_IDLE: SpeechState = { speaking: false, aboveMs: 0, belowMs: 0, peak: 0 };

/** Advance the hysteresis by one frame of `dtMs`. Levels between the close and open thresholds hold the current state. */
export function stepSpeech(s: SpeechState, level: number, dtMs: number, cfg: SpeechConfig = DEFAULT_SPEECH): SpeechState {
  const dt = Math.max(0, Math.min(dtMs, 250)); // a backgrounded tab must not flip state in one jump
  const peak = Math.max(level, s.peak * Math.exp(-dt / cfg.peakDecayMs), cfg.floor);
  const openAbove = Math.max(cfg.floor, cfg.openRatio * peak);
  const closeBelow = Math.max(cfg.floor * 0.6, cfg.closeRatio * peak);
  let aboveMs = level > openAbove ? s.aboveMs + dt : 0;
  let belowMs = level < closeBelow ? s.belowMs + dt : 0;
  let speaking = s.speaking;
  if (!speaking && aboveMs >= cfg.openMs) { speaking = true; belowMs = 0; }
  else if (speaking && belowMs >= cfg.closeMs) { speaking = false; aboveMs = 0; }
  return { speaking, aboveMs, belowMs, peak };
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
