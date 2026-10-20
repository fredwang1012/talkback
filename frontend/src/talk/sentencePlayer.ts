/**
 * Speaks answer sentences one clip at a time. `prefetch` starts synthesis early (the controller asks for the
 * next sentence while the current one plays); `play` resolves when the clip ends; `stop` halts it (barge-in).
 * When synthesis also returns a lip-synced `video_url`, `onClip` gets it as the audio starts and null when it ends,
 * so the face can play that video (muted) in step with the sound.
 */

export interface SentenceSpeaker {
  prefetch(text: string): void;
  play(text: string): Promise<void>;
  stop(): void;
}

export interface AudioLike {
  onended: (() => void) | null;
  onerror: (() => void) | null;
  play(): Promise<void> | void;
  pause(): void;
}

type Clip = { audio_url: string; video_url?: string | null; release?: () => void }; // release frees a streamed video

export function createSentenceSpeaker(
  speak: (text: string) => Promise<Clip>,
  createAudio: (url: string) => AudioLike = (url) => new Audio(url) as unknown as AudioLike,
  onClip: (videoUrl: string | null) => void = () => {},
): SentenceSpeaker {
  const pending = new Map<string, Promise<Clip>>();
  let current: { audio: AudioLike; settle: () => void } | null = null;
  let epoch = 0; // bumped by stop(), so a clip that was still synthesising never starts

  const synth = (text: string) => {
    let p = pending.get(text);
    if (!p) {
      p = speak(text);
      p.catch(() => {}); // a prefetch nobody plays must not be an unhandled rejection
      pending.set(text, p);
    }
    return p;
  };

  return {
    prefetch(text) { synth(text); },
    async play(text) {
      const my = epoch;
      const p = synth(text);
      let res: Clip;
      try { res = await p; } finally { pending.delete(text); }
      if (my !== epoch) { res.release?.(); return; }
      await new Promise<void>((resolve, reject) => {
        const audio = createAudio(res.audio_url);
        const settle = () => { if (current?.audio === audio) current = null; onClip(null); res.release?.(); resolve(); };
        const fail = (e: Error) => { if (current?.audio === audio) current = null; onClip(null); res.release?.(); reject(e); };
        current = { audio, settle };
        audio.onended = settle;
        audio.onerror = () => fail(new Error("Could not play the answer audio."));
        onClip(res.video_url ?? null);
        Promise.resolve(audio.play()).catch((e) => fail(e instanceof Error ? e : new Error(String(e))));
      });
    },
    stop() {
      epoch++;
      for (const p of pending.values()) p.then((r) => r.release?.(), () => {}); // prefetched streams nobody will play
      pending.clear();
      const c = current;
      current = null;
      if (c) { c.audio.onended = null; c.audio.onerror = null; c.audio.pause(); c.settle(); }
    },
  };
}
