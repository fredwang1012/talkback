/**
 * Keeps the lip-synced face locked to the voice.
 *
 * The voice is the separate TTS mp3 and it is the clock: nothing here ever pauses, seeks or rewinds it. The streamed
 * video (muted) follows it. Using the video's own AAC track instead would tie the voice to Boson's render speed and
 * stalls, and that track is 40 ms late anyway (un-trimmed AAC priming, measured on the cached clips).
 *
 * Why the start used to stutter (#66): Boson renders at about real time, and the first 0.79 s fragment arrives
 * ~1.5-3 s after the request, so a clip that joins a voice which is already playing is always behind it. Seeking the
 * video to the voice asked for video that does not exist yet: the stream's init segment has no duration, so MSE's
 * `seekable` is [0, buffered end] and the browser CLAMPS the seek to the buffered end. The seek's `waiting` then
 * paused the voice, and on `playing` the voice was moved back to the (clamped) video: a stutter and a ~0.5 s rewind.
 *
 * Now:
 *  - start gate: `join` before the voice starts returns a promise that resolves once the clip has its first frame and
 *    enough of it is buffered (`startLeadFor`: render-speed headroom for the sentence length). The speaker waits for
 *    it (bounded, see sentencePlayer) and then the voice and the video start together from 0. No seek at all.
 *  - the face is shown only while it plays in step. If the video stalls (`waiting`: the render fell behind) or
 *    drifts too far, it is hidden (the talk loop stands in) and the voice carries on untouched.
 *  - rejoin by rendezvous: only when the stream has REJOIN_LEAD_S rendered ahead of the voice, the hidden video is
 *    seeked to a point slightly ahead of the voice (never past the buffered end) and started the moment the voice
 *    gets there, then shown. A late clip (the gate timed out) joins the same way.
 *  - small drift is corrected by nudging playbackRate (driftController), never by a visible seek.
 */

import { AudioClock, DriftController, type DriftOptions } from "./driftController";
import type { AudioLike, ClipSync, VideoClip } from "./sentencePlayer";

export interface VideoLike {
  src: string;
  currentTime: number;
  playbackRate?: number;
  readyState?: number;
  paused?: boolean;
  duration?: number;
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

/**
 * Seconds of clip to have buffered before the voice starts. Boson streams at roughly real time (0.75 s fragments,
 * measured ~1.0-1.1x), so the head start must cover one fragment of granularity plus ~9% of the sentence for a render
 * up to ~1.1x slower than real time. A clip that is complete (short sentence) is ready as soon as it is all in.
 */
export const START_LEAD_BASE_S = 0.7;
export const START_LEAD_PER_S = 0.09;
export const startLeadFor = (voiceS: number | null | undefined): number => {
  const d = voiceS && Number.isFinite(voiceS) && voiceS > 0 ? voiceS : 6;
  return Math.min(d, START_LEAD_BASE_S + START_LEAD_PER_S * d);
};

export interface FaceSyncOptions {
  checkMs?: number;
  startOffsetS?: number;
  drift?: Partial<DriftOptions>;
  startLead?: (voiceS: number | null) => number;
  now?: () => number;
  timers?: Timers;
}

const REJOIN_LEAD_S = 1;        // rejoin only with this much video rendered ahead of the voice
const REJOIN_BACKOFF_MS = 1000; // after a stall, wait this long before trying again; doubles after each stall
const REJOIN_BACKOFF_MAX_MS = 8000;
const SEEK_LEAD_S = 0.25;       // rendezvous: seek this far ahead of the voice (grows if seeks are slower)
const START_SLACK_S = 0.15;     // the voice may be this far in when the video starts from 0 with it
const SEEK_TIMEOUT_MS = 1500;

type Phase =
  | "loading"  // joined before the voice: buffering at the start, the speaker waits for the gate
  | "starting" // play() called, waiting for the video's 'playing' to show it
  | "shown"    // in step and visible
  | "paused"   // the voice was paused (not by us): the face freezes with it
  | "hidden"   // the voice plays, the face does not (talk loop): rejoin when enough is rendered
  | "seeking"  // rendezvous seek in flight
  | "armed"    // seeked ahead of the voice: starts when the voice gets there
  | "done";    // the clip is over or broken

export class FaceSync implements ClipSync {
  private video: VideoLike | null = null;
  private active: { detach: () => void; openGate: () => void } | null = null;
  private shown = false;
  private tick: unknown = null;
  private arm: unknown = null;
  private ctrl: DriftController;

  private onShown: (shown: boolean) => void;
  private opts: FaceSyncOptions;

  constructor(onShown: (shown: boolean) => void, opts: FaceSyncOptions = {}) {
    this.onShown = onShown;
    this.opts = opts;
    this.ctrl = new DriftController({ ...opts.drift, ...(opts.startOffsetS !== undefined ? { startOffsetS: opts.startOffsetS } : {}) });
  }

  /** Constant lead of the video over the voice (seconds), if one is ever measured; 0 by default. */
  setStartOffset(s: number) { this.ctrl.setStartOffset(s); }

  /** False while no face <video> is mounted: the speaker then has nothing to wait for. */
  get canShow(): boolean { return this.video !== null; }

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

  /**
   * Attach a sentence's clip. `late`: the voice is already playing (the clip joins by rendezvous when it can).
   * Otherwise the returned promise resolves when the voice may start: the clip is buffered enough to start with it
   * from 0 (or never will be: no video element, error, or the sentence was left).
   */
  join(audio: AudioLike, clip: VideoClip, late = false): Promise<void> {
    const v = this.video;
    this.leave();
    if (!v) { clip.release(); return Promise.resolve(); }
    const timers = this.timers;
    const checkMs = this.opts.checkMs ?? 50;
    const startLead = this.opts.startLead ?? startLeadFor;
    const ctrl = this.ctrl;
    ctrl.reset();
    const clk = new AudioClock();
    const offset = () => ctrl.opts.startOffsetS;

    let openGate: () => void = () => {};
    const gate = new Promise<void>((resolve) => { openGate = resolve; });
    let phase: Phase = late ? "hidden" : "loading";
    let loaded = false;
    let voiceOn = false;
    let target = 0;
    let seekLead = SEEK_LEAD_S;
    let seekTicks = 0;
    let backoffMs = REJOIN_BACKOFF_MS;
    let backoffTicks = 0;
    if (late) openGate();

    const voicePos = () => clk.sample(audio.currentTime ?? 0, this.now());
    const bufferedEnd = (): number | null => {
      const b = v.buffered;
      if (!b) return null;
      return b.length ? b.end(b.length - 1) : 0;
    };
    const finite = (x: number | undefined): x is number => typeof x === "number" && Number.isFinite(x);
    const complete = () => { const e = bufferedEnd(); return finite(v.duration) && e !== null && e >= v.duration - 0.05; };
    const prerolled = () => {
      if (!loaded) return false;
      const e = bufferedEnd();
      if (e === null || complete()) return true;
      const d = finite(audio.duration) ? audio.duration : null;
      return e >= startLead(d) + offset();
    };
    const cancelArm = () => { if (this.arm !== null) { timers.clearTimeout(this.arm); this.arm = null; } };
    const hide = () => { cancelArm(); this.show(false); v.pause(); if (v.playbackRate !== undefined) v.playbackRate = 1; };
    const stall = () => { // the render fell behind the voice: talk loop, and try again later, less eagerly each time
      hide();
      phase = "hidden";
      backoffTicks = Math.ceil(backoffMs / checkMs);
      backoffMs = Math.min(backoffMs * 2, REJOIN_BACKOFF_MAX_MS);
    };
    const play = () => {
      phase = "starting";
      ctrl.reset();
      if (v.playbackRate !== undefined) v.playbackRate = 1;
      void Promise.resolve(v.play()).catch(() => { if (phase === "starting") phase = "hidden"; }); // autoplay refused
    };
    const tryRendezvous = () => {
      const at = voicePos() + offset() + seekLead;
      if (finite(v.duration) && at >= v.duration - 0.05) { phase = "done"; return; } // the voice is past the clip
      const e = bufferedEnd();
      if (e !== null && e < at + (complete() ? 0.05 : REJOIN_LEAD_S)) return; // not rendered that far yet: never seek into nothing
      if ((v.readyState ?? 4) < 1) return;
      v.pause();
      phase = "seeking";
      target = at;
      seekTicks = 0;
      v.currentTime = at;
    };
    const onSeeked = () => {
      if (phase !== "seeking") return;
      const wait = target - (voicePos() + offset());
      if (wait < -0.03) { seekLead = Math.min(1, seekLead * 2); phase = "hidden"; return; } // the seek was slower than the lead
      phase = "armed";
      this.arm = timers.setTimeout(() => { this.arm = null; if (phase === "armed") play(); }, Math.max(0, wait * 1000));
    };
    const onVoicePlaying = () => {
      clk.anchor(audio.currentTime ?? 0, this.now());
      if (!voiceOn) {
        voiceOn = true;
        openGate(); // the voice started (normally because the gate opened; else the speaker gave up waiting)
        if (phase === "loading") {
          const e = bufferedEnd();
          const startable = loaded && (e === null || e > 0.3 + offset()) && voicePos() < START_SLACK_S;
          if (startable) play(); else phase = "hidden"; // start together from 0, or join later by rendezvous
        }
        return;
      }
      if (phase === "paused") { // the voice resumes where it was paused, and so does the face
        if (Math.abs(v.currentTime - (voicePos() + offset())) <= 0.08) play(); else { hide(); phase = "hidden"; }
      }
    };
    const onVoicePause = () => {
      if (!voiceOn) return;
      clk.pause(this.now());
      if (phase === "shown" || phase === "starting") { v.pause(); phase = "paused"; }
      else if (phase === "seeking" || phase === "armed") { cancelArm(); phase = "hidden"; }
    };
    const onLoaded = () => {
      loaded = true;
      if (phase === "loading" && offset() > 0 && Math.abs(v.currentTime - offset()) > 0.02) v.currentTime = offset();
    };
    const onPlaying = () => {
      if (phase !== "starting") return;
      phase = "shown";
      ctrl.reset();
      this.show(true);
    };
    const onWaiting = () => { if (phase === "shown" || phase === "starting") stall(); };
    const onEnded = () => { hide(); phase = "done"; };
    const onError = () => { hide(); phase = "done"; openGate(); };

    const handlers: [string, () => void][] = [
      ["loadeddata", onLoaded], ["playing", onPlaying], ["waiting", onWaiting], ["seeked", onSeeked],
      ["ended", onEnded], ["error", onError],
    ];
    for (const [t, f] of handlers) v.addEventListener(t, f);
    const canListen = typeof audio.addEventListener === "function";
    const audioHandlers: [string, () => void][] = [["playing", onVoicePlaying], ["pause", onVoicePause]];
    if (canListen) for (const [t, f] of audioHandlers) audio.addEventListener!(t, f);
    this.active = {
      openGate: () => openGate(),
      detach: () => {
        for (const [t, f] of handlers) v.removeEventListener(t, f);
        if (canListen) for (const [t, f] of audioHandlers) audio.removeEventListener?.(t, f);
      },
    };

    v.src = clip.url;
    v.load?.();
    if (late && (audio.currentTime ?? 0) > 0.02) { voiceOn = true; clk.anchor(audio.currentTime ?? 0, this.now()); }

    this.tick = timers.setInterval(() => {
      if (!voiceOn && (audio.currentTime ?? 0) > 0.02) onVoicePlaying(); // missed the 'playing' event
      if (phase === "loading" && prerolled()) openGate();
      if (!voiceOn || !clk.isRunning) return;
      if (phase === "hidden") {
        if (backoffTicks > 0) backoffTicks--;
        else tryRendezvous();
      } else if (phase === "seeking") {
        if (++seekTicks * checkMs >= SEEK_TIMEOUT_MS) stall();
      } else if (phase === "shown") {
        const act = ctrl.update(v.currentTime, voicePos(), this.now());
        if (act.kind === "seek") { hide(); phase = "hidden"; backoffTicks = 0; } // too far off: rejoin, no visible jump
        else if (act.kind === "rate" && v.playbackRate !== undefined) v.playbackRate = act.rate;
      }
    }, checkMs);
    return gate;
  }

  /** Detach from the sentence: the video is emptied and hidden; a speaker waiting on the gate is released. */
  leave(): void {
    const a = this.active;
    const timers = this.timers;
    if (this.tick !== null) { timers.clearInterval(this.tick); this.tick = null; }
    if (this.arm !== null) { timers.clearTimeout(this.arm); this.arm = null; }
    this.show(false);
    if (!a) return;
    this.active = null;
    a.detach();
    a.openGate();
    const v = this.video;
    if (v) {
      v.pause();
      v.removeAttribute?.("src");
      v.load?.();
    }
  }
}
