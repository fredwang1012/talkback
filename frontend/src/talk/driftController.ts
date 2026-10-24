/**
 * Pure drift logic shared by the Talk face (faceSync) and the read-mode lip-sync clip.
 *
 * Seeking a streamed video jumps frames (a visible twitch), and HTMLAudioElement.currentTime is coarse and noisy, so:
 *  - the voice position comes from an `AudioClock` anchored when the audio really starts and advanced by the wall
 *    clock; the raw `currentTime` only nudges it gently (or re-anchors it after a real jump such as a seek);
 *  - the measured drift is smoothed (EMA);
 *  - inside a dead-band nothing changes (with hysteresis: enter below 0.75x, leave above 1.25x, so a drift hovering
 *    at the edge does not flip the rate); outside it the video's playbackRate is nudged (clamped) so it catches up
 *    without a frame jump; only a large drift triggers a seek, and never more often than the cooldown.
 */
export interface DriftOptions {
  deadBandS: number;      // |smoothed drift| below this: leave the video alone
  hardSeekS: number;      // above this a seek is allowed
  seekCooldownMs: number; // minimum gap between seeks
  maxRateDelta: number;   // playbackRate stays within 1 +- this
  rateGain: number;       // rate change per second of drift
  emaAlpha: number;       // weight of each new drift sample
  startOffsetS: number;   // constant: how far the video leads (+) the voice at the same instant
}

export const DEFAULT_DRIFT: DriftOptions = {
  deadBandS: 0.04, hardSeekS: 0.3, seekCooldownMs: 1000, maxRateDelta: 0.08, rateGain: 0.6, emaAlpha: 0.3, startOffsetS: 0,
};

export type DriftAction =
  | { kind: "none"; rate: number }
  | { kind: "rate"; rate: number }
  | { kind: "seek"; to: number; rate: 1 };

/** Voice position estimate: anchored at play time, advanced by the wall clock, softly corrected by raw readings. */
export class AudioClock {
  private pos = 0;
  private at = 0;
  private running = false;
  private anchored = false;

  anchor(pos: number, nowMs: number, running = true) { this.pos = pos; this.at = nowMs; this.running = running; this.anchored = true; }
  pause(nowMs: number) { if (this.running) { this.pos = this.position(nowMs); this.running = false; } }
  resume(nowMs: number) { if (!this.running) { this.at = nowMs; this.running = true; } }
  get isRunning() { return this.running; }
  position(nowMs: number): number { return this.running ? this.pos + (nowMs - this.at) / 1000 : this.pos; }

  /** Feed a raw `currentTime` reading; returns the smoothed position. */
  sample(reading: number, nowMs: number, jumpS = 0.2, blend = 0.1): number {
    if (!this.anchored) { this.anchor(reading, nowMs); return reading; }
    const est = this.position(nowMs);
    if (Math.abs(reading - est) > jumpS) this.anchor(reading, nowMs, this.running);
    else if (this.running) { this.pos += (reading - est) * blend; }
    return this.position(nowMs);
  }
}

export class DriftController {
  readonly opts: DriftOptions;
  private ema: number | null = null;
  private lastSeekAt = -Infinity;
  private rate = 1;
  private inBand = true;

  constructor(opts: Partial<DriftOptions> = {}) { this.opts = { ...DEFAULT_DRIFT, ...opts }; }

  get smoothedDrift(): number { return this.ema ?? 0; }
  get currentRate(): number { return this.rate; }
  setStartOffset(s: number) { this.opts.startOffsetS = s; }

  /** Forget history, e.g. when a new clip starts or after the video was moved. */
  reset() { this.ema = null; this.rate = 1; this.inBand = true; }

  /** videoTime and audioTime in seconds (audioTime should come from AudioClock). */
  update(videoTime: number, audioTime: number, nowMs: number): DriftAction {
    const o = this.opts;
    const target = audioTime + o.startOffsetS;
    const raw = videoTime - target;
    this.ema = this.ema === null ? raw : this.ema + (raw - this.ema) * o.emaAlpha;
    const d = this.ema;

    if (Math.abs(d) > o.hardSeekS && Math.abs(raw) > o.hardSeekS && nowMs - this.lastSeekAt >= o.seekCooldownMs) {
      this.lastSeekAt = nowMs; this.ema = null; this.rate = 1; this.inBand = true;
      return { kind: "seek", to: Math.max(0, target), rate: 1 };
    }
    this.inBand = Math.abs(d) < o.deadBandS * (this.inBand ? 1.25 : 0.75);
    if (this.inBand) {
      if (this.rate !== 1) { this.rate = 1; return { kind: "rate", rate: 1 }; }
      return { kind: "none", rate: 1 };
    }
    // video ahead (d > 0) -> slow down; behind -> speed up
    const delta = Math.max(-o.maxRateDelta, Math.min(o.maxRateDelta, -d * o.rateGain));
    const next = 1 + delta;
    if (Math.abs(next - this.rate) < 0.005) return { kind: "none", rate: this.rate };
    this.rate = next;
    return { kind: "rate", rate: next };
  }
}
