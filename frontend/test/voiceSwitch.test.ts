import { describe, expect, it } from "vitest";
import { isAutoplayBlock, resumeTime, staleIndexes } from "../src/reader/voiceSwitch";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";

describe("voiceSwitch helpers", () => {
  it("finds only the entries whose voice changed", () => {
    const have: [number, string | undefined][] = [[1, "a"], [2, "b"], [3, undefined]];
    expect(staleIndexes(have, (i) => (i === 2 ? "c" : i === 1 ? "a" : undefined))).toEqual([2]);
  });
  it("resumes at the same word in the new voice", () => {
    const old = [{ start: 0 }, { start: 1 }, { start: 2 }];
    const next = [{ start: 0 }, { start: 1.5 }, { start: 3 }];
    expect(resumeTime(old, 1.2, next)).toBe(1.5);
    expect(resumeTime(old, 9, [{ start: 0 }, { start: 2 }])).toBe(2);
    expect(resumeTime(null, 5, next)).toBe(0);
    expect(resumeTime(old, 0, next)).toBe(0);
  });
  it("detects autoplay blocks", () => {
    expect(isAutoplayBlock({ name: "NotAllowedError" })).toBe(true);
    expect(isAutoplayBlock(new Error("x"))).toBe(false);
  });
});

let blocked = false;
class FakeAudio implements AudioLike {
  currentTime = 0; playbackRate = 1; preservesPitch = false; preload = "";
  onended: (() => void) | null = null; playing = false;
  constructor(public src: string) {}
  async play() {
    if (blocked) { const e = new Error("blocked"); e.name = "NotAllowedError"; throw e; }
    this.playing = true;
  }
  pause() { this.playing = false; }
}
const flush = () => new Promise((r) => setTimeout(r, 0));
const chunks: FlatChunk[] = Array.from({ length: 8 }, (_, i) => ({
  id: `c${i}`, speaker: i % 2 ? "bob" : "narrator", voice: "marcus", tts_text: `t${i} ${"long sentence text ".repeat(12)}`, char_start: 0, char_end: 10, para: i,
}));

function setup() {
  const fetched: string[] = [];
  const audios: FakeAudio[] = [];
  const engine = new ReaderEngine(chunks, {
    fetchTts: async (c, voice): Promise<TtsResult> => {
      fetched.push(`${c.id}:${voice ?? "-"}`);
      return {
        audio_url: `/m/${c.id}-${voice ?? "-"}.mp3`,
        timestamps: [{ start: 0, end: 1, char_start: 0, char_end: 2 }, { start: 1, end: 2, char_start: 3, char_end: 5 }],
      };
    },
    createAudio: (u) => { const a = new FakeAudio(u); audios.push(a); return a; },
  });
  return { engine, fetched, audios };
}

describe("ReaderEngine voice switching", () => {
  it("restarts the current chunk in the new voice at the same word, and refetches the window", async () => {
    blocked = false;
    const { engine, fetched, audios } = setup();
    engine.play(); await flush();
    audios[0].currentTime = 1.2;
    fetched.length = 0;
    engine.setNarratorOnly("belinda"); await flush();
    expect(fetched).toEqual(["c0:belinda", "c1:belinda", "c2:belinda", "c3:belinda"]); // same depth as before
    const fresh = audios.find((a) => a.src.includes("c0-belinda"))!;
    expect(fresh.playing).toBe(true);
    expect(fresh.currentTime).toBe(1);
    expect(audios[0].playing).toBe(false);
    expect(engine.snapshot().state).toBe("playing");
  });
  it("only refetches chunks whose voice changed", async () => {
    blocked = false;
    const { engine, fetched } = setup();
    engine.setVoiceResolver((c) => (c.speaker === "bob" ? "x" : undefined));
    engine.play(); await flush();
    fetched.length = 0;
    engine.setVoiceResolver((c) => (c.speaker === "bob" ? "y" : undefined));
    await flush();
    expect(fetched.sort()).toEqual(["c1:y", "c3:y"]);
  });
  it("pauses instead of erroring when the restart is blocked by autoplay", async () => {
    blocked = false;
    const { engine } = setup();
    engine.play(); await flush();
    blocked = true;
    engine.setNarratorOnly("belinda"); await flush();
    expect(engine.snapshot().state).toBe("paused");
    expect(engine.snapshot().error).toMatch(/Tap play/); // the #26 notice, not a raw error
  });
  it("while paused, drops the current audio so play starts it in the new voice", async () => {
    blocked = false;
    const { engine, fetched } = setup();
    engine.play(); await flush();
    engine.pause();
    fetched.length = 0;
    engine.setNarratorOnly("belinda"); await flush();
    expect(fetched).toEqual([]);
    engine.play(); await flush();
    expect(fetched[0]).toBe("c0:belinda");
    expect(engine.snapshot().state).toBe("playing");
  });
});
