/**
 * Fake-clock media elements that behave like Chrome where it matters for lip-sync (driven by vi fake timers).
 *
 * FakeStreamVideo models a <video> fed by Media Source Extensions from Boson's fMP4 stream:
 *  - fragments arrive over wall time; `buffered` is [0, end of the fragments that have arrived] once attached;
 *  - the init segment has no duration, so `duration` is Infinity until the stream ends and `seekable` is
 *    [0, buffered end]: a seek past the buffered end is CLAMPED to it (HTML seek algorithm step 8, Chromium
 *    MediaSource::SeekableInternal);
 *  - a seek drops readyState to HAVE_METADATA, fires seeking (+ waiting if it was playing), then seeked after seekMs;
 *  - playback stops at the buffered end with `waiting` and resumes with `playing` once more data arrives.
 * FakeVoice models the answer's HTMLAudioElement: `playing` a little after play(), and it records every pause and
 * every backwards jump of currentTime (what the user hears as a stutter / rewind).
 * Events are queued and dispatched after the state update of each step, like media element tasks.
 */

export type Clock = () => number;

/** Boson's measured fragment layout: the first fragment is 0.7927 s of video, then 0.75 s (18 frames at 24 fps). */
export function bosonFragmentEnds(clipS: number): number[] {
  const ends: number[] = [];
  let e = 0.7927;
  while (e < clipS - 1e-6) { ends.push(e); e += 0.75; }
  ends.push(clipS);
  return ends;
}

class Queue extends EventTarget {
  private pending: string[] = [];
  protected queue(type: string) { this.pending.push(type); }
  protected flush() { const p = this.pending; this.pending = []; for (const t of p) this.dispatchEvent(new Event(t)); }
}

type Stream = { arrivals: { end: number; at: number }[]; end: number };

export class FakeStreamVideo extends Queue {
  private _src = "";
  playbackRate = 1;
  paused = true;
  seeking = false;
  readyState = 0;
  openMs = 30;
  seekMs = 60;
  seeks: { asked: number; landed: number }[] = [];
  private pos = 0;
  private arrivals: { end: number; at: number }[] = [];
  private streamEnd = Infinity; // media end once the whole stream is known
  private attachedAt: number | null = null;
  private loaded = false;
  private seekDoneAt = 0;
  private streams = new Map<string, Stream>(); // per-URL streams (read mode: several clips, prefetched ahead)
  private now: Clock;

  constructor(now: Clock) { super(); this.now = now; }

  /** Attaching a URL that `addStream` registered plays that clip's stream (its fragments arrive on their own clock). */
  get src() { return this._src; }
  set src(u: string) {
    this._src = u;
    const s = this.streams.get(u);
    if (s) { this.arrivals = s.arrivals; this.streamEnd = s.end; }
  }

  private makeStream(clipS: number, firstMs: number, rtf: number, fragMs: number): Stream {
    const t0 = this.now();
    const ends = bosonFragmentEnds(clipS);
    return { arrivals: ends.map((end, k) => ({ end, at: t0 + firstMs + k * fragMs * rtf })), end: clipS };
  }

  /** The stream starts now: fragment k arrives at firstMs + k * fragMs * rtf (rtf > 1: slower than real time). */
  startStream(clipS: number, firstMs: number, rtf = 1, fragMs = 750) {
    const s = this.makeStream(clipS, firstMs, rtf, fragMs);
    this.arrivals = s.arrivals;
    this.streamEnd = s.end;
  }

  /** A clip's stream starts now and is played when `url` is attached (see `src`). */
  addStream(url: string, clipS: number, firstMs: number, rtf = 1, fragMs = 750) {
    this.streams.set(url, this.makeStream(clipS, firstMs, rtf, fragMs));
  }

  private arrivedEnd(): number {
    const t = this.now();
    let e = 0;
    for (const a of this.arrivals) if (a.at <= t) e = Math.max(e, a.end);
    return e;
  }
  private get end(): number { return this.loaded ? this.arrivedEnd() : 0; }
  private get complete(): boolean { return this.loaded && this.arrivals.length > 0 && this.arrivals.every((a) => a.at <= this.now()); }

  get duration(): number { return this.complete ? this.streamEnd : this.loaded ? Infinity : NaN; }
  get buffered() { const e = this.end; return { length: e > 0 ? 1 : 0, end: () => e }; }
  get currentTime() { return this.pos; }
  set currentTime(x: number) {
    const landed = Math.max(0, Math.min(x, this.end)); // seekable = [0, buffered end]
    this.seeks.push({ asked: x, landed });
    const wasPlaying = !this.paused && this.readyState >= 3;
    this.pos = landed;
    this.seeking = true;
    this.seekDoneAt = this.now() + this.seekMs;
    this.readyState = Math.min(this.readyState, 1);
    this.queue("seeking");
    if (wasPlaying) this.queue("waiting");
  }

  load() { this.attachedAt = this.now(); this.loaded = false; this.readyState = 0; this.pos = 0; }
  removeAttribute(n: string) { if (n === "src") { this._src = ""; this.attachedAt = null; this.loaded = false; this.readyState = 0; this.pos = 0; } }
  play() {
    if (!this.paused) return Promise.resolve();
    this.paused = false;
    this.queue("play");
    if (this.readyState >= 3 && !this.seeking) this.queue("playing");
    else this.queue("waiting");
    return Promise.resolve();
  }
  pause() { if (!this.paused) { this.paused = true; this.queue("pause"); } }

  /** Advance by dtMs of wall time (call after the fake clock moved). */
  advance(dtMs: number) {
    const t = this.now();
    if (this.src && this.attachedAt !== null && !this.loaded && t >= this.attachedAt + this.openMs && this.arrivedEnd() > 0) {
      this.loaded = true;
      this.readyState = this.end - this.pos > 0.04 ? 4 : 2;
      this.queue("loadedmetadata"); this.queue("loadeddata");
    }
    if (!this.loaded) { this.flush(); return; }
    const ahead = () => this.end - this.pos;
    if (this.seeking) {
      if (t >= this.seekDoneAt) {
        this.seeking = false;
        this.readyState = ahead() > 0.04 ? 4 : 2;
        this.queue("seeked");
        if (!this.paused && this.readyState >= 3) this.queue("playing");
      }
    } else if (!this.paused) {
      if (this.readyState < 3 && ahead() > 0.04) { this.readyState = 4; this.queue("playing"); }
      else if (this.readyState >= 3) {
        this.pos += (dtMs / 1000) * this.playbackRate;
        if (this.pos >= this.end) {
          this.pos = this.end;
          if (this.complete) { this.paused = true; this.queue("ended"); }
          else { this.readyState = 2; this.queue("waiting"); }
        }
      }
    }
    this.flush();
  }
}

export class FakeVoice extends Queue {
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  paused = true;
  duration: number;
  startMs = 20;
  pauses = 0;
  backJumps: number[] = [];
  startedAt: number | null = null; // wall time of the first sound
  private pos = 0;
  private soundAt: number | null = null;
  private now: Clock;

  constructor(now: Clock, duration: number) { super(); this.now = now; this.duration = duration; }

  get currentTime() { return this.pos; }
  set currentTime(x: number) { if (x < this.pos - 0.001) this.backJumps.push(this.pos - x); this.pos = x; }
  play() {
    if (this.paused) { this.paused = false; this.soundAt = this.now() + this.startMs; }
    return Promise.resolve();
  }
  pause() { if (!this.paused) { this.paused = true; this.pauses++; this.soundAt = null; this.queue("pause"); } }
  get sounding() { return !this.paused && this.soundAt !== null && this.now() >= this.soundAt; }

  advance(dtMs: number) {
    if (!this.paused && this.soundAt !== null) {
      const t = this.now();
      if (t >= this.soundAt) {
        if (t - dtMs < this.soundAt) { this.queue("playing"); if (this.startedAt === null) this.startedAt = this.soundAt; }
        else this.pos += dtMs / 1000;
        if (this.pos >= this.duration) { this.pos = this.duration; this.paused = true; this.flush(); this.onended?.(); return; }
      }
    }
    this.flush();
  }
}

/** A complete, progressive MP4 (read mode's pre-rendered clip): loads after loadMs, seeks take seekMs. */
export class FakeFileVideo {
  playbackRate = 1;
  paused = true;
  seeking = false;
  readyState = 0;
  duration: number;
  plays = 0;
  private pos = 0;
  private pendingSeek: number | null = null;
  private loadAt: number;
  private seekDoneAt = 0;
  private now: Clock;
  seekMs = 60;

  constructor(now: Clock, duration: number, loadMs: number) { this.now = now; this.duration = duration; this.loadAt = now() + loadMs; }
  get ended() { return this.pos >= this.duration; }
  get currentTime() { return this.pos; }
  set currentTime(x: number) {
    const v = Math.max(0, Math.min(x, this.duration));
    if (this.readyState === 0) { this.pendingSeek = v; this.pos = v; return; } // default playback start position
    this.pos = v; this.seeking = true; this.readyState = 1; this.seekDoneAt = this.now() + this.seekMs;
  }
  play() { if (this.paused) { this.paused = false; this.plays++; } return Promise.resolve(); }
  pause() { this.paused = true; }
  advance(dtMs: number) {
    const t = this.now();
    if (this.readyState === 0) {
      if (t < this.loadAt) return;
      this.readyState = 1;
      if (this.pendingSeek !== null && this.pendingSeek > 0) { this.seeking = true; this.seekDoneAt = t + this.seekMs; }
      else this.readyState = 4;
      return;
    }
    if (this.seeking) { if (t >= this.seekDoneAt) { this.seeking = false; this.readyState = 4; } return; }
    if (!this.paused && this.readyState >= 3) {
      this.pos = Math.min(this.duration, this.pos + (dtMs / 1000) * this.playbackRate);
      if (this.pos >= this.duration) this.paused = true;
    }
  }
}
