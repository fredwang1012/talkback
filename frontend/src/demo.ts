import type { FlatChunk } from "./reader/position";

/** Demo mode (?demo=1): Harry Potter book 1 at a preset position, pre-rendered audio only, Hagrid pinned. */
export const DEMO_BOOK = "hp1-sorcerers-stone";
export const DEMO_START_PARA = 300; // set after ingest
export const DEMO_SPEAKER = "hagrid";

export function isDemo(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get("demo") === "1";
}

/** Index of the first chunk at or after `para`, or the last chunk when the book is shorter. */
export function firstChunkAtOrAfter(chunks: FlatChunk[], para: number): number {
  const i = chunks.findIndex((c) => c.para >= para);
  return i === -1 ? Math.max(0, chunks.length - 1) : i;
}
