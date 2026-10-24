import { rms } from "./speechLevel";

/**
 * Measures the loudness of whichever HTMLAudioElement the reader is playing, through one shared AnalyserNode.
 * A media element can feed createMediaElementSource only once, so sources are cached per element.
 * If Web Audio is unavailable or the context will not run, level() returns null and callers fall back to "playing".
 */
export class AudioLevelMeter {
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private buf: Float32Array<ArrayBuffer> | null = null;
  private sources = new WeakMap<HTMLAudioElement, MediaElementAudioSourceNode>();
  private attached: { el: HTMLAudioElement; src: MediaElementAudioSourceNode } | null = null;
  private resuming = false;
  private broken = false;

  /** Call from a user gesture (play click) so the context may start. */
  unlock(): void {
    if (this.broken || typeof window === "undefined") return;
    if (!this.ctx) {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AC) { this.broken = true; return; }
      try {
        this.ctx = new AC();
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 1024;
        this.analyser.smoothingTimeConstant = 0;
        this.buf = new Float32Array(this.analyser.fftSize);
      } catch { this.broken = true; this.ctx = null; return; }
    }
    if (this.ctx.state === "suspended" && !this.resuming) {
      this.resuming = true;
      this.ctx.resume().catch(() => {}).finally(() => { this.resuming = false; });
    }
  }

  /** Route `el` through the analyser (and on to the speakers). Only once the context is running, so sound never goes silent. */
  private attach(el: HTMLAudioElement): boolean {
    if (!this.ctx || !this.analyser || this.ctx.state !== "running") return false;
    if (this.attached?.el === el) return true;
    this.detach();
    try {
      let src = this.sources.get(el);
      if (!src) { src = this.ctx.createMediaElementSource(el); this.sources.set(el, src); }
      src.connect(this.analyser);
      this.analyser.connect(this.ctx.destination); // idempotent; keeps the audio audible
      this.attached = { el, src };
      return true;
    } catch { this.broken = true; return false; }
  }

  private detach(): void {
    if (!this.attached) return;
    try { this.attached.src.disconnect(); } catch { /* already gone */ }
    // the element keeps playing only if re-attached; a finished/old chunk is silent anyway
    this.attached = null;
  }

  /** RMS of the last ~23 ms of the playing element, or null when no measurement is possible. */
  level(el: unknown, playing: boolean): number | null {
    if (this.broken) return null;
    if (!(typeof HTMLAudioElement !== "undefined" && el instanceof HTMLAudioElement)) return null;
    this.unlock();
    // Attach even while not yet "playing": routing an element through Web Audio after it has started playing
    // makes it blip as the output path switches, audibly at every sentence boundary.
    const attached = this.attach(el);
    if (!playing) { return 0; }
    if (!attached || !this.analyser || !this.buf) return null;
    this.analyser.getFloatTimeDomainData(this.buf);
    return rms(this.buf);
  }

  dispose(): void {
    this.detach();
    this.attached = null;
    const ctx = this.ctx;
    this.ctx = null; this.analyser = null; this.buf = null;
    void ctx?.close().catch(() => {});
  }
}
