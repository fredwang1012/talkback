/**
 * Keeps the lip-synced face locked to the voice.
 *
 * Design choice: the voice stays the separate TTS mp3 (it starts instantly and drives the sentence timing), and the
 * streamed video, which is muted, follows it. Using the video's own AAC track would tie the voice to the stream's
 * start-up time and to its stalls, which is the opposite of what we want, so the mp3 is the clock.
 *
 * One persistent <video> element is handed in with `setVideo`. For each sentence `join` points it at the stream:
 *  - the video is only shown (`onShown(true)`) once it is actually playing a decoded frame, so there is no black flash
 *    and the talk loop or idle face stays visible underneath until then;
 *  - on joining it seeks to `audio.currentTime`, so a clip that arrives mid-sentence matches the voice;
 *  - while it plays, an offset above DRIFT_LIMIT_S is corrected by seeking the video to the voice;
 *  - if the video `waiting`s (Boson renders slower than real time), the voice is paused (`holding`) so the two stay
 *    together; if the stall lasts longer than `stallMs` the face falls back to the talk loop and the voice carries
 *    on. When the video recovers it seeks to the voice and is shown again.
 */

import { DRIFT_LIMIT_S, needsResync } from "./speechGate";
import type { AudioLike, ClipSync, VideoClip } from "./sentencePlayer";

export interface VideoLike {
  src: string;
  currentTime: number;
  readyState?: number;
  play(): Promise<void> | void;
  pause(): void;
  load?(): void;
  removeAttribute?(name: string): void;
  addEventListener(type: string, fn: () => void): void;
  removeEventListener(type: string, fn: () => void): void;
}

type Timers = {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (h: unknown) => void;
  setInterval: (fn: () => void, ms: number) => unknown;
  clearInterval: (h: unknown) => void;
};

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (h) => clearInterval(h as ReturnType<typeof setInterval>),
};

export class FaceSync implements ClipSync {
  holding = false;
  private video: VideoLike | null = null;
  private active: { audio: AudioLike; detach: () => void } | null = null;
  private shown = false;
  private stall: unknown = null;
  private tick: unknown = null;
  private lastSeek = -Infinity;

  private onShown: (shown: boolean) => void;
  private opts: { stallMs?: number; checkMs?: number; seekCooldownMs?: number; now?: () => number; timers?: Timers };

  constructor(
    onShown: (shown: boolean) => void,
    opts: { stallMs?: number; checkMs?: number; seekCooldownMs?: number; now?: () => number; timers?: Timers } = {},
  ) {
    this.onShown = onShown;
    this.opts = opts;
  }

  private get timers() { return this.opts.timers ?? realTimers; }
  private now() { return (this.opts.now ?? Date.now)(); }

  /** The face's one persistent <video> (or null when it unmounts). */
  setVideo(v: VideoLike | null): void {
    if (v === this.video) return;
    this.leave();
    this.video = v;
  }

  private show(on: boolean) {
    if (this.shown === on) return;
    this.shown = on;
    this.onShown(on);
  }

  join(audio: AudioLike, clip: VideoClip): void {
    const v = this.video;
    this.leave();
    if (!v) { clip.release(); return; }
    const timers = this.timers;
    const stallMs = this.opts.stallMs ?? 1500;

    const align = () => { // seek the video to where the voice is
      const t = audio.currentTime ?? 0;
      if (Math.abs(v.currentTime - t) > 0.02) { v.currentTime = t; this.lastSeek = this.now(); }
    };
    const resumeVoice = () => {
      if (!this.holding) return;
      this.holding = false;
      void Promise.resolve(audio.play()).catch(() => {});
    };
    const onLoaded = () => { // first decoded frame: line up with the voice and start
      align();
      void Promise.resolve(v.play()).catch(() => { /* autoplay refused: the talk loop stays */ });
    };
    const onPlaying = () => {
      if (this.stall !== null) { timers.clearTimeout(this.stall); this.stall = null; }
      if (this.holding) { // the video caught up: put the voice where the video is and carry on together
        if (needsResync(v.currentTime, audio.currentTime ?? 0)) audio.currentTime = v.currentTime;
        resumeVoice();
      } else if (!this.shown) {
        align(); // coming back from a fallback: the voice kept going
      }
      this.show(true);
    };
    const onWaiting = () => {
      if (!this.shown && !this.holding && (v.readyState ?? 0) < 2 && (audio.currentTime ?? 0) < 0.05) return; // still starting up
      if (!this.holding) { this.holding = true; audio.pause(); }
      if (this.stall === null) {
        this.stall = timers.setTimeout(() => { // too slow: the talk loop stands in and the voice goes on
          this.stall = null;
          this.show(false);
          resumeVoice();
        }, stallMs);
      }
    };
    const onEnded = () => { // the video is shorter than the voice: stop following it
      this.show(false);
      resumeVoice();
      if (this.tick !== null) { timers.clearInterval(this.tick); this.tick = null; }
    };

    const handlers: [string, () => void][] = [
      ["loadeddata", onLoaded], ["playing", onPlaying], ["waiting", onWaiting], ["ended", onEnded],
    ];
    for (const [t, f] of handlers) v.addEventListener(t, f);
    this.active = {
      audio,
      detach: () => { for (const [t, f] of handlers) v.removeEventListener(t, f); },
    };

    v.src = clip.url;
    v.load?.();

    this.tick = timers.setInterval(() => {
      if (!this.shown || this.holding || audio.currentTime === undefined) return;
      if (this.now() - this.lastSeek < (this.opts.seekCooldownMs ?? 1000)) return;
      if (needsResync(v.currentTime, audio.currentTime, DRIFT_LIMIT_S)) { v.currentTime = audio.currentTime; this.lastSeek = this.now(); }
    }, this.opts.checkMs ?? 250);
  }

  /** Detach from the sentence: the voice resumes if it was held, the video is emptied and hidden. */
  leave(): void {
    const a = this.active;
    const timers = this.timers;
    if (this.stall !== null) { timers.clearTimeout(this.stall); this.stall = null; }
    if (this.tick !== null) { timers.clearInterval(this.tick); this.tick = null; }
    this.holding = false;
    this.show(false);
    if (!a) return;
    this.active = null;
    a.detach();
    const v = this.video;
    if (v) {
      v.pause();
      v.removeAttribute?.("src");
      v.load?.();
    }
  }
}
