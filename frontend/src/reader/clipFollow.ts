import { AudioClock, DriftController } from "../talk/driftController";

/** The bits of an <audio> / <video> element the follower needs (so it can be tested with fakes). */
export interface FollowAudio { currentTime: number; paused?: boolean; ended?: boolean; }
export interface FollowVideo {
  currentTime: number; playbackRate: number; paused: boolean; ended?: boolean; duration?: number;
  play(): Promise<void> | void; pause(): void;
}

/** True only while the voice is really being heard: not paused, not ended, and past the very start. */
export const audioSounding = (a: FollowAudio | null | undefined): boolean =>
  !!a && !a.paused && !a.ended && a.currentTime > 0;

/**
 * Keeps a pre-rendered (muted) lip-sync clip locked to the chunk's audio in read mode. Stateless polling: call `tick`
 * every ~100 ms. The clip starts together with the voice, pauses/ends with it, and drift is corrected by nudging
 * playbackRate (seek only on a big, rare drift) - the same logic as the Talkback face (talk/driftController).
 */
/** The clip has played to its end (it can be a little shorter than its audio): hold the last frame. */
const pastEnd = (v: FollowVideo, at: number): boolean =>
  typeof v.duration === "number" && Number.isFinite(v.duration) && at >= v.duration - 0.05;
const clipOver = (v: FollowVideo): boolean => !!v.ended || pastEnd(v, v.currentTime);

export function createClipFollower(video: FollowVideo, now: () => number = () => performance.now(), startOffsetS = 0) {
  const ctrl = new DriftController({ startOffsetS });
  let clock = new AudioClock();
  let lastAudio: FollowAudio | null = null;
  let wasSounding = false;

  return {
    tick(audio: FollowAudio | null | undefined): void {
      const t = now();
      if (audio !== lastAudio) { lastAudio = audio ?? null; clock = new AudioClock(); ctrl.reset(); wasSounding = false; }
      const sounding = audioSounding(audio);
      if (!sounding || !audio) {
        if (!video.paused) video.pause();
        if (wasSounding) { clock.pause(t); wasSounding = false; }
        if (audio?.ended) video.playbackRate = 1;
        return;
      }
      if (!wasSounding) { // the voice (re)started: line the clip up and start both together
        wasSounding = true;
        clock.anchor(audio.currentTime, t);
        const target = audio.currentTime + ctrl.opts.startOffsetS;
        video.playbackRate = 1;
        ctrl.reset();
        if (pastEnd(video, target)) {
          video.currentTime = video.duration!; // the voice is past the end of the clip: show its last frame
          return;
        }
        video.currentTime = target;
        void Promise.resolve(video.play()).catch(() => { /* autoplay refused: the portrait stays */ });
        return;
      }
      if (clipOver(video)) { // play() on an ended clip restarts it from frame 0: hold the last frame instead
        if (video.playbackRate !== 1) video.playbackRate = 1;
        return;
      }
      if (video.paused) { void Promise.resolve(video.play()).catch(() => {}); return; }
      const act = ctrl.update(video.currentTime, clock.sample(audio.currentTime, t), t);
      if (act.kind === "seek") {
        if (!pastEnd(video, act.to)) video.currentTime = act.to; // never seek past the end (that ends it again)
      }
      else if (act.kind === "rate") video.playbackRate = act.rate;
    },
  };
}
