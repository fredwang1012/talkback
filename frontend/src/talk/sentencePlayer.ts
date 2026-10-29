/**
 * Speaks answer sentences one clip at a time. `prefetch` starts synthesis early (the controller asks for the
 * next few sentences while the current one plays) and preloads the audio so `play` starts instantly; `play`
 * resolves when the clip ends; `stop` halts it (barge-in).
 *
 * Lip-sync start gate (#66, talk/startGate): when synthesis also offers a lip-synced video (`openVideo`) and a face
 * can show it, the voice waits for its face before the first word, at most `startWaitMs`, and both start together
 * from 0. A rank-0 prefetch opens the next sentence's stream early, so usually only the first sentence of an answer
 * waits. Once the voice plays, nothing waits for the face again. `hooks.onPlaying` reports whether sound is actually
 * coming out, which is what the face follows.
 *
 * Pause / resume (#90, #88): `pause` pauses the playing clip where it is (the lip-sync face freezes with it: faceSync
 * follows the voice's pause/playing events) and holds back any sentence that has not started yet; `resume` plays the
 * same clip on from that moment. `stop` ends a paused sentence like a playing one.
 */

import type { TimingEvent } from "./speechGate";
import { gateVoiceOnClip, type ClipGate } from "./startGate";

export interface SentenceSpeaker {
  /** Synthesise ahead. `rank` 0 is the very next sentence: its lip-sync stream is opened too, later ranks only get audio. */
  prefetch(text: string, rank?: number): void;
  play(text: string): Promise<void>;
  stop(): void;
  /** Hold the voice at this moment (a sentence not started yet waits too). Optional: test doubles may lack it. */
  pause?(): void;
  /** Continue from the moment it was paused. False when there is nothing to continue (not paused, or stopped since). */
  resume?(): boolean;
  readonly paused?: boolean;
  /** False while `pause` would not hold the voice (a live face, #140). Absent = it can. */
  readonly canPause?: boolean;
}

export interface AudioLike {
  onended: (() => void) | null;
  onerror?: (() => void) | null;
  play(): Promise<void> | void;
  pause(): void;
  currentTime?: number;
  duration?: number;
  playbackRate?: number; // the voice's speed (read mode); the lip-sync clip plays at the same rate
  addEventListener?(type: string, fn: () => void): void;
  removeEventListener?(type: string, fn: () => void): void;
}

/** A lip-synced video that is ready to show: `release` frees the stream and its object URL. */
export type VideoClip = { url: string; release: () => void };

/** `lipsync` (#80): the voice and face its video is made of, so a finished answer can be downloaded as one MP4. */
export type Clip = { audio_url: string; openVideo?: () => Promise<VideoClip>; lipsync?: { voice: string; faceId: string } };

/**
 * Keeps the lip-sync video locked to the voice (see faceSync.ts); it never pauses or moves the voice. `join` before the
 * voice starts returns a promise that resolves when the voice may start (`late`: the voice already plays). `leave`
 * detaches; the speaker releases the clip.
 */
export interface ClipSync {
  join(audio: AudioLike, clip: VideoClip, late?: boolean): Promise<void> | void;
  leave(): void;
  /** False when there is no face to show a clip on: the voice then never waits for one. */
  readonly canShow?: boolean;
}

/** The longest the first word waits for its lip-synced face; past that the talk loop starts with the voice. */
export const START_WAIT_MS = 6000;

export interface SpeakerHooks {
  sync?: ClipSync;
  onPlaying?: (on: boolean) => void;
  onTiming?: (e: TimingEvent) => void;
  now?: () => number;
  startWaitMs?: number;
}

type Entry = {
  clip: Promise<Clip>;
  audio?: AudioLike;
  video?: Promise<VideoClip | null>; // resolves once the video's first fragment is in (null: no clip)
};

export function createSentenceSpeaker(
  speak: (text: string) => Promise<Clip>,
  createAudio: (url: string) => AudioLike = (url) => {
    const a = new Audio(url);
    a.preload = "auto";
    return a as unknown as AudioLike;
  },
  hooks: SpeakerHooks = {},
): SentenceSpeaker {
  const now = hooks.now ?? (() => Date.now());
  const pending = new Map<string, Entry>();
  let current: { audio: AudioLike; settle: () => void; started: () => boolean } | null = null;
  let epoch = 0; // bumped by stop(), so a clip that was still synthesising never starts
  let paused = false;
  let held: (() => void) | null = null; // a voice start that waits for resume

  const emit = (stage: TimingEvent["stage"], since: number, text: string) =>
    hooks.onTiming?.({ stage, ms: Math.max(0, now() - since), sentence: text.slice(0, 40) });

  const openVideo = (entry: Entry, text: string) => {
    if (entry.video) return;
    entry.video = entry.clip.then(async (c) => {
      if (!c.openVideo) return null;
      const t0 = now();
      try {
        const v = await c.openVideo();
        emit("avatar-first-fragment", t0, text);
        return v;
      } catch { return null; } // render failed: the talk loop stands in
    }, () => null);
  };

  const synth = (text: string): Entry => {
    const known = pending.get(text);
    if (known) return known;
    const t0 = now();
    const my = epoch;
    const entry: Entry = { clip: speak(text) };
    entry.clip.then((c) => {
      emit("tts", t0, text);
      if (my === epoch && !entry.audio) entry.audio = createAudio(c.audio_url); // preload while the previous sentence plays
    }, () => {}); // a prefetch nobody plays must not be an unhandled rejection
    pending.set(text, entry);
    return entry;
  };

  /** Free what a sentence holds: its (possibly still opening) video stream and its preloaded audio. */
  const discard = async (entry: Entry) => {
    if (entry.audio && current?.audio !== entry.audio) entry.audio.pause();
    (await entry.video)?.release();
  };

  return {
    prefetch(text, rank = 0) {
      const entry = synth(text);
      if (rank === 0) openVideo(entry, text);
    },
    async play(text) {
      const my = epoch;
      const asked = now();
      const entry = synth(text);
      let res: Clip;
      try { res = await entry.clip; } finally { pending.delete(text); }
      if (my !== epoch) { void discard(entry); return; }
      openVideo(entry, text);
      const audio = entry.audio ?? (entry.audio = createAudio(res.audio_url));
      await new Promise<void>((resolve, reject) => {
        let over = false;
        let started = false;
        const listeners: [string, () => void][] = [];
        const on = (type: string, fn: () => void) => { audio.addEventListener?.(type, fn); listeners.push([type, fn]); };
        let gate: ClipGate | null = null;
        const teardown = () => {
          over = true;
          gate?.cancel();
          if (current?.audio === audio) current = null;
          for (const [t, f] of listeners) audio.removeEventListener?.(t, f);
          hooks.sync?.leave();
          hooks.onPlaying?.(false);
          void discard(entry);
        };
        const settle = () => { teardown(); resolve(); };
        const fail = (e: Error) => { teardown(); reject(e); };
        let voiceAsked = false;
        current = { audio, settle, started: () => voiceAsked };
        const playing = () => {
          if (over) return;
          if (!started) { started = true; emit("audio-start", asked, text); }
          hooks.onPlaying?.(true);
        };
        const quiet = () => { if (!over) hooks.onPlaying?.(false); };
        audio.onended = settle;
        audio.onerror = () => fail(new Error("Could not play the answer audio."));
        on("playing", playing);
        on("pause", quiet);
        on("waiting", quiet);
        const startVoice = () => {
          if (over || voiceAsked) return;
          if (paused) { held = startVoice; return; } // resume starts it
          voiceAsked = true;
          Promise.resolve(audio.play()).then(
            () => { if (!audio.addEventListener) playing(); }, // test doubles without media events
            (e) => fail(e instanceof Error ? e : new Error(String(e))),
          );
        };
        const sync = hooks.sync;
        if (!sync || !res.openVideo || sync.canShow === false || !entry.video) { startVoice(); return; }
        // start gate (talk/startGate): the voice waits (bounded) until its clip can start with it from 0
        const waitFrom = now();
        gate = gateVoiceOnClip(audio, entry.video, sync, hooks.startWaitMs ?? START_WAIT_MS);
        void gate.ready.then(() => { if (!voiceAsked && !over) { emit("face-wait", waitFrom, text); startVoice(); } });
      });
    },
    get paused() { return paused; },
    pause() {
      if (paused) return;
      paused = true;
      if (current?.started()) current.audio.pause();
    },
    resume() {
      if (!paused) return false;
      paused = false;
      const h = held;
      held = null;
      if (h) h();
      else if (current?.started()) {
        const a = current.audio;
        Promise.resolve(a.play()).catch(() => a.onerror?.());
      }
      return true;
    },
    stop() {
      epoch++;
      paused = false;
      held = null;
      for (const e of pending.values()) void discard(e);
      pending.clear();
      const c = current;
      current = null;
      if (c) { c.audio.onended = null; c.audio.onerror = null; c.audio.pause(); c.settle(); }
    },
  };
}
