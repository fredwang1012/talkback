import { beforeEach, describe, expect, it } from "vitest";
import { loadNaturalSpeed, NATURAL_SPEED_KEY, rateFor, saveNaturalSpeed, speedTagFor } from "../src/reader/naturalSpeed";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";

describe("natural speed mapping", () => {
  it("is off by default and falls back to playbackRate", () => {
    for (const s of [0.75, 1, 1.25, 1.5]) { expect(speedTagFor(s, false)).toBeNull(); expect(rateFor(s, null)).toBe(s); }
  });
  it("maps only tagged speeds when on", () => {
    expect(speedTagFor(0.75, true)).toBe("slow");
    expect(speedTagFor(1.25, true)).toBe("fast");
    expect(speedTagFor(1.5, true)).toBe("very_fast");
    expect(speedTagFor(1, true)).toBeNull();
    expect(speedTagFor(1.1, true)).toBeNull();
  });
  it("plays tagged audio at 1.0", () => { expect(rateFor(1.5, "very_fast")).toBe(1); });
  it("is remembered in localStorage", () => {
    const store = new Map<string, string>();
    (globalThis as unknown as { localStorage: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    expect(loadNaturalSpeed()).toBe(false);
    saveNaturalSpeed(true);
    expect(store.get(NATURAL_SPEED_KEY)).toBe("1");
    expect(loadNaturalSpeed()).toBe(true);
    saveNaturalSpeed(false);
    expect(loadNaturalSpeed()).toBe(false);
  });
});

class FakeAudio implements AudioLike {
  currentTime = 0; playbackRate = 1; preservesPitch = false; preload = ""; onended: (() => void) | null = null;
  constructor(public src: string) {}
  async play() {}
  pause() {}
}
const flush = () => new Promise((r) => setTimeout(r, 0));
const chunks: FlatChunk[] = Array.from({ length: 4 }, (_, i) => ({
  id: `c${i}`, speaker: "narrator", voice: "marcus", tts_text: `t${i}`.padEnd(200, "."), char_start: 0, char_end: 10, para: i,
}));

function setup() {
  const fetched: { id: string; speed?: string | null }[] = [];
  const audios: FakeAudio[] = [];
  const engine = new ReaderEngine(chunks, {
    fetchTts: async (chunk, _voice, speed): Promise<TtsResult> => {
      fetched.push({ id: chunk.id, speed });
      return { audio_url: `/media/${chunk.id}-${speed ?? "n"}.mp3`, timestamps: null };
    },
    createAudio: (url) => { const a = new FakeAudio(url); audios.push(a); return a; },
  });
  return { engine, fetched, audios };
}

describe("ReaderEngine natural speed", () => {
  it("off: fetches without a speed and uses playbackRate (unchanged)", async () => {
    const { engine, fetched, audios } = setup();
    engine.setSpeed(1.5);
    engine.play();
    await flush();
    expect(fetched.every((f) => !f.speed)).toBe(true);
    expect(audios[0].playbackRate).toBe(1.5);
  });
  it("on: requests the speed tag for played and prefetched chunks only, audio plays at 1.0", async () => {
    const { engine, fetched, audios } = setup();
    engine.setSpeed(1.5, "very_fast");
    engine.play();
    await flush();
    expect(fetched.length).toBeGreaterThan(0);
    expect(fetched.length).toBeLessThanOrEqual(4);
    expect(fetched.every((f) => f.speed === "very_fast")).toBe(true);
    expect(audios[0].playbackRate).toBe(1);
  });
  it("changing the tag mid-play refetches only the window, not the whole book", async () => {
    const { engine, fetched } = setup();
    engine.play();
    await flush();
    const before = fetched.length;
    engine.setSpeed(1.25, "fast");
    await flush();
    const added = fetched.slice(before);
    expect(added.length).toBeGreaterThan(0);
    expect(added.every((f) => f.speed === "fast")).toBe(true);
    expect(added.length).toBeLessThanOrEqual(chunks.length);
  });
  it("switching natural speed off returns to plain audio at the playbackRate", async () => {
    const { engine, fetched, audios } = setup();
    engine.setSpeed(1.5, "very_fast");
    engine.play();
    await flush();
    engine.setSpeed(1.5, null);
    await flush();
    expect(fetched.at(-1)?.speed ?? null).toBeNull();
    expect(audios.at(-1)!.playbackRate).toBe(1.5);
  });
});

describe("PlayerBar natural speed hint", () => {
  it("shows the credit hint when on", async () => {
    const { renderToStaticMarkup } = await import("react-dom/server");
    const { createElement } = await import("react");
    const PlayerBar = (await import("../src/reader/PlayerBar")).default;
    const base = { playing: false, onToggle() {}, onPrev() {}, onNext() {}, speed: 1.5, speeds: [1, 1.5], onSpeed() {},
      onVoices() {}, autoResume: true, onAutoResume() {}, para: 0, totalParas: 1, atStart: true, atEnd: false, hasCast: false,
      onRecap() {}, inSeries: false, audioAvailable: true, audioError: null };
    const on = renderToStaticMarkup(createElement(PlayerBar, { ...base, naturalSpeed: true, onNaturalSpeed() {} }));
    expect(on).toContain("Natural speed");
    expect(on).toContain("uses Boson credit");
    const off = renderToStaticMarkup(createElement(PlayerBar, { ...base, naturalSpeed: false, onNaturalSpeed() {} }));
    expect(off).toContain("Reading aloud.");
    expect(off).not.toContain("(uses Boson credit)");
  });
});

describe("ReaderEngine natural speed change while a chunk loads", () => {
  class TrackedAudio extends FakeAudio {
    paused = true;
    async play() { this.paused = false; }
    pause() { this.paused = true; }
  }
  function deferredSetup() {
    const fetched: { id: string; speed?: string | null }[] = [];
    const pending: (() => void)[] = [];
    const audios: TrackedAudio[] = [];
    const engine = new ReaderEngine(chunks, {
      fetchTts: (chunk, _voice, speed): Promise<TtsResult> => {
        fetched.push({ id: chunk.id, speed });
        return new Promise((res) => pending.push(() => res({ audio_url: `/media/${chunk.id}-${speed ?? "n"}.mp3`, timestamps: null })));
      },
      createAudio: (url) => { const a = new TrackedAudio(url); audios.push(a); return a; },
    });
    return { engine, fetched, pending, audios };
  }

  it("changing the tag during 'Preparing audio' still plays the loading chunk (not paused, onended wired)", async () => {
    const { engine, fetched, pending, audios } = deferredSetup();
    engine.setSpeed(1, "slow");
    engine.play();
    await flush();
    expect(engine.snapshot().state).toBe("loading");
    const firstFetches = fetched.filter((f) => f.id === "c0").length;
    engine.setSpeed(1.25, "fast");
    await flush();
    for (const p of pending.splice(0)) p();
    await flush(); await flush();
    expect(engine.snapshot().state).toBe("playing");
    const playing = audios.find((a) => !a.paused);
    expect(playing).toBeDefined();
    expect(playing!.src).toContain("c0");
    expect(playing!.onended).not.toBeNull();
    // no second synth of the chunk that was already loading
    expect(fetched.filter((f) => f.id === "c0").length).toBe(firstFetches);
  });
});
