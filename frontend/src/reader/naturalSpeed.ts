/** Natural speed (#78): at the speeds Boson has a prosody tag for, the voice itself is re-spoken fast or slow (and
 * plays at 1.0x) instead of being sped up with playbackRate. Opt-in, because every tagged chunk is a new Boson call. */
export type SpeedTag = "very_slow" | "slow" | "fast" | "very_fast";

export const NATURAL_SPEED_KEY = "talkback.naturalSpeed";

const TAGS: Record<number, SpeedTag> = { 0.75: "slow", 1.25: "fast", 1.5: "very_fast" };

/** The Boson speed tag for a reader speed, or null (off, 1x, or a speed without a tag: playbackRate is used). */
export function speedTagFor(speed: number, natural: boolean): SpeedTag | null {
  return natural ? TAGS[speed] ?? null : null;
}

/** playbackRate for audio that was voiced with `tag` (tagged audio already has the pace: 1.0x). */
export function rateFor(speed: number, tag: SpeedTag | null): number {
  return tag ? 1 : speed;
}

export function loadNaturalSpeed(): boolean {
  try { return localStorage.getItem(NATURAL_SPEED_KEY) === "1"; } catch { return false; }
}

export function saveNaturalSpeed(on: boolean): void {
  try { localStorage.setItem(NATURAL_SPEED_KEY, on ? "1" : "0"); } catch { /* private mode: not remembered */ }
}
