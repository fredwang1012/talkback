/**
 * Replay of a finished reply (issue 67). Each reply's voice is kept in memory, bounded to the last MAX_REPLAYS:
 * either the per-sentence TTS clips (Gemini path; the lip-sync video is re-opened from the server cache) or the
 * Realtime PCM chunks. Replaying costs nothing. Only one reply plays at a time.
 */
import { AUDIO_RATE } from "../audio/constants";
import type { Clip, SentenceSpeaker } from "./sentencePlayer";

export const MAX_REPLAYS = 20;

export type ReplayItem =
  | { kind: "sentences"; parts: { text: string; clip: Clip }[] }
  | { kind: "pcm"; chunks: string[] };

export interface ReplayStore {
  addSentence(id: number, text: string, clip: Clip): void;
  addPcm(id: number, b64: string): void;
  get(id: number): ReplayItem | undefined;
  has(id: number): boolean;
  ids(): number[];
  clear(): void;
}

export function createReplayStore(max = MAX_REPLAYS, onChange?: () => void): ReplayStore {
  const items = new Map<number, ReplayItem>(); // insertion order = age
  const touch = (id: number, make: () => ReplayItem): ReplayItem => {
    let it = items.get(id);
    if (!it) {
      it = make();
      items.set(id, it);
      while (items.size > max) items.delete(items.keys().next().value as number);
    }
    return it;
  };
  return {
    addSentence(id, text, clip) {
      const it = touch(id, () => ({ kind: "sentences", parts: [] }));
      if (it.kind !== "sentences") return;
      const known = it.parts.find((p) => p.text === text);
      if (known) known.clip = clip; else it.parts.push({ text, clip });
      onChange?.();
    },
    addPcm(id, b64) {
      const it = touch(id, () => ({ kind: "pcm", chunks: [] }));
      if (it.kind === "pcm") it.chunks.push(b64);
      onChange?.();
    },
    get: (id) => items.get(id),
    has: (id) => items.has(id),
    ids: () => [...items.keys()],
    clear() { items.clear(); onChange?.(); },
  };
}

/** Base64 PCM16 mono chunks as one WAV file, so an <audio> element can replay a Realtime reply. */
export function pcmToWav(chunks: string[], rate = AUDIO_RATE): Uint8Array<ArrayBuffer> {
  const bytes = chunks.map((c) => { const s = atob(c); const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; });
  const len = bytes.reduce((n, b) => n + b.length, 0);
  const out = new Uint8Array(44 + len);
  const v = new DataView(out.buffer);
  const tag = (o: number, s: string) => { for (let i = 0; i < s.length; i++) out[o + i] = s.charCodeAt(i); };
  tag(0, "RIFF"); v.setUint32(4, 36 + len, true); tag(8, "WAVE"); tag(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, "data"); v.setUint32(40, len, true);
  let o = 44;
  for (const b of bytes) { out.set(b, o); o += b.length; }
  return out;
}

export type ReplayRun = { done: Promise<void>; stop: () => void };

/** Speak the parts in order. `stop` cancels for good: a stopped speaker must not go on to the next part. */
export function runSentences(parts: { text: string }[], speaker: SentenceSpeaker): ReplayRun {
  let cancelled = false;
  const done = (async () => {
    parts.slice(1).forEach((p) => speaker.prefetch(p.text));
    for (const p of parts) {
      if (cancelled) return;
      await speaker.play(p.text);
    }
  })();
  return { done, stop: () => { cancelled = true; speaker.stop(); } };
}

export interface ReplayPlayer {
  /** Plays reply `id`, or stops it when it is the one playing. Anything else playing stops first. */
  toggle(id: number): void;
  stop(): void;
  playing(): number | null;
}

export function createReplayPlayer(
  store: ReplayStore,
  run: (item: ReplayItem) => ReplayRun,
  onChange: (playing: number | null) => void,
  onError?: (e: unknown) => void,
): ReplayPlayer {
  let current: { id: number; run: ReplayRun } | null = null;
  const set = (c: typeof current) => { current = c; onChange(c ? c.id : null); };
  const stop = () => {
    const c = current;
    if (!c) return;
    set(null);
    c.run.stop();
  };
  return {
    stop,
    playing: () => current?.id ?? null,
    toggle(id) {
      if (current?.id === id) { stop(); return; }
      stop();
      const item = store.get(id);
      if (!item) return;
      let r: ReplayRun;
      try { r = run(item); } catch (e) { onError?.(e); return; }
      const mine = { id, run: r };
      set(mine);
      r.done.then(() => {}, (e) => onError?.(e)).then(() => { if (current === mine) set(null); });
    },
  };
}

/** Plays a Realtime reply's PCM through an <audio> element. */
export function runPcm(chunks: string[], make: (url: string) => HTMLAudioElement = (u) => new Audio(u)): ReplayRun {
  const url = URL.createObjectURL(new Blob([pcmToWav(chunks)], { type: "audio/wav" }));
  const audio = make(url);
  let finish: () => void = () => {};
  const done = new Promise<void>((resolve, reject) => {
    finish = resolve;
    audio.onended = () => resolve();
    audio.onerror = () => reject(new Error("Could not replay the reply."));
    Promise.resolve(audio.play()).catch(reject);
  }).finally(() => URL.revokeObjectURL(url));
  return { done, stop: () => { audio.onended = null; audio.pause(); finish(); } };
}
