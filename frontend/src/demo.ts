import type { FlatChunk } from "./reader/position";

/** Demo mode (?demo=1): Sherlock just after Holmes reads the King's letter, pre-rendered audio only, Holmes pinned. */
export const DEMO_START_PARA = 22;
export const DEMO_SPEAKER = "holmes";

export function isDemo(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get("demo") === "1";
}

/** Index of the first chunk at or after `para`, or the last chunk when the book is shorter. */
export function firstChunkAtOrAfter(chunks: FlatChunk[], para: number): number {
  const i = chunks.findIndex((c) => c.para >= para);
  return i === -1 ? Math.max(0, chunks.length - 1) : i;
}
