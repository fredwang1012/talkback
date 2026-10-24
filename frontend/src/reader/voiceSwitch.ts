/** Pure helpers for switching the voice while reading (issue #37). */

export type Timing = { start: number };

/** Chunk indexes whose recorded voice no longer matches the voice they should have now. */
export function staleIndexes(
  have: Iterable<[number, string | undefined]>,
  want: (i: number) => string | undefined,
): number[] {
  const out: number[] = [];
  for (const [i, v] of have) if (v !== want(i)) out.push(i);
  return out;
}

/**
 * Where to resume a chunk re-rendered in a new voice: the same word (by position among the words) the
 * old voice had reached, so the listener hears the same text again rather than starting over.
 * Falls back to the chunk start without timings.
 */
export function resumeTime(oldWords: Timing[] | null, oldTime: number, newWords: Timing[] | null): number {
  if (!oldWords?.length || !newWords?.length || !(oldTime > 0)) return 0;
  let n = -1;
  for (let i = 0; i < oldWords.length && oldWords[i].start <= oldTime; i++) n = i;
  if (n < 0) return 0;
  return newWords[Math.min(n, newWords.length - 1)].start;
}

/** True for the browser's "play() was blocked until the user interacts" error. */
export function isAutoplayBlock(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { name?: string }).name === "NotAllowedError";
}
