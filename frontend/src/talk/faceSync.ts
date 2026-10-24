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
 *  - the video only starts once the voice is really playing (no mouthing before the voice);
 *  - while it plays, drift is measured against a smoothed voice clock (see driftController): small drift nudges the
 *    video's playbackRate, only a big one (>300 ms, at most once a second) seeks, a dead-band leaves it alone;
 *  - if the video `waiting`s (Boson renders slower than real time), the voice is paused (`holding`) so the two stay
 *    together; if the stall lasts longer than `stallMs` the face falls back to the talk loop and the voice carries
 *    on. After that give-up the voice is never held again for this sentence: the face stays on the talk loop and
 *    rejoins (seek to the voice, shown on the next `playing`) only after a back-off and once the stream has at
 *    least REJOIN_LEAD_S buffered ahead of the voice; a rejoin that stalls hides the face again and doubles the
 *    back-off. (Holding again would make a slower-than-real-time render stutter the voice every few seconds.)
 */

import { needsResync } from "./speechGate";
import { AudioClock, DriftController, type DriftOptions } from "./driftController";
import type { AudioLike, ClipSync, VideoClip } from "./sentencePlayer";

export interface VideoLike {
  src: string;
  currentTime: number;
  playbackRate?: number;
  readyState?: number;
  buffered?: { length: number; end(i: number): number };
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

export interface FaceSyncOptions {
  stallMs?: number; checkMs?: number; seekCooldownMs?: number; startOffsetS?: number;
  drift?: Partial<DriftOptions>;
  /** Called with (video position - voice position) the moment the video first shows, to measure the start offset. */
  onStartOffset?: (offsetS: number) => void;
  now?: () => number; timers?: Timers;
}

const REJOIN_LEAD_S = 1;        // after a give-up: rejoin only with this much video buffered ahead of the voice
const REJOIN_BACKOFF_MS = 2000; // first wait before a rejoin attempt; doubles after each failed one
const REJOIN_BACKOFF_MAX_MS = 8000;

export class FaceSync implements ClipSync {
  holding = false;
  private video: VideoLike | null = null;
  private active: { audio: AudioLike; detach: () => void } | null = null;
  private shown = false;
  private stall: unknown = null;
  private tick: unknown = null;
  private clock = new AudioClock();
  private ctrl: DriftController;

  private onShown: (shown: boolean) => void;
  private opts: FaceSyncOptions;

  constructor(onShown: (shown: boolean) => void, opts: FaceSyncOptions = {}) {
    this.onShown = onShown;
    this.opts = opts;
    this.ctrl = new DriftController({ ...opts.drift, ...(opts.seekCooldownMs !== undefined ? { seekCooldownMs: opts.seekCooldownMs } : {}), ...(opts.startOffsetS !== undefined ? { startOffsetS: opts.startOffsetS } : {}) });
  }

  /** Constant lead of the video over the voice, measured once per device and applied to every sentence. */
  setStartOffset(s: number) { this.ctrl.setStartOffset(s); }

  private get timers() { return this.opts.timers ?? realTimers; }
  private now() { return (this.opts.now ?? (() => performance.now()))(); }

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

    const ctrl = this.ctrl;
    ctrl.reset();
    this.clock = new AudioClock();
    const clk = this.clock;
    const canListen = typeof audio.addEventListener === "function";
    let audioStarted = !canListen || (audio.currentTime ?? 0) > 0.02;
    let loaded = false;
    let gaveUp = false;     // the stall fallback fired: the voice is never held again in this sentence
    let rejoining = false;  // a rejoin seek + play is in flight: show the face on the next 'playing'
    let backoffMs = REJOIN_BACKOFF_MS;
    let rejoinTicks = 0;
    let rejoinWaitTicks = 0;
    const checkMs = this.opts.checkMs ?? 100;
    const voicePos = () => clk.sample(audio.currentTime ?? 0, this.now());
    if (audioStarted) clk.anchor(audio.currentTime ?? 0, this.now());
    const align = () => { // seek the video to where the voice is (once, on joining / rejoining)
      const t = voicePos() + ctrl.opts.startOffsetS;
      if (Math.abs(v.currentTime - t) > 0.02) { v.currentTime = t; ctrl.reset(); }
      if (v.playbackRate !== undefined) v.playbackRate = 1;
    };
    const startVideo = () => { // only when the first frame is decoded AND the voice is really playing
      if (!loaded || !audioStarted || this.holding) return;
      if (gaveUp && !this.shown) return; // fallen back: the tick decides when to rejoin
      align();
      void Promise.resolve(v.play()).catch(() => { /* autoplay refused: the talk loop stays */ });
    };
    const resumeVoice = () => {
      if (!this.holding) return;
      this.holding = false;
      void Promise.resolve(audio.play()).catch(() => {});
    };
    const bufferedAhead = (): number | null => { // seconds of video buffered past where the voice is; null: unknown
      const b = v.buffered;
      if (!b) return null;
      return b.length ? b.end(b.length - 1) - (voicePos() + ctrl.opts.startOffsetS) : 0;
    };
    const fallBack = () => { // the talk loop stands in; try again later, less eagerly each time
      gaveUp = true;
      rejoining = false;
      this.show(false);
      v.pause();
      rejoinTicks = Math.ceil(backoffMs / checkMs);
      backoffMs = Math.min(backoffMs * 2, REJOIN_BACKOFF_MAX_MS);
    };
    const tryRejoin = () => {
      if (rejoinTicks > 0) rejoinTicks--;
      if (rejoinTicks > 0) return;
      const ahead = bufferedAhead();
      if (ahead !== null && ahead < REJOIN_LEAD_S) return; // still rendering behind the voice: keep waiting
      rejoining = true;
      rejoinWaitTicks = 0;
      v.pause(); // play() from paused fires 'playing' once the seeked-to frame is ready
      align();
      void Promise.resolve(v.play()).catch(() => { rejoining = false; });
    };
    const onLoaded = () => { loaded = true; startVideo(); };
    const onAudioPlaying = () => {
      if (!audioStarted) { audioStarted = true; clk.anchor(audio.currentTime ?? 0, this.now()); startVideo(); return; }
      if (!clk.isRunning) { clk.anchor(audio.currentTime ?? 0, this.now()); if (!this.holding) startVideo(); }
    };
    const onAudioPause = () => { // the reader paused (not our own hold): freeze the face with the voice
      if (this.holding) return;
      clk.pause(this.now());
      v.pause();
    };
    const onPlaying = () => {
      if (this.stall !== null) { timers.clearTimeout(this.stall); this.stall = null; }
      if (gaveUp) { // only a rejoin we started may bring the face back
        if (rejoining && !this.shown) { rejoining = false; this.show(true); }
        return;
      }
      if (this.holding) { // the video caught up: put the voice where the video is and carry on together
        if (needsResync(v.currentTime, audio.currentTime ?? 0)) audio.currentTime = v.currentTime;
        clk.anchor(v.currentTime, this.now(), false);
        resumeVoice();
        clk.resume(this.now());
      } else if (!this.shown) {
        align(); // coming back from a fallback: the voice kept going
      }
      if (!this.shown && this.opts.onStartOffset) this.opts.onStartOffset(v.currentTime - voicePos());
      this.show(true);
    };
    const onWaiting = () => {
      if (gaveUp) { if (this.shown || rejoining) fallBack(); return; } // never hold the voice again
      if (!this.shown && !this.holding && (v.readyState ?? 0) < 2 && (audio.currentTime ?? 0) < 0.05) return; // still starting up
      if (!this.holding) { this.holding = true; audio.pause(); }
      if (this.stall === null) {
        this.stall = timers.setTimeout(() => { // too slow: the talk loop stands in and the voice goes on
          this.stall = null;
          resumeVoice();
          fallBack();
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
    const audioHandlers: [string, () => void][] = [["playing", onAudioPlaying], ["pause", onAudioPause]];
    if (canListen) for (const [t, f] of audioHandlers) audio.addEventListener!(t, f);
    this.active = {
      audio,
      detach: () => {
        for (const [t, f] of handlers) v.removeEventListener(t, f);
        if (canListen) for (const [t, f] of audioHandlers) audio.removeEventListener?.(t, f);
      },
    };

    v.src = clip.url;
    v.load?.();

    this.tick = timers.setInterval(() => {
      if (!audioStarted && canListen && (audio.currentTime ?? 0) > 0.02) { // missed the 'playing' event
        audioStarted = true; clk.anchor(audio.currentTime ?? 0, this.now()); startVideo();
      }
      if (gaveUp && !this.shown && clk.isRunning && !this.holding) {
        if (!rejoining) tryRejoin();
        else if (++rejoinWaitTicks * checkMs >= stallMs) fallBack(); // the rejoin never got going
        return;
      }
      if (!this.shown || this.holding || audio.currentTime === undefined || !clk.isRunning) return;
      const act = ctrl.update(v.currentTime, voicePos(), this.now());
      if (act.kind === "seek") v.currentTime = act.to;
      else if (act.kind === "rate" && v.playbackRate !== undefined) v.playbackRate = act.rate;
    }, checkMs);
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
