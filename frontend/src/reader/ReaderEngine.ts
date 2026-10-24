import type { FlatChunk } from "./position";

export type WordTiming = { start: number; end: number; char_start: number; char_end: number };
export type TtsResult = { audio_url: string; timestamps: WordTiming[] | null };

/** The subset of HTMLAudioElement the engine uses, so tests can supply a fake. */
export interface AudioLike {
  src: string;
  currentTime: number;
  playbackRate: number;
  preservesPitch: boolean;
  preload: string;
  onended: (() => void) | null;
  play(): Promise<void>;
  pause(): void;
}

export type EngineState = "idle" | "loading" | "playing" | "paused" | "ended" | "error";
export type Snapshot = { state: EngineState; index: number; error: string | null };

export type EngineDeps = {
  fetchTts: (chunk: FlatChunk, voiceOverride?: string) => Promise<TtsResult>;
  createAudio: (url: string) => AudioLike;
};

type Loaded = { audio: AudioLike; timestamps: WordTiming[] | null };
type Entry = { promise: Promise<Loaded>; resolved: boolean };

const PREFETCH = 3;

/**
 * Plays a book chunk by chunk. One audio element per chunk (preservesPitch, so speed changes keep the voice),
 * the next PREFETCH chunks are fetched ahead, and a seek cancels everything outside the new window.
 */
export class ReaderEngine {
  private state: EngineState = "idle";
  private index = 0;
  private error: string | null = null;
  private speed = 1;
  private narratorOnlyVoice: string | null = null; // set when "full cast" is off
  private wantPlay = false;
  private gen = 0;
  private current: Loaded | null = null;
  private entries = new Map<number, Entry>();
  private listeners = new Set<(s: Snapshot) => void>();

  private chunks: FlatChunk[];
  private deps: EngineDeps;

  constructor(chunks: FlatChunk[], deps: EngineDeps, startIndex = 0) {
    this.chunks = chunks;
    this.deps = deps;
    this.index = Math.max(0, Math.min(startIndex, chunks.length - 1));
  }

  on(fn: (s: Snapshot) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  snapshot(): Snapshot {
    return { state: this.state, index: this.index, error: this.error };
  }

  private emit(): void {
    const s = this.snapshot();
    this.listeners.forEach((fn) => fn(s));
  }

  private set(state: EngineState, error: string | null = null): void {
    this.state = state;
    this.error = error;
    this.emit();
  }

  private entry(i: number): Entry {
    let e = this.entries.get(i);
    if (!e) {
      const chunk = this.chunks[i];
      const entry: Entry = { resolved: false, promise: null as unknown as Promise<Loaded> };
      entry.promise = this.deps.fetchTts(chunk, this.narratorOnlyVoice ?? undefined).then((r) => {
        const audio = this.deps.createAudio(r.audio_url);
        audio.preload = "auto";
        audio.preservesPitch = true;
        audio.playbackRate = this.speed;
        entry.resolved = true;
        return { audio, timestamps: r.timestamps };
      });
      entry.promise.catch(() => this.entries.delete(i)); // a failed prefetch is retried when its turn comes
      this.entries.set(i, entry);
      e = entry;
    }
    return e;
  }

  /** Fetch the window [current, current + PREFETCH] and drop everything outside it (stale after a seek). */
  private prefetchFrom(i: number): void {
    for (let n = i; n <= Math.min(i + PREFETCH, this.chunks.length - 1); n++) this.entry(n).promise.catch(() => {});
    for (const [k, e] of this.entries) {
      if (k < i || k > i + PREFETCH) {
        e.promise.then((l) => l.audio.pause()).catch(() => {});
        this.entries.delete(k);
      }
    }
  }

  private stopCurrent(): void {
    if (this.current) {
      this.current.audio.onended = null;
      this.current.audio.pause();
    }
    this.current = null;
  }

  private async startChunk(i: number): Promise<void> {
    const gen = ++this.gen;
    const entry = this.entry(i);
    if (!entry.resolved && this.state !== "playing") this.set("loading");
    else this.emit();
    try {
      const loaded = await entry.promise;
      if (gen !== this.gen) return; // a seek happened while loading
      if (!this.wantPlay) {
        this.current = loaded;
        this.set("paused");
        return;
      }
      this.current = loaded;
      loaded.audio.currentTime = 0;
      loaded.audio.playbackRate = this.speed;
      loaded.audio.onended = () => this.handleEnded(gen);
      await loaded.audio.play();
      if (gen !== this.gen) return;
      this.set("playing");
      this.prefetchFrom(i);
    } catch (e) {
      if (gen !== this.gen) return;
      this.wantPlay = false;
      this.set("error", e instanceof Error ? e.message : String(e));
    }
  }

  private handleEnded(gen: number): void {
    if (gen !== this.gen) return;
    if (this.index >= this.chunks.length - 1) {
      this.wantPlay = false;
      this.stopCurrent();
      this.set("ended");
      return;
    }
    this.index += 1;
    void this.startChunk(this.index);
  }

  play(): void {
    if (this.state === "playing" || this.chunks.length === 0) return;
    this.wantPlay = true;
    if (this.state === "paused" && this.current) {
      void this.current.audio.play().then(() => this.set("playing"), (e) => this.set("error", String(e)));
      return;
    }
    if (this.state === "ended") this.index = 0;
    void this.startChunk(this.index);
  }

  pause(): void {
    this.wantPlay = false;
    if (this.state === "playing") {
      this.current?.audio.pause();
      this.set("paused");
    } else if (this.state === "loading") {
      this.set("paused");
    }
  }

  toggle(): void {
    if (this.wantPlay) this.pause();
    else this.play();
  }

  seek(i: number): void {
    const next = Math.max(0, Math.min(i, this.chunks.length - 1));
    this.gen++; // invalidate in-flight loads and onended callbacks
    this.stopCurrent();
    this.index = next;
    this.prefetchFrom(next);
    if (this.wantPlay) void this.startChunk(next);
    else this.set(this.state === "paused" ? "paused" : "idle"); // not playing: just move the position
  }

  next(): void { this.seek(this.index + 1); }
  prev(): void { this.seek(this.index - 1); }

  setSpeed(speed: number): void {
    this.speed = speed;
    if (this.current) this.current.audio.playbackRate = speed;
    for (const e of this.entries.values()) e.promise.then((l) => { l.audio.playbackRate = speed; }).catch(() => {});
  }

  /** Full cast off: read everything in the narrator's voice. Applies from the next chunk on. */
  setNarratorOnly(voice: string | null): void {
    if (voice === this.narratorOnlyVoice) return;
    this.narratorOnlyVoice = voice;
    for (const [k, e] of this.entries) {
      if (k > this.index) { e.promise.then((l) => l.audio.pause()).catch(() => {}); this.entries.delete(k); }
    }
    if (this.state === "playing") this.prefetchFrom(this.index);
  }

  /** Voices changed (cast picker): forget chunks fetched ahead so they are requested again with the new voice. */
  refreshAhead(): void {
    for (const [k, e] of this.entries) {
      if (k > this.index) { e.promise.then((l) => l.audio.pause()).catch(() => {}); this.entries.delete(k); }
    }
    if (this.state === "playing") this.prefetchFrom(this.index);
  }

  /** The word being spoken now, as offsets into the chunk's text; null when timestamps are unavailable. */
  activeWord(): { charStart: number; charEnd: number } | null {
    const cur = this.current;
    if (!cur?.timestamps || this.state !== "playing") return null;
    const t = cur.audio.currentTime;
    let hit: WordTiming | null = null;
    for (const w of cur.timestamps) {
      if (w.start <= t) hit = w;
      else break;
    }
    return hit ? { charStart: hit.char_start, charEnd: hit.char_end } : null;
  }

  dispose(): void {
    this.wantPlay = false;
    this.gen++;
    this.stopCurrent();
    for (const e of this.entries.values()) e.promise.then((l) => l.audio.pause()).catch(() => {});
    this.entries.clear();
    this.listeners.clear();
  }
}
