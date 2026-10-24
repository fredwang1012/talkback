import { describe, expect, it } from "vitest";
import { firstChunkAtOrAfter, isDemo } from "../src/demo";
import type { FlatChunk } from "../src/reader/position";

const chunks = [0, 0, 1, 2, 2, 5].map((para, i) => ({ id: `c${i}`, para }) as unknown as FlatChunk);

describe("demo mode", () => {
  it("is on only for ?demo=1", () => {
    expect(isDemo("?demo=1")).toBe(true);
    expect(isDemo("?demo=0")).toBe(false);
    expect(isDemo("")).toBe(false);
    expect(isDemo("?x=1&demo=1")).toBe(true);
  });
  it("starts at the first chunk of the paragraph, or the next one that has chunks", () => {
    expect(firstChunkAtOrAfter(chunks, 2)).toBe(3);
    expect(firstChunkAtOrAfter(chunks, 3)).toBe(5);
    expect(firstChunkAtOrAfter(chunks, 99)).toBe(5);
    expect(firstChunkAtOrAfter([], 5)).toBe(0);
  });
});
