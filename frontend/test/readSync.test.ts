/**
 * Issue #80: real lip-sync in read mode. The ReaderEngine streams a Boson clip per TTS chunk and plays it with the
 * same start gate as Talkback (#66): chunk audio and clip start together from 0, the voice is never paused or
 * rewound, the next chunk's clip is prefetched while the current one plays, and a stalled clip drops to the talk
 * loop. Runs the real engine + FaceSync against Chrome-like fake media on a fake clock.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";
import { FaceSync } from "../src/talk/faceSync";
import { READ_CHAIN_WAIT_MS } from "../src/reader/readLipSync";
import { FakeStreamVideo, FakeVoice } from "./fakeMedia";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

const now = () => Date.now();

class FakeChunkAudio extends FakeVoice implements AudioLike {
  playbackRate = 1;
  preservesPitch = false;
  preload = "";
  constructor(public src: string, duration: number) { super(now, duration); }
  load() {}
}

const mkChunks = (n: number, speaker = (_i: number) => "narrator"): FlatChunk[] =>
  Array.from({ length: n }, (_, i) => ({
    id: `c${i}`, speaker: speaker(i), voice: "v1", tts_text: `t${i}`.padEnd(120, "."), char_start: 0, char_end: 10, para: i,
  }));

type Opts = {
  chunks?: number; voiceS?: number | ((i: number) => number); clipS?: number; firstMs?: number; rtf?: number;
  canLipSync?: (chunk: FlatChunk) => boolean; startWaitMs?: number; chainWaitMs?: number;
};

function setup(o: Opts = {}) {
  const voiceOf = (i: number) => typeof o.voiceS === "function" ? o.voiceS(i) : o.voiceS ?? 8.4;
  const clipOf = (i: number) => o.clipS ?? (typeof o.voiceS === "function" ? voiceOf(i) + 0.64 : 9.04);
  const firstMs = o.firstMs ?? 1300, rtf = o.rtf ?? 1;
  const audios = new Map<string, FakeChunkAudio>();
  const video = new FakeStreamVideo(now);
  let shown = false;
  const sync = new FaceSync((s) => { shown = s; }, { now });
  sync.setVideo(video);
  const opens: { id: string; at: number }[] = [];
  const releases: string[] = [];
  const t0 = now();
  const engine = new ReaderEngine(mkChunks(o.chunks ?? 3), {
    fetchTts: async (chunk): Promise<TtsResult> => ({ audio_url: `/media/tts/${chunk.id}.mp3`, timestamps: null, voice: "v1" }),
    createAudio: (url) => { const a = new FakeChunkAudio(url, voiceOf(Number(url.match(/c(\d+)\.mp3/)![1]))); audios.set(url, a); return a; },
    sync, startWaitMs: o.startWaitMs ?? 6000, chainWaitMs: o.chainWaitMs ?? 1000,
  });
  engine.setClipSource((chunk) => {
    if (o.canLipSync && !o.canLipSync(chunk)) return null;
    opens.push({ id: chunk.id, at: now() - t0 });
    const url = `blob:${chunk.id}-${opens.length}`;
    video.addStream(url, clipOf(Number(chunk.id.slice(1))), firstMs, rtf);
    return new Promise((res) => setTimeout(() => res({ url, release: () => releases.push(chunk.id) }), firstMs));
  });
  const audioOf = (id: string) => audios.get(`/media/tts/${id}.mp3`);
  const current = () => engine.currentAudio() as FakeChunkAudio | null;
  const samples: { t: number; index: number; shown: boolean; drift: number; sounding: boolean }[] = [];
  const drive = async (ms: number) => {
    for (let i = 0; i < ms / 10; i++) {
      await vi.advanceTimersByTimeAsync(10);
      video.advance(10);
      for (const a of audios.values()) a.advance(10);
      const cur = current();
      samples.push({ t: now() - t0, index: engine.snapshot().index, shown, drift: cur ? Math.abs(video.currentTime - cur.currentTime) : 0, sounding: !!cur?.sounding });
    }
  };
  const stats = (index?: number) => {
    const s = index === undefined ? samples : samples.filter((x) => x.index === index);
    const sound = s.filter((x) => x.sounding);
    return { soundMs: sound.length * 10, shownMs: sound.filter((x) => x.shown).length * 10, maxDrift: Math.max(0, ...s.filter((x) => x.shown).map((x) => x.drift)) };
  };
  /** Ms from the chunk's first sound to its face showing (null: never shown while it sounded). */
  const faceLagMs = (index: number) => {
    const sound = samples.filter((x) => x.index === index && x.sounding);
    const k = sound.findIndex((x) => x.shown);
    return k < 0 ? null : k * 10;
  };
  /** The longest stretch (ms) the lip-sync face stayed up while no voice was sounding. */
  const longestSilentShownMs = () => {
    let run = 0, max = 0;
    for (const x of samples) { run = x.shown && !x.sounding ? run + 10 : 0; max = Math.max(max, run); }
    return max;
  };
  return { engine, video, sync, audios, audioOf, opens, releases, drive, stats, faceLagMs, longestSilentShownMs, t0, isShown: () => shown };
}

describe("read-mode lip-sync (#80)", () => {
  it("start together: the first word waits for its clip, then voice and face start from 0 and stay in step", async () => {
    const r = setup({ chunks: 1 });
    r.engine.play();
    await r.drive(200);
    expect(r.engine.snapshot().state).toBe("loading"); // waiting at the gate
    expect(r.audioOf("c0")?.startedAt).toBeNull();
    await r.drive(12000);
    const voice = r.audioOf("c0")!;
    expect(voice.pauses).toBe(0);
    expect(voice.backJumps).toEqual([]);
    expect(voice.startedAt! - r.t0).toBeLessThan(1300 + 750 * 2 + 300); // bounded pre-roll: two fragments
    const s = r.stats(0);
    expect(s.shownMs).toBeGreaterThan(0.9 * s.soundMs); // the lip-synced face is up from the first word
    expect(s.maxDrift).toBeLessThan(0.1);
    expect(r.video.seeks.every((x) => x.landed === x.asked)).toBe(true);
    expect(r.engine.snapshot().state).toBe("ended");
  });

  it("prefetch hides the gap: the next clip opens when the voice starts, so the next chunk starts at once, in step", async () => {
    const r = setup({ chunks: 2 });
    r.engine.play();
    await r.drive(22000);
    const a0 = r.audioOf("c0")!, a1 = r.audioOf("c1")!;
    expect(r.opens.map((o) => o.id)).toEqual(["c0", "c1"]);
    expect(r.opens[1].at).toBeGreaterThanOrEqual(a0.startedAt! - r.t0 - 50); // not before the first chunk's voice
    expect(r.opens[1].at).toBeLessThan(a0.startedAt! - r.t0 + 200);
    const gap = a1.startedAt! - (a0.startedAt! + a0.duration * 1000);
    expect(gap).toBeLessThan(150); // no 2 s render wait between chunks
    expect(a1.pauses).toBe(0);
    expect(a1.backJumps).toEqual([]);
    const s1 = r.stats(1);
    expect(s1.shownMs).toBeGreaterThan(0.9 * s1.soundMs);
    expect(s1.maxDrift).toBeLessThan(0.1);
    expect(r.releases).toContain("c0"); // the played clip is freed
  });

  it("cancel on seek: clips outside the new window are released, only the new chunk and one ahead are opened", async () => {
    const r = setup({ chunks: 8 });
    r.engine.play();
    await r.drive(3000); // c0 plays, c1 is rendering ahead
    expect(r.opens.map((o) => o.id)).toEqual(["c0", "c1"]);
    r.engine.seek(5);
    await r.drive(4000);
    expect(r.releases).toEqual(expect.arrayContaining(["c0", "c1"]));
    expect(r.opens.map((o) => o.id)).toEqual(["c0", "c1", "c5", "c6"]);
    const a5 = r.audioOf("c5")!;
    expect(a5.startedAt).not.toBeNull();
    expect(a5.pauses).toBe(0);
    expect(a5.backJumps).toEqual([]);
    const s5 = r.stats(5);
    expect(s5.shownMs).toBeGreaterThan(0.9 * s5.soundMs); // the new chunk starts with its own clip
    expect(s5.maxDrift).toBeLessThan(0.1);
  });

  it("stall fallback: a render slower than real time hides the face (talk loop) and leaves the voice alone", async () => {
    const r = setup({ chunks: 1, voiceS: 12, clipS: 12.8, rtf: 1.3 });
    r.engine.play();
    await r.drive(16000);
    const voice = r.audioOf("c0")!;
    expect(voice.pauses).toBe(0);
    expect(voice.backJumps).toEqual([]);
    const s = r.stats(0);
    expect(s.shownMs).toBeGreaterThan(0);
    expect(s.shownMs).toBeLessThan(s.soundMs); // it fell behind at some point and was hidden
    expect(s.maxDrift).toBeLessThan(0.1); // whenever the face is visible it is in step
  });

  it("a chunk whose reader cannot lip-sync starts at once with no face; the next lip-syncable chunk gets its clip", async () => {
    const r = setup({ chunks: 2, canLipSync: (c) => c.id !== "c0" });
    r.engine.play();
    await r.drive(300);
    expect(r.audioOf("c0")?.startedAt).not.toBeNull(); // no wait at all
    expect(r.isShown()).toBe(false);
    await r.drive(20000);
    expect(r.opens.map((o) => o.id)).toEqual(["c1"]);
    const s1 = r.stats(1);
    expect(s1.shownMs).toBeGreaterThan(0.9 * s1.soundMs);
  });

  it("dropping the clip source mid-read (speed off 1x) releases every clip and hides the face; the voice carries on", async () => {
    const r = setup({ chunks: 2 });
    r.engine.play();
    await r.drive(4000);
    expect(r.isShown()).toBe(true);
    r.engine.setClipSource(null);
    await r.drive(50);
    expect(r.isShown()).toBe(false);
    expect(r.audioOf("c0")!.pauses).toBe(0);
    await r.drive(1500); // c1's open (in flight at the switch) resolves and is released too
    expect(r.releases).toEqual(expect.arrayContaining(["c0", "c1"]));
    expect(r.opens.length).toBe(2); // nothing new is opened
  });

  it("pausing while the first word waits at the gate keeps it paused; play then starts voice and face together", async () => {
    const r = setup({ chunks: 1 });
    r.engine.play();
    await r.drive(500);
    r.engine.pause();
    await r.drive(4000);
    expect(r.engine.snapshot().state).toBe("paused");
    expect(r.audioOf("c0")?.startedAt).toBeNull();
    r.engine.play();
    await r.drive(10000);
    const voice = r.audioOf("c0")!;
    expect(voice.pauses).toBe(0);
    expect(voice.backJumps).toEqual([]);
    const s = r.stats(0);
    expect(s.shownMs).toBeGreaterThan(0.9 * s.soundMs);
    expect(s.maxDrift).toBeLessThan(0.1);
  });
});

describe("short chunks back to back (#104)", () => {
  it("when a chunk's voice ends its face goes down at once, not frozen on the last frame until the next clip", async () => {
    const r = setup({ chunks: 2, voiceS: (i) => [1, 3][i], firstMs: 1800 });
    r.engine.play();
    await r.drive(15000);
    expect(r.engine.snapshot().state).toBe("ended");
    expect(r.longestSilentShownMs()).toBeLessThanOrEqual(60); // was ~1 s: the old clip stayed up between chunks
  });

  it("each short chunk's voice starts with its own face from 0, not before it", async () => {
    const lens = [4, 0.9, 0.8, 1.1, 0.7, 3];
    const r = setup({ chunks: lens.length, voiceS: (i) => lens[i], firstMs: 1800 });
    r.engine.play();
    await r.drive(30000);
    expect(r.engine.snapshot().state).toBe("ended");
    for (let i = 0; i < lens.length; i++) {
      const a = r.audioOf(`c${i}`)!;
      expect(a.pauses, `c${i} pauses`).toBe(0);
      expect(a.backJumps, `c${i} rewinds`).toEqual([]);
      expect(r.faceLagMs(i), `c${i} face lag`).not.toBeNull();
      expect(r.faceLagMs(i)!, `c${i} face lag`).toBeLessThanOrEqual(100);
      expect(r.stats(i).maxDrift, `c${i} drift`).toBeLessThan(0.1);
    }
  });

  it("slower render starts (3.5 s to the first fragment, several streams in flight): the chained wait still covers short chunks", async () => {
    const lens = [2, 0.9, 0.8, 1.1, 0.7, 3];
    const r = setup({ chunks: lens.length, voiceS: (i) => lens[i], firstMs: 3500, chainWaitMs: READ_CHAIN_WAIT_MS });
    r.engine.play();
    await r.drive(30000);
    expect(r.engine.snapshot().state).toBe("ended");
    for (let i = 0; i < lens.length; i++) {
      expect(r.faceLagMs(i), `c${i} face lag`).not.toBeNull();
      expect(r.faceLagMs(i)!, `c${i} face lag`).toBeLessThanOrEqual(100);
    }
    expect(r.opens.length).toBe(lens.length); // every clip opened once
  });
});
