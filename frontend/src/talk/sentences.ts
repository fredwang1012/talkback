/**
 * Splits a streamed text answer into sentences as it arrives (B1), the way `gcp/llm.py stream_sentences` does for
 * Gemini: a sentence ends at `. ! ? 。！？` (plus closing quotes or brackets) followed by whitespace, or by the next
 * sentence's `[feeling]` marker. The tail stays in `rest` until more text or `final` says it is complete, because the
 * next delta may continue it ("3." + "5 litres"). A run with no sentence end is cut at a space before the TTS limit
 * (`/api/speak` takes at most 400 characters; the marker is still on the first piece).
 */

/** Longest piece handed to TTS as one sentence (under /api/speak's 400, with room for the marker). */
export const MAX_SENTENCE_CHARS = 360;

// a Latin stop needs whitespace (or the next marker) after it, so "3.5" stays whole; a CJK stop ends a sentence by itself
const END = /(?:[.!?]+["'”’)\]]*(?:\s+|(?=[\[［【]))|[。！？]+["'”’)\]]*\s*)/g;

export function takeSentences(buf: string, final = false): { done: string[]; rest: string } {
  const done: string[] = [];
  let start = 0;
  END.lastIndex = 0;
  for (let m = END.exec(buf); m; m = END.exec(buf)) {
    const end = m.index + m[0].length;
    const piece = buf.slice(start, end).trim();
    if (piece) done.push(piece);
    start = end;
  }
  let rest = buf.slice(start);
  // an endless run: cut whole words off the front so TTS never gets more than it takes
  while (rest.trim().length > MAX_SENTENCE_CHARS) {
    const cut = rest.lastIndexOf(" ", MAX_SENTENCE_CHARS);
    const at = cut > MAX_SENTENCE_CHARS / 2 ? cut : MAX_SENTENCE_CHARS;
    const piece = rest.slice(0, at).trim();
    if (piece) done.push(piece);
    rest = rest.slice(at);
  }
  if (final) {
    const tail = rest.trim();
    if (tail) done.push(tail);
    rest = "";
  } else if (!rest.trim()) {
    rest = "";
  } else {
    rest = rest.replace(/^\s+/, "");
  }
  return { done, rest };
}
