import { beforeEach, describe, expect, it } from "vitest";
import type { Book, Character } from "../src/api";
import {
  clampIndex, flattenChunks, loadPosition, readerPara, savePosition, silentDurationMs, splitCharacters,
} from "../src/reader/position";

const chunk = (id: string, s: number, e: number) => ({
  id, speaker: "narrator", voice: "marcus", tts_text: id, char_start: s, char_end: e,
});
const book = {
  paragraphs: [
    { idx: 0, section_id: "s1", text: "", segments: [], tts_chunks: [chunk("p0c0", 0, 10), chunk("p0c1", 11, 20)] },
    { idx: 1, section_id: "s1", text: "", segments: [], tts_chunks: [chunk("p1c0", 0, 5)] },
  ],
} as unknown as Book;

describe("position helpers", () => {
  it("flattens chunks in book order and tags each with its paragraph", () => {
    expect(flattenChunks(book).map((c) => [c.id, c.para])).toEqual([["p0c0", 0], ["p0c1", 0], ["p1c0", 1]]);
  });

  it("clamps indexes, including an empty book", () => {
    expect(clampIndex(-3, 5)).toBe(0);
    expect(clampIndex(9, 5)).toBe(4);
    expect(clampIndex(2, 0)).toBe(0);
  });

  it("reports the reader's paragraph from the chunk index", () => {
    const flat = flattenChunks(book);
    expect([0, 1, 2].map((i) => readerPara(flat, i))).toEqual([0, 0, 1]);
    expect(readerPara([], 0)).toBe(0);
  });

  it("locks characters who first appear after the reader's paragraph", () => {
    const chars = [
      { id: "holmes", first_para: 0 }, { id: "king", first_para: 40 }, { id: "irene", first_para: 41 },
    ] as Character[];
    const { met, locked } = splitCharacters(chars, 40);
    expect(met.map((c) => c.id)).toEqual(["holmes", "king"]);
    expect(locked.map((c) => c.id)).toEqual(["irene"]);
  });

  it("estimates a minimum silent duration and scales with speed", () => {
    const flat = flattenChunks(book);
    expect(silentDurationMs(flat[2], 1)).toBe(900);
    expect(silentDurationMs({ ...flat[0], char_end: 300 }, 2)).toBeLessThan(silentDurationMs({ ...flat[0], char_end: 300 }, 1));
  });
});

describe("saved position", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    };
  });

  it("round-trips and clamps to the book length", () => {
    savePosition("b", 12);
    expect(loadPosition("b", 100)).toBe(12);
    expect(loadPosition("b", 5)).toBe(4);
    expect(loadPosition("other", 100)).toBe(0);
  });

  it("falls back to the start when storage is blocked or corrupt", () => {
    store.set("bookson:pos:b", "nonsense");
    expect(loadPosition("b", 10)).toBe(0);
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    };
    expect(loadPosition("b", 10)).toBe(0);
    expect(() => savePosition("b", 3)).not.toThrow();
  });
});
