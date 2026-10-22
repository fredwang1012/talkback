/**
 * Speaks answer sentences one clip at a time. `prefetch` starts synthesis early (the controller asks for the
 * next few sentences while the current one plays) and preloads the audio so `play` starts instantly; `play`
 * resolves when the clip ends; `stop` halts it (barge-in).
 *
 * The voice never waits for the face. When synthesis also offers a lip-synced video (`openVideo`), the stream is
 * opened in the background and handed to `hooks.sync` once its first fragment has arrived; until then (and if it
 * fails) the face shows its talk loop. `hooks.onPlaying` reports whether sound is actually coming out, which is
 * what the face follows.
 */

import type { TimingEvent } from "./speechGate";

export interface SentenceSpeaker {
  /** Synthesise ahead. `rank` 0 is the very next sentence: its lip-sync stream is opened too, later ranks only get audio. */
  prefetch(text: string, rank?: number): void;
  play(text: string): Promise<void>;
  stop(): void;
}

export interface AudioLike {
  onended: (() => void) | null;
  onerror: (() => void) | null;
  play(): Promise<void> | void;
  pause(): void;
  currentTime?: number;
  addEventListener?(type: string, fn: () => void): void;
  removeEventListener?(type: string, fn: () => void): void;
}

/** A lip-synced video that is ready to show: `release` frees the stream and its object URL. */
export type VideoClip = { url: string; release: () => void };

export type Clip = { audio_url: string; openVideo?: () => Promise<VideoClip> };

/** Keeps the lip-sync video locked to the voice (see faceSync.ts). `leave` detaches; the speaker releases the clip. */
export interface ClipSync {
  join(audio: AudioLike, clip: VideoClip): void;
  leave(): void;
  /** True while the sync itself has paused the voice to let the video catch up (not a real pause). */
  readonly holding: boolean;
}

export interface SpeakerHooks {
  sync?: ClipSync;
  onPlaying?: (on: boolean) => void;
  onTiming?: (e: TimingEvent) => void;
  now?: () => number;
}

type Entry = {
  clip: Promise<Clip>;
  audio?: AudioLike;
  video?: Promise<VideoClip | null>;
  ready?: VideoClip; // set once the video's first fragment is in
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
  let current: { audio: AudioLike; settle: () => void } | null = null;
  let epoch = 0; // bumped by stop(), so a clip that was still synthesising never starts

  const emit = (stage: TimingEvent["stage"], since: number, text: string) =>
    hooks.onTiming?.({ stage, ms: Math.max(0, now() - since), sentence: text.slice(0, 40) });

  const openVideo = (entry: Entry, text: string) => {
    if (entry.video) return;
    entry.video = entry.clip.then(async (c) => {
      if (!c.openVideo) return null;
      const t0 = now();
      try {
        const v = await c.openVideo();
        entry.ready = v;
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
        const teardown = () => {
          over = true;
          if (current?.audio === audio) current = null;
          for (const [t, f] of listeners) audio.removeEventListener?.(t, f);
          hooks.sync?.leave();
          hooks.onPlaying?.(false);
          void discard(entry);
        };
        const settle = () => { teardown(); resolve(); };
        const fail = (e: Error) => { teardown(); reject(e); };
        current = { audio, settle };
        const playing = () => {
          if (over) return;
          if (!started) { started = true; emit("audio-start", asked, text); }
          hooks.onPlaying?.(true);
        };
        const quiet = () => { if (!over && !hooks.sync?.holding) hooks.onPlaying?.(false); };
        audio.onended = settle;
        audio.onerror = () => fail(new Error("Could not play the answer audio."));
        on("playing", playing);
        on("pause", quiet);
        on("waiting", quiet);
        // the video joins whenever its first fragment is in (a late one seeks to the voice); the voice never waits
        const attach = (v: VideoClip | null) => { if (v && !over && hooks.sync) hooks.sync.join(audio, v); };
        if (entry.ready) attach(entry.ready);
        else void entry.video?.then(attach);
        Promise.resolve(audio.play()).then(
          () => { if (!audio.addEventListener) playing(); }, // test doubles without media events
          (e) => fail(e instanceof Error ? e : new Error(String(e))),
        );
      });
    },
    stop() {
      epoch++;
      for (const e of pending.values()) void discard(e);
      pending.clear();
      const c = current;
      current = null;
      if (c) { c.audio.onended = null; c.audio.onerror = null; c.audio.pause(); c.settle(); }
    },
  };
}
