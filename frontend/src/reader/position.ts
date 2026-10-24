import type { Book, Character, TtsChunk } from "../api";

/** One readable unit: a TTS chunk plus where it sits in the book. */
export type FlatChunk = TtsChunk & { para: number };

export function flattenChunks(book: Book): FlatChunk[] {
  return book.paragraphs.flatMap((p) => p.tts_chunks.map((c) => ({ ...c, para: p.idx })));
}

export function clampIndex(i: number, length: number): number {
  return length === 0 ? 0 : Math.max(0, Math.min(i, length - 1));
}

/** The paragraph the reader has reached. Drives spoiler filtering, so it must never run ahead of the chunk. */
export function readerPara(chunks: FlatChunk[], index: number): number {
  return chunks[clampIndex(index, chunks.length)]?.para ?? 0;
}

/** Characters the reader has met. Everything else stays locked (spoiler rule, mirrored from the backend). */
export function splitCharacters(characters: Character[], para: number) {
  return {
    met: characters.filter((c) => c.first_para <= para),
    locked: characters.filter((c) => c.first_para > para),
  };
}

/** Rough silent reading time for one chunk, used until audio exists. */
export function silentDurationMs(chunk: FlatChunk, speed: number): number {
  const spoken = chunk.char_end - chunk.char_start;
  return Math.max(900, (spoken / 15 / speed) * 1000);
}

const key = (bookId: string) => `bookson:pos:${bookId}`;

export function loadPosition(bookId: string, length: number): number {
  try {
    const raw = localStorage.getItem(key(bookId));
    const n = raw === null ? 0 : Number.parseInt(raw, 10);
    return Number.isFinite(n) ? clampIndex(n, length) : 0;
  } catch {
    return 0;
  }
}

export function savePosition(bookId: string, index: number): void {
  try {
    localStorage.setItem(key(bookId), String(index));
  } catch {
    /* storage can be blocked; the reader still works without it */
  }
}
