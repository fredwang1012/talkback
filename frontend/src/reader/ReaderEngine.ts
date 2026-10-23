import { isAutoplayBlock, TAP_TO_PLAY } from "../audio/mediaErrors";
import type { ClipSync, VideoClip } from "../talk/sentencePlayer";
import { gateVoiceOnClip, type ClipGate } from "../talk/startGate";
import { rateFor, type SpeedTag } from "./naturalSpeed";
import { prefetchEnd } from "./prefetch";
import { nextChunk, type FlatChunk } from "./position";
import { clipAheadFor, clipWindow, READ_CHAIN_WAIT_MS, READ_START_WAIT_MS, speechSeconds } from "./readLipSync";
import { resumeTime, staleIndexes } from "./voiceSwitch";

export type WordTiming = { start: number; end: number; char_start: number; char_end: number };
export type TtsResult = { audio_url: string; timestamps: WordTiming[] | null; voice?: string };

/** The subset of HTMLAudioElement the engine uses, so tests can supply a fake. */
export interface AudioLike {
  src: string;
  currentTime: number;
  playbackRate: number;
  preservesPitch: boolean;
  preload: string;
  duration?: number;
  onended: (() => void) | null;
  play(): Promise<void>;
  pause(): void;
  load?(): void;
  addEventListener?(type: string, fn: () => void): void;
  removeEventListener?(type: string, fn: () => void): void;
}

export type EngineState = "idle" | "loading" | "playing" | "paused" | "ended" | "error";
export type Snapshot = { state: EngineState; index: number; error: string | null };

/**
 * Read-mode lip-sync (#80): opens the clip of a chunk given its cached TTS audio and the voice it was read in.
 * Null when the chunk cannot be lip-synced (its reader's face is not bound to a voice); the talk loop then shows.
 */
export type ClipSource = (chunk: FlatChunk, audioUrl: string, voice: string | undefined) => Promise<VideoClip> | null;

export type EngineDeps = {
  fetchTts: (chunk: FlatChunk, voiceOverride?: string, speed?: SpeedTag | null) => Promise<TtsResult>;
  createAudio: (url: string) => AudioLike;
  sync?: ClipSync;        // the read-mode face's lip-sync video (talk/faceSync); clips are only opened with one
  startWaitMs?: number;   // the first word after Play waits this long at most for its clip
  chainWaitMs?: number;   // between chunks (the next clip is normally prefetched)
  clipAhead?: number;     // clips opened ahead of the playing chunk (default: by speech length, readLipSync.clipAheadFor)
};

type Loaded = { audio: AudioLike; timestamps: WordTiming[] | null; url: string; voice: string | undefined };
type Entry = {
  promise: Promise<Loaded>; resolved: boolean; voice: string | undefined; loaded?: Loaded;
  speed: SpeedTag | null; // natural speed (#78): the Boson speed tag the audio was voiced with (null: plain audio)
  video?: Promise<VideoClip | null>; // the chunk's lip-sync clip, once opened (null: none, or the render failed)
  clipJoined?: boolean;              // the clip was attached to the face: starting this chunk over needs a fresh one
};

/**
 * Plays a book chunk by chunk. One audio element per chunk (preservesPitch, so speed changes keep the voice),
 * the next few chunks (see prefetch.ts) are fetched ahead, across paragraph and section boundaries, and a seek cancels everything outside the new window.
 *
 * Lip-sync (#80): with a `sync` and a clip source, each chunk's clip is opened with its audio and played with the
 * Talkback start gate (talk/startGate): the first word waits (bounded) until the clip can start with it from 0; the
 * voice is never paused or moved for the face. The next chunk's clip opens the moment the current voice starts
 * (more ahead when chunks are short: readLipSync.clipAheadFor), so continuous reading has no render wait between chunks; a seek, skip or voice change
 * drops the clips that are no longer next.
 */
export class ReaderEngine {
  private state: EngineState = "idle";
  private index = 0;
  private error: string | null = null;
  private speed = 1;
  private speedTag: SpeedTag | null = null; // natural speed (#78): new fetches carry this tag and play at 1.0x
  private narratorOnlyVoice: string | null = null; // set when "full cast" is off
  private voiceResolver: ((chunk: FlatChunk) => string | undefined) | null = null; // local cast: voice per chunk
  private currentVoice: string | undefined;
  private wantPlay = false;
  private gen = 0;
  private current: Loaded | null = null;
  private entries = new Map<number, Entry>();
  private listeners = new Set<(s: Snapshot) => void>();
  private clipSource: ClipSource | null = null;
  private gate: ClipGate | null = null;

  private chunks: FlatChunk[];
  private deps: EngineDeps;
  private skipCaptions = false;

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
      const entry: Entry = { resolved: false, voice: this.voiceOf(i), speed: this.speedTag, promise: null as unknown as Promise<Loaded> };
      entry.promise = (entry.speed ? this.deps.fetchTts(chunk, entry.voice, entry.speed) : this.deps.fetchTts(chunk, entry.voice)).then((r) => {
        const audio = this.deps.createAudio(r.audio_url);
        audio.preload = "auto";
        audio.load?.(); // start downloading the audio now so the chunk is buffered when its turn comes
        audio.preservesPitch = true;
        audio.playbackRate = rateFor(this.speed, entry.speed);
        entry.resolved = true;
        entry.loaded = { audio, timestamps: r.timestamps, url: r.audio_url, voice: entry.voice ?? r.voice };
        return entry.loaded;
      });
      entry.promise.catch(() => this.entries.delete(i)); // a failed prefetch is retried when its turn comes
      this.entries.set(i, entry);
      e = entry;
    }
    return e;
  }

  /** Fetch the window [current, prefetchEnd] and drop everything outside it (stale after a seek). */
  /** Figure captions (#40) are read by default; skipped, continuous reading passes over them (never fetched ahead). */
  setSkipCaptions(skip: boolean): void { this.skipCaptions = skip; }

  private prefetchFrom(i: number): void {
    const end = prefetchEnd(this.chunks, i);
    for (let n = i; n <= end; n++) if (n === i || !(this.skipCaptions && this.chunks[n].caption)) this.entry(n).promise.catch(() => {});
    for (const [k, e] of this.entries) {
      if (k < i || k > end) this.dropEntry(k, e);
    }
  }

  private dropEntry(k: number, e: Entry): void {
    this.entries.delete(k);
    e.promise.then((l) => l.audio.pause()).catch(() => {});
    this.dropClip(e);
  }

  // ---------- lip-sync clips ----------

  /** Read-mode lip-sync on, off, or with another face/voice mapping: every held clip is stale and dropped. With a
   * source, the chunk playing right now gets a clip that joins late (by rendezvous) and the next one is prefetched. */
  setClipSource(src: ClipSource | null): void {
    if (src === this.clipSource) return;
    this.cancelGate();
    this.deps.sync?.leave();
    for (const e of this.entries.values()) this.dropClip(e);
    this.clipSource = src;
    if (src && this.current && this.state === "playing") {
      this.joinLate(this.index, this.gen);
      this.holdClips(this.index, this.clipAheadAt(this.index));
    }
  }

  /** Clips held ahead of chunk i: a fixed count when given, else enough that the next clip has a head start of a few
   * seconds of speech (short chunks back to back open more ahead, #104). */
  private clipAheadAt(i: number): number {
    if (this.deps.clipAhead !== undefined) return this.deps.clipAhead;
    return clipAheadFor(i, this.chunks.length - 1, (k) => this.speechAt(k));
  }

  /** Seconds chunk k takes to speak at the current speed: its audio's duration once known, else from its text. */
  private speechAt(k: number): number {
    const d = this.entries.get(k)?.loaded?.audio.duration;
    const s = typeof d === "number" && Number.isFinite(d) && d > 0 ? d : speechSeconds(this.chunks[k]?.tts_text ?? "");
    const rate = rateFor(this.speed, this.entries.get(k)?.speed ?? this.speedTag);
    return s / (rate > 0 ? rate : 1);
  }

  /** The chunk's clip, opened once its audio is known. `fresh`: the chunk starts over, so a clip that was already
   * attached to the face (a MediaSource plays only once) is replaced. */
  private openClip(i: number, fresh = false): Promise<VideoClip | null> | null {
    const src = this.clipSource;
    const e = this.entries.get(i);
    if (!src || !this.deps.sync || !e) return null;
    if (e.video && (!fresh || !e.clipJoined)) return e.video;
    this.dropClip(e);
    const chunk = this.chunks[i];
    e.video = e.promise.then((l) => {
      const p = src(chunk, l.url, l.voice);
      return p ? p.catch(() => null) : null; // render refused or failed: the talk loop stands in
    }, () => null);
    return e.video;
  }

  /** Hold clips only for [i, i + ahead] (see readLipSync.clipWindow): release the rest and, with `open`, open those. */
  private holdClips(i: number, ahead: number, open = true): void {
    const [a, b] = clipWindow(i, ahead, this.chunks.length - 1);
    for (const [k, e] of this.entries) if (k < a || k > b) this.dropClip(e);
    if (!open) return;
    for (let n = a; n <= b; n++) void this.openClip(n);
  }

  private dropClip(e: Entry): void {
    const v = e.video;
    if (!v) return;
    e.video = undefined;
    e.clipJoined = false;
    void v.then((c) => c?.release()); // also a stream still opening: it is released when it arrives
  }

  /** The clip of the chunk playing now joins the face late (rendezvous when it has rendered ahead of the voice). */
  private joinLate(i: number, gen: number): void {
    const sync = this.deps.sync;
    const cur = this.current;
    const video = this.openClip(i, true);
    const e = this.entries.get(i);
    if (!sync || !cur || !video || !e) return;
    e.clipJoined = true;
    void video.then((v) => {
      if (gen !== this.gen || this.current !== cur || this.entries.get(i)?.video !== video || !v) return;
      void sync.join(cur.audio, v, true);
    });
  }

  private cancelGate(): void {
    this.gate?.cancel();
    this.gate = null;
  }

  // ---------- playback ----------

  private stopCurrent(leaveFace = true): void {
    if (this.current) {
      this.current.audio.onended = null;
      this.current.audio.pause();
    }
    this.current = null;
    if (leaveFace) this.deps.sync?.leave();
  }

  /** The voice chunk i should be read in right now (undefined: the server picks from the saved cast). */
  private voiceOf(i: number): string | undefined {
    return this.narratorOnlyVoice ?? this.voiceResolver?.(this.chunks[i]) ?? undefined;
  }

  /** `resume`: a voice switch mid-chunk (continue at the same word). `chained`: the previous chunk just ended. */
  private async startChunk(i: number, { resume = false, chained = false } = {}): Promise<void> {
    const gen = ++this.gen;
    this.cancelGate();
    const entry = this.entry(i);
    this.prefetchFrom(i); // request the lookahead now, in parallel with the first chunk, not after it starts
    this.holdClips(i, this.clipAheadAt(i), false);
    this.openClip(i, true); // this chunk's clip renders while its audio loads; the next one's waits for the voice
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
      const sync = this.deps.sync;
      const video = at === 0 ? this.openClip(i, true) : null;
      if (sync && video) {
        // start gate: the first word waits (bounded) until its clip can start with it from 0; a chunk that follows
        // the previous one has its clip prefetched and waits less
        entry.clipJoined = true;
        const gate = gateVoiceOnClip(loaded.audio, video, sync,
          chained ? this.deps.chainWaitMs ?? READ_CHAIN_WAIT_MS : this.deps.startWaitMs ?? READ_START_WAIT_MS);
        this.gate = gate;
        if (!chained) this.set("loading");
        await gate.ready;
        if (gen !== this.gen) return;
        this.gate = null;
        if (!this.wantPlay) { // paused while waiting: the clip stays joined and starts with the voice on play
          this.current = loaded;
          this.currentVoice = entry.voice;
          loaded.audio.onended = () => this.handleEnded(gen);
          this.set("paused");
          return;
        }
      } else sync?.leave(); // no clip for this chunk (or it joins late below): the talk loop
      if (old) this.stopCurrent(false);
      this.current = loaded;
      this.currentVoice = entry.voice;
      loaded.audio.currentTime = at;
      loaded.audio.playbackRate = rateFor(this.speed, entry.speed);
      loaded.audio.onended = () => this.handleEnded(gen);
      await loaded.audio.play();
      if (gen !== this.gen) return;
      if (at > 0) this.joinLate(i, gen); // resumed mid-chunk (voice switch): the clip joins when it can
      this.set("playing");
      this.prefetchFrom(i);
      this.holdClips(i, this.clipAheadAt(i)); // the next chunk's clip renders while this one plays
    } catch (e) {
      if (gen !== this.gen) return;
      this.wantPlay = false;
      if (isAutoplayBlock(e)) { this.set("paused", TAP_TO_PLAY); return; } // autoplay policy: wait for a tap, not an error
      this.set("error", e instanceof Error ? e.message : String(e));
    }
  }

  private handleEnded(gen: number): void {
    if (gen !== this.gen) return;
    const next = nextChunk(this.chunks, this.index, this.skipCaptions);
    if (next < 0) {
      this.wantPlay = false;
      this.stopCurrent();
      this.set("ended");
      return;
    }
    this.deps.sync?.leave(); // the finished chunk's clip comes down now, not when the next clip joins (#104)
    this.index = next;
    void this.startChunk(this.index, { chained: true });
  }

  play(): void {
    if (this.state === "playing" || this.chunks.length === 0) return;
    this.wantPlay = true;
    if (this.state === "paused" && this.current) {
      void this.current.audio.play().then(() => { this.set("playing"); this.holdClips(this.index, this.clipAheadAt(this.index)); }, (e) => {
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
    this.cancelGate();
    this.stopCurrent();
    this.index = next;
    this.prefetchFrom(next);
    this.holdClips(next, this.clipAheadAt(next), false); // clips that are no longer next are cancelled; none opens until play
    if (this.wantPlay) void this.startChunk(next);
    else this.set(this.state === "paused" ? "paused" : "idle"); // not playing: just move the position
  }

  next(): void { this.seek(this.index + 1); }
  prev(): void { this.seek(this.index - 1); }

  /** `tag`: natural speed (#78), the Boson speed tag for this speed (null: plain audio at playbackRate). Audio voiced
   * with another tag is stale: only the window [current, prefetchEnd] is fetched again, never the whole book, and the
   * chunk playing now finishes in the voice it has (at the rate that matches it). */
  setSpeed(speed: number, tag: SpeedTag | null = null): void {
    const changed = tag !== this.speedTag;
    this.speed = speed;
    this.speedTag = tag;
    if (changed) {
      for (const [k, e] of [...this.entries]) {
        if (e.speed === tag) continue;
        if (e.loaded && e.loaded === this.current) continue; // keeps playing; its clip stays with it
        this.dropEntry(k, e);
      }
    }
    if (this.current) {
      const cur = [...this.entries.values()].find((e) => e.loaded === this.current);
      this.current.audio.playbackRate = rateFor(speed, cur ? cur.speed : tag);
    }
    for (const e of this.entries.values()) e.promise.then((l) => { l.audio.playbackRate = rateFor(speed, e.speed); }).catch(() => {});
    if (changed && this.wantPlay && (this.state === "playing" || this.state === "loading")) this.prefetchFrom(this.index);
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
      this.dropClip(e); // a clip in the old voice is stale too
      if (k !== this.index) e.promise.then((l) => l.audio.pause()).catch(() => {});
    }
    if (this.wantPlay && (this.state === "playing" || this.state === "loading")) {
      if (curStale || stale.includes(this.index)) void this.startChunk(this.index, { resume: true }); // old voice plays until the new one is ready
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
    this.cancelGate();
    this.stopCurrent();
    for (const e of this.entries.values()) { e.promise.then((l) => l.audio.pause()).catch(() => {}); this.dropClip(e); }
    this.entries.clear();
    this.listeners.clear();
  }
}
