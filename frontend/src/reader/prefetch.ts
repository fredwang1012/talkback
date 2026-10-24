import type { FlatChunk } from "./position";

/** Chunks fetched ahead of the one being played. Spend is bounded by PREFETCH_MAX. */
export const PREFETCH_BASE = 3;
export const PREFETCH_MAX = 5;
/** Aim to have this much text (about 25 s of speech at 15 chars/s) requested ahead, so short lines
 *  (chapter headings, one-word paragraphs) at a section start do not leave the next long chunk unrequested. */
export const PREFETCH_MIN_CHARS = 360;

/**
 * Index of the last chunk to request while chunk `i` plays. Always crosses paragraph and section boundaries
 * (the chunk list is flat), takes at least PREFETCH_BASE chunks, and extends up to PREFETCH_MAX while the
 * lookahead is shorter than PREFETCH_MIN_CHARS.
 */
export function prefetchEnd(chunks: Pick<FlatChunk, "tts_text">[], i: number): number {
  const last = chunks.length - 1;
  let end = Math.min(i + PREFETCH_BASE, last);
  let chars = 0;
  for (let n = i + 1; n <= end; n++) chars += chunks[n].tts_text.length;
  while (end < Math.min(i + PREFETCH_MAX, last) && chars < PREFETCH_MIN_CHARS) {
    end++;
    chars += chunks[end].tts_text.length;
  }
  return end;
}
