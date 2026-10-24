import { describe, expect, it } from "vitest";
import { prefetchEnd } from "../src/reader/prefetch";
import { ReaderEngine, type AudioLike } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";

const mk = (lens: number[]) => lens.map((n) => ({ tts_text: "x".repeat(n) }));

describe("prefetchEnd", () => {
  it("takes 3 chunks when the lookahead is long enough", () => {
    expect(prefetchEnd(mk([200, 200, 200, 200, 200, 200]), 0)).toBe(3);
  });
  it("extends past short heading chunks, up to 5 ahead", () => {
    expect(prefetchEnd(mk([10, 8, 5, 4, 6, 7, 9, 200]), 0)).toBe(5);
  });
  it("extends only until enough text is ahead", () => {
    expect(prefetchEnd(mk([10, 8, 5, 4, 400, 7, 9]), 0)).toBe(4);
  });
  it("stops at the end of the book", () => {
    expect(prefetchEnd(mk([5, 5, 5]), 1)).toBe(2);
  });
});

describe("ReaderEngine prefetch", () => {
  it("requests the next chunks while the first one is still being fetched", async () => {
    const chunks: FlatChunk[] = Array.from({ length: 6 }, (_, i) => ({
      id: `c${i}`, speaker: "n", voice: "v", tts_text: "x".repeat(200), char_start: 0, char_end: 200, para: i,
    }));
    const requested: string[] = [];
    const engine = new ReaderEngine(chunks, {
      fetchTts: (c) => { requested.push(c.id); return new Promise(() => {}); }, // never resolves
      createAudio: () => ({}) as AudioLike,
    });
    engine.play();
    await new Promise((r) => setTimeout(r, 0));
    expect(requested).toEqual(["c0", "c1", "c2", "c3"]);
  });
});
