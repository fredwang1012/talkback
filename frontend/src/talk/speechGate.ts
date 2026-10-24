/**
 * Pure helpers for the talking face and the per-stage timing log.
 *
 * `createPlayingGate` turns the sentence audio's raw playing/not-playing events into the "speaking" flag the face
 * follows. Going true is immediate; going false waits a short moment, so the tiny gap between two prefetched
 * sentences never flashes the idle face. A real silence (synthesising the next sentence) outlasts the delay.
 */
export function createPlayingGate(
  set: (speaking: boolean) => void,
  releaseMs = 220,
  timers: { set: (fn: () => void, ms: number) => unknown; clear: (h: unknown) => void } = {
    set: (fn, ms) => setTimeout(fn, ms), clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  },
): { playing: (on: boolean) => void; reset: () => void } {
  let value = false;
  let pending: unknown = null;
  const apply = (v: boolean) => { if (v !== value) { value = v; set(v); } };
  const cancel = () => { if (pending !== null) { timers.clear(pending); pending = null; } };
  return {
    playing(on) {
      cancel();
      if (on) apply(true);
      else if (value) pending = timers.set(() => { pending = null; apply(false); }, releaseMs);
    },
    reset() { cancel(); apply(false); },
  };
}

/** What the sync loop should do about the offset between the lip-sync video and the voice (seconds). */
export const DRIFT_LIMIT_S = 0.08;
export const driftSeconds = (videoTime: number, audioTime: number): number => videoTime - audioTime;
export const needsResync = (videoTime: number, audioTime: number, limit = DRIFT_LIMIT_S): boolean =>
  Math.abs(driftSeconds(videoTime, audioTime)) > limit;

/** Pipeline stages timed for each answer, relative to the question (first sentence) or to the sentence (the rest). */
/** "face-wait": how long the first word waited for its lip-synced face (start gate, sentencePlayer). */
export type TimingStage = "first-sentence" | "tts" | "avatar-first-fragment" | "face-wait" | "audio-start";

export interface TimingEvent { stage: TimingStage; ms: number; sentence?: string }

/** Starts a clock per key; `lap` returns the elapsed ms once (a repeated lap of the same key is ignored). */
export function createStopwatch(now: () => number = () => Date.now()) {
  const starts = new Map<string, number>();
  const done = new Set<string>();
  return {
    start(key: string) { starts.set(key, now()); done.delete(key); },
    lap(key: string): number | null {
      const s = starts.get(key);
      if (s === undefined || done.has(key)) return null;
      done.add(key);
      return Math.max(0, now() - s);
    },
    clear() { starts.clear(); done.clear(); },
  };
}
