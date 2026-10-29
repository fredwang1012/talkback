/**
 * The two ways an answer reaches a live face (#140), with the interfaces the talk code already uses:
 * - `LivePlayer` (PlayerLike) for Realtime direct audio: each PCM delta goes to the avatar; `endTurn` ends the reply.
 * - `createLiveSpeaker` (SentenceSpeaker) for Gemini and text-mode answers: each sentence's /api/speak clip is decoded
 *   to 24 kHz PCM and spoken by the avatar as one segment; `play` resolves when the avatar reports it played it.
 * The avatar's own audio track is what the reader hears, so nothing plays locally and voice and lips stay in step.
 *
 * `RoutedPlayer` and `createRoutedSpeaker` pick, per reply (per sentence for the speaker), between the live face and the
 * usual local path, so the talk session is never rebuilt when a live face starts or ends (a rebuild drops a question
 * asked meanwhile). Whoever is speaking through the face binds its `onPlaying` at that moment.
 */
import type { PlayerLike } from "./TalkClient";
import type { SentenceSpeaker } from "./sentencePlayer";
import { b64ToBytes, clipToPcm, type LiveFace } from "./liveFace";

export class LivePlayer implements PlayerLike {
  private face: LiveFace;
  private onSpeaking: (speaking: boolean) => void;

  constructor(face: LiveFace, onSpeaking: (speaking: boolean) => void) {
    this.face = face;
    this.onSpeaking = onSpeaking;
  }
  /** This player speaks through the face from now on (a reply starts). */
  bind(): void { this.face.onPlaying = this.onSpeaking; }
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
  reset(): void { this.bind(); }
  async close(): Promise<void> {}
}

export function createLiveSpeaker(clipUrl: (text: string) => Promise<string>, face: LiveFace,
  hooks: { onPlaying?: (on: boolean) => void; decode?: (url: string) => Promise<Uint8Array> } = {}): SentenceSpeaker {
  const decode = hooks.decode ?? clipToPcm;
  const ready = new Map<string, Promise<Uint8Array>>();
  let epoch = 0;
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
      face.onPlaying = (on) => hooks.onPlaying?.(on);
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

/** A PlayerLike that sends each reply to the live face when one is running at the reply's start, else to `local`. */
export class RoutedPlayer implements PlayerLike {
  private getLive: () => LiveFace | null;
  private local: PlayerLike;
  private onSpeaking: (speaking: boolean) => void;
  private lives = new WeakMap<LiveFace, LivePlayer>();
  private cur: PlayerLike;

  constructor(getLive: () => LiveFace | null, local: PlayerLike, onSpeaking: (speaking: boolean) => void) {
    this.getLive = getLive;
    this.local = local;
    this.onSpeaking = onSpeaking;
    this.cur = this.pick();
  }
  private pick(): PlayerLike {
    const face = this.getLive();
    if (!face || !face.alive) return this.local;
    let p = this.lives.get(face);
    if (!p) { p = new LivePlayer(face, this.onSpeaking); this.lives.set(face, p); }
    return p;
  }
  /** Which way this reply goes (tests, the debug drawer). */
  get live(): boolean { return this.cur !== this.local; }
  /** The avatar protocol has no pause: a live reply cannot be held. */
  get canPause(): boolean { return !this.live; }
  resume(): Promise<void> { return this.local.resume(); } // inside the user gesture, whichever way replies go
  enqueueBase64(b64: string): void { this.cur.enqueueBase64(b64); }
  endTurn(): void { this.cur.endTurn?.(); }
  flush(): Promise<number> { return this.cur.flush(); }
  playedMs(): number { return this.cur.playedMs?.() ?? 0; }
  pausePlayback(): Promise<void> { return this.cur.pausePlayback?.() ?? Promise.resolve(); }
  resumePlayback(): Promise<void> { return this.cur.resumePlayback?.() ?? Promise.resolve(); }
  /** A new reply: decide its route now (a face that started or ended meanwhile takes effect here). */
  reset(): void {
    this.local.reset();
    this.cur = this.pick();
    this.cur.reset();
  }
  close(): Promise<void> { return this.local.close(); }
}

/**
 * A SentenceSpeaker that speaks each sentence through the live face when one is running, else through `local`.
 * Pause / resume only exist on the local path (the avatar protocol has no pause), so `canPause` tells the UI.
 */
export function createRoutedSpeaker(getLive: () => LiveFace | null, makeLive: (face: LiveFace) => SentenceSpeaker,
  local: SentenceSpeaker): SentenceSpeaker & { readonly canPause: boolean } {
  const lives = new WeakMap<LiveFace, SentenceSpeaker>();
  const route = (): SentenceSpeaker => {
    const face = getLive();
    if (!face || !face.alive) return local;
    let s = lives.get(face);
    if (!s) { s = makeLive(face); lives.set(face, s); }
    return s;
  };
  let last: SentenceSpeaker = local;
  return {
    prefetch(text: string, rank?: number) { route().prefetch(text, rank); },
    play(text: string) { last = route(); return last.play(text); },
    stop() {
      local.stop();
      if (last !== local) last.stop();
      const face = getLive();
      if (face) lives.get(face)?.stop();
    },
    pause() { if (last === local) local.pause?.(); },
    resume() { return last === local ? (local.resume?.() ?? false) : false; },
    get paused() { return last === local ? !!local.paused : false; },
    get canPause() { return last === local && route() === local; },
  };
}
