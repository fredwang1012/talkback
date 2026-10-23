import { AudioClock, DriftController } from "../talk/driftController";

/** The bits of an <audio> / <video> element the follower needs (so it can be tested with fakes). */
export interface FollowAudio { currentTime: number; paused?: boolean; ended?: boolean; playbackRate?: number; }
export interface FollowVideo {
  currentTime: number; playbackRate: number; paused: boolean; ended?: boolean; duration?: number;
  readyState?: number; seeking?: boolean;
  play(): Promise<void> | void; pause(): void;
}

/** True only while the voice is really being heard: not paused, not ended, and past the very start. */
export const audioSounding = (a: FollowAudio | null | undefined): boolean =>
  !!a && !a.paused && !a.ended && a.currentTime > 0;

/** The clip has played to its end (it can be a little shorter than its audio): hold the last frame. */
const pastEnd = (v: FollowVideo, at: number): boolean =>
  typeof v.duration === "number" && Number.isFinite(v.duration) && at >= v.duration - 0.05;
const clipOver = (v: FollowVideo): boolean => !!v.ended || pastEnd(v, v.currentTime);

type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (h: unknown) => void };
const realTimers: Timers = { set: (fn, ms) => setTimeout(fn, ms), clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>) };

const SEEK_LEAD_S = 0.2;  // rendezvous: seek this far ahead of the voice (doubles if the seek was slower)
const SEEK_TIMEOUT_TICKS = 30;

/**
 * Keeps a pre-rendered (muted) lip-sync clip locked to the chunk's audio in read mode. Poll `tick` every ~100 ms.
 *
 * The clip never plays out of step (#66). When the voice starts (or resumes, or the clip drifts far) the clip is
 * paused, seeked to a point slightly ahead of the voice, and started by a timer the moment the voice gets there; until
 * then it holds its frame. Before, it was played at once while still loading or seeking, so it started behind the
 * voice and was then corrected by visible jumps. While in step, small drift nudges playbackRate (talk/driftController).
 */
export function createClipFollower(video: FollowVideo, now: () => number = () => performance.now(), startOffsetS = 0,
  timers: Timers = realTimers) {
  const ctrl = new DriftController({ startOffsetS });
  let clock = new AudioClock();
  let lastAudio: FollowAudio | null = null;
  let phase: "idle" | "align" | "seeking" | "armed" | "synced" | "over" = "idle";
  let target = 0;
  let lead = SEEK_LEAD_S;
  let seekTicks = 0;
  let arm: unknown = null;
  const cancelArm = () => { if (arm !== null) { timers.clear(arm); arm = null; } };
  const ready = (min: number) => (video.readyState ?? 4) >= min;

  return {
    tick(audio: FollowAudio | null | undefined): void {
      const t = now();
      // the voice's speed (#105): the clip plays at the same rate, nudged by the drift controller
      const base = audio && typeof audio.playbackRate === "number" && audio.playbackRate > 0 ? audio.playbackRate : 1;
      if (audio !== lastAudio) { lastAudio = audio ?? null; clock = new AudioClock(); ctrl.reset(); cancelArm(); phase = "idle"; }
      if (!audioSounding(audio) || !audio) {
        cancelArm();
        if (!video.paused) video.pause();
        if (phase !== "idle") { clock.pause(t); phase = "idle"; }
        if (audio?.ended) video.playbackRate = 1;
        return;
      }
      if (phase === "idle") { // the voice (re)started: line the clip up before it moves
        clock.anchor(audio.currentTime, t);
        ctrl.reset();
        video.playbackRate = base;
        phase = "align";
      }
      clock.setRate(base, t);
      const voice = clock.sample(audio.currentTime, t);
      const pos = voice + ctrl.opts.startOffsetS; // where the clip should be now
      if (phase === "align") {
        if (!ready(1) || video.seeking) return; // no metadata yet: hold
        if (pastEnd(video, pos + lead)) { video.pause(); video.currentTime = video.duration!; phase = "over"; return; }
        if (ready(3) && Math.abs(video.currentTime - pos) <= 0.04) { // already there (e.g. resumed together)
          phase = "synced";
          void Promise.resolve(video.play()).catch(() => { /* autoplay refused: the frame stays */ });
          return;
        }
        if (!video.paused) video.pause();
        target = pos + lead;
        seekTicks = 0;
        video.currentTime = target;
        phase = "seeking";
        return;
      }
      if (phase === "seeking") {
        if (video.seeking || !ready(2)) { if (++seekTicks > SEEK_TIMEOUT_TICKS) phase = "align"; return; }
        const wait = target - pos;
        if (wait < -0.03) { lead = Math.min(1.5, lead * 2); phase = "align"; return; } // the voice got there first
        phase = "armed";
        arm = timers.set(() => {
          arm = null;
          if (phase !== "armed") return;
          phase = "synced";
          ctrl.reset();
          void Promise.resolve(video.play()).catch(() => { /* autoplay refused: the frame stays */ });
        }, Math.max(0, (wait / base) * 1000));
        return;
      }
      if (phase === "armed") return;
      if (phase === "over" || clipOver(video)) { // play() on an ended clip restarts it from frame 0: hold the last frame
        phase = "over";
        if (video.playbackRate !== 1) video.playbackRate = 1;
        return;
      }
      if (video.paused) { phase = "align"; return; } // something paused it (e.g. a background tab): line it up again
      const act = ctrl.update(video.currentTime, voice, t);
      if (act.kind === "seek") { video.pause(); phase = "align"; } // far off: rendezvous again rather than a visible jump
      else if (Math.abs(video.playbackRate - base * act.rate) > 1e-4) video.playbackRate = base * act.rate; // follows a speed change too
    },
  };
}
