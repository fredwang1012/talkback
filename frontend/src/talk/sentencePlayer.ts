/**
 * Speaks answer sentences one clip at a time. `prefetch` starts synthesis early (the controller asks for the
 * next sentence while the current one plays); `play` resolves when the clip ends; `stop` halts it (barge-in).
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

export function createSentenceSpeaker(
  speak: (text: string) => Promise<{ audio_url: string }>,
  createAudio: (url: string) => AudioLike = (url) => new Audio(url) as unknown as AudioLike,
): SentenceSpeaker {
  const pending = new Map<string, Promise<{ audio_url: string }>>();
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
      let res: { audio_url: string };
      try { res = await p; } finally { pending.delete(text); }
      if (my !== epoch) return;
      await new Promise<void>((resolve, reject) => {
        const audio = createAudio(res.audio_url);
        const settle = () => { if (current?.audio === audio) current = null; resolve(); };
        current = { audio, settle };
        audio.onended = settle;
        audio.onerror = () => { if (current?.audio === audio) current = null; reject(new Error("Could not play the answer audio.")); };
        Promise.resolve(audio.play()).catch((e) => { if (current?.audio === audio) current = null; reject(e instanceof Error ? e : new Error(String(e))); });
      });
    },
    stop() {
      epoch++;
      pending.clear();
      const c = current;
      current = null;
      if (c) { c.audio.onended = null; c.audio.onerror = null; c.audio.pause(); c.settle(); }
    },
  };
}
