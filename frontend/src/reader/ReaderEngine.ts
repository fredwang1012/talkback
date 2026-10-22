import { isAutoplayBlock, TAP_TO_PLAY } from "../audio/mediaErrors";
import { prefetchEnd } from "./prefetch";
import type { FlatChunk } from "./position";
import { resumeTime, staleIndexes } from "./voiceSwitch";

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
  load?(): void;
}

export type EngineState = "idle" | "loading" | "playing" | "paused" | "ended" | "error";
export type Snapshot = { state: EngineState; index: number; error: string | null };

export type EngineDeps = {
  fetchTts: (chunk: FlatChunk, voiceOverride?: string) => Promise<TtsResult>;
  createAudio: (url: string) => AudioLike;
};

type Loaded = { audio: AudioLike; timestamps: WordTiming[] | null };
type Entry = { promise: Promise<Loaded>; resolved: boolean; voice: string | undefined };

/**
 * Plays a book chunk by chunk. One audio element per chunk (preservesPitch, so speed changes keep the voice),
 * the next few chunks (see prefetch.ts) are fetched ahead, across paragraph and section boundaries, and a seek cancels everything outside the new window.
 */
export class ReaderEngine {
  private state: EngineState = "idle";
  private index = 0;
  private error: string | null = null;
  private speed = 1;
  private narratorOnlyVoice: string | null = null; // set when "full cast" is off
  private voiceResolver: ((chunk: FlatChunk) => string | undefined) | null = null; // local cast: voice per chunk
  private currentVoice: string | undefined;
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
      const entry: Entry = { resolved: false, voice: this.voiceOf(i), promise: null as unknown as Promise<Loaded> };
      entry.promise = this.deps.fetchTts(chunk, entry.voice).then((r) => {
        const audio = this.deps.createAudio(r.audio_url);
        audio.preload = "auto";
        audio.load?.(); // start downloading the audio now so the chunk is buffered when its turn comes
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

  /** Fetch the window [current, prefetchEnd] and drop everything outside it (stale after a seek). */
  private prefetchFrom(i: number): void {
    const end = prefetchEnd(this.chunks, i);
    for (let n = i; n <= end; n++) this.entry(n).promise.catch(() => {});
    for (const [k, e] of this.entries) {
      if (k < i || k > end) {
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

  /** The voice chunk i should be read in right now (undefined: the server picks from the saved cast). */
  private voiceOf(i: number): string | undefined {
    return this.narratorOnlyVoice ?? this.voiceResolver?.(this.chunks[i]) ?? undefined;
  }

  private async startChunk(i: number, resume = false): Promise<void> {
    const gen = ++this.gen;
    const entry = this.entry(i);
    this.prefetchFrom(i); // request the lookahead now, in parallel with the first chunk, not after it starts
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
      // a voice switch keeps the old voice going until the new one is ready, then continues at the same word
      const old = this.current !== loaded ? this.current : null;
      const at = resume && old ? resumeTime(old.timestamps, old.audio.currentTime, loaded.timestamps) : 0;
      if (old) this.stopCurrent();
      this.current = loaded;
      this.currentVoice = entry.voice;
      loaded.audio.currentTime = at;
      loaded.audio.playbackRate = this.speed;
      loaded.audio.onended = () => this.handleEnded(gen);
      await loaded.audio.play();
      if (gen !== this.gen) return;
      this.set("playing");
      this.prefetchFrom(i);
    } catch (e) {
      if (gen !== this.gen) return;
      this.wantPlay = false;
      if (isAutoplayBlock(e)) { this.set("paused", TAP_TO_PLAY); return; } // autoplay policy: wait for a tap, not an error
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
      void this.current.audio.play().then(() => this.set("playing"), (e) => {
        this.wantPlay = false;
        if (isAutoplayBlock(e)) this.set("paused", TAP_TO_PLAY);
        else this.set("error", e instanceof Error ? e.message : String(e));
      });
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

  /** Full cast off: read everything in the narrator's voice. Applies at once, mid-chunk if playing. */
  setNarratorOnly(voice: string | null): void {
    if (voice === this.narratorOnlyVoice) return;
    this.narratorOnlyVoice = voice;
    this.applyVoices();
  }

  /** Local cast: the voice each chunk should have, applied before the cast is saved. */
  setVoiceResolver(fn: ((chunk: FlatChunk) => string | undefined) | null): void {
    this.voiceResolver = fn;
    this.applyVoices();
  }

  /** Voices changed: refetch only the chunks whose voice differs; restart the current one in the new voice. */
  refreshAhead(): void { this.applyVoices(); }

  private applyVoices(): void {
    const stale = staleIndexes([...this.entries].map(([k, e]) => [k, e.voice] as [number, string | undefined]), (i) => this.voiceOf(i));
    const curStale = this.current !== null && this.currentVoice !== this.voiceOf(this.index);
    for (const k of stale) {
      const e = this.entries.get(k);
      if (!e) continue;
      this.entries.delete(k);
      if (k !== this.index) e.promise.then((l) => l.audio.pause()).catch(() => {});
    }
    if (this.wantPlay && (this.state === "playing" || this.state === "loading")) {
      if (curStale || stale.includes(this.index)) void this.startChunk(this.index, true); // old voice plays until the new one is ready
      this.prefetchFrom(this.index);
    } else if (curStale) {
      this.stopCurrent(); // paused: playing again starts the chunk over in the new voice
    }
  }

  /** The audio element of the chunk that is current (playing or paused), for level metering. */
  currentAudio(): AudioLike | null {
    return this.current?.audio ?? null;
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
