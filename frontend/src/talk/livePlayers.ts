/**
 * The two ways an answer reaches a live face (#140), with the interfaces the talk code already uses:
 * - `LivePlayer` (PlayerLike) for Realtime direct audio: each PCM delta goes to the avatar; `endTurn` ends the reply.
 * - `createLiveSpeaker` (SentenceSpeaker) for Gemini and text-mode answers: each sentence's /api/speak clip is decoded
 *   to 24 kHz PCM and spoken by the avatar as one segment; `play` resolves when the avatar reports it played it.
 * The avatar's own audio track is what the reader hears, so nothing plays locally and voice and lips stay in step.
 */
import type { PlayerLike } from "./TalkClient";
import type { SentenceSpeaker } from "./sentencePlayer";
import { b64ToBytes, clipToPcm, type LiveFace } from "./liveFace";

export class LivePlayer implements PlayerLike {
  private face: LiveFace;

  constructor(face: LiveFace, onSpeaking: (speaking: boolean) => void) {
    this.face = face;
    face.onPlaying = onSpeaking;
  }
  async resume(): Promise<void> {}
  enqueueBase64(b64: string): void { this.face.write(b64ToBytes(b64)); }
  /** The reply's audio is complete: the avatar plays the rest and reports when it is done. */
  endTurn(): void { void this.face.endSegment(); }
  async flush(): Promise<number> {
    const ms = this.face.playedMs();
    this.face.clear();
    return ms;
  }
  playedMs(): number { return this.face.playedMs(); }
  reset(): void {}
  async close(): Promise<void> {}
}

export function createLiveSpeaker(clipUrl: (text: string) => Promise<string>, face: LiveFace,
  hooks: { onPlaying?: (on: boolean) => void; decode?: (url: string) => Promise<Uint8Array> } = {}): SentenceSpeaker {
  const decode = hooks.decode ?? clipToPcm;
  const ready = new Map<string, Promise<Uint8Array>>();
  let epoch = 0;
  face.onPlaying = (on) => hooks.onPlaying?.(on);
  const get = (text: string) => {
    let p = ready.get(text);
    if (!p) { p = clipUrl(text).then(decode); ready.set(text, p); }
    return p;
  };
  return {
    prefetch(text: string) { void get(text).catch(() => {}); },
    async play(text: string) {
      const mine = epoch;
      const pcm = await get(text);
      ready.delete(text);
      if (mine !== epoch || !face.alive) return;
      face.write(pcm);
      await face.endSegment();
    },
    stop() {
      epoch++;
      ready.clear();
      face.clear();
    },
  };
}
