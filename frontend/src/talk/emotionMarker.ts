/**
 * Emotion markers in a text-mode Realtime answer (B1), the browser side of `backend/app/emotion.py`: the model starts
 * each sentence with a feeling in square brackets ("[sadness] I miss him."). The marker is removed so it is never
 * shown or spoken, and kept only when it is one of the emotions the books use (schema.EMOTIONS); `/api/speak` then
 * turns it into a Boson `<|emotion:...|>` tag (and re-validates it). Any Boson tag in the text is stripped as well, so a
 * model (or a passage) can never inject one.
 */

/** The same list as `backend/app/schema.py` EMOTIONS. */
export const EMOTIONS: readonly string[] = [
  "elation", "amusement", "enthusiasm", "determination", "pride", "contentment",
  "affection", "relief", "contemplation", "confusion", "surprise", "awe", "longing",
  "anger", "fear", "disgust", "bitterness", "sadness", "shame", "helplessness",
];

const OPEN = "\\[［【";
const CLOSE = "\\]］】";
// a leading marker: [sadness], [Emotion: fear], ［surprise］, or the Boson form <|emotion:fear|>
const LEAD = new RegExp(`^\\s*(?:<\\|\\s*emotion\\s*:\\s*([^|>]*?)\\s*\\|>|[${OPEN}]\\s*(?:emotion\\s*:\\s*)?([^${OPEN}${CLOSE}\\n]{0,30}?)\\s*[${CLOSE}])\\s*`, "i");
const UNCLOSED = new RegExp(`^\\s*[${OPEN}]\\s*\\p{L}+\\s+`, "u"); // "[sadness I miss him." (no closing bracket)
// a stray bracket marker inside the text; starts only at the first space of a run (no backtracking)
const INNER = new RegExp(`(?<!\\s)\\s*[${OPEN}]\\s*(?:emotion\\s*:\\s*)?\\p{L}{1,24}\\s*[${CLOSE}]`, "giu");
const TAG = /<\|[^|<>]*\|>/g;
const TAG_PASSES = 4;
const SENTENCE_MAX = 2000;

/** `text` with no Boson <|...|> tag left (removals can join a new one, "<|<|x|>sfx:laughter|>"), spaces collapsed. */
export function stripTags(text: string): string {
  for (let i = 0; i < TAG_PASSES; i++) {
    if (!text.includes("<|") || !text.includes("|>")) break;
    text = text.replace(TAG, "");
  }
  if (text.includes("<|") || text.includes("|>")) text = text.replace(/\|/g, "");
  return text.replace(/\s{2,}/g, " ").trim();
}

/** The clean sentence and its emotion (null for unknown, translated, malformed or "neutral" markers). */
export function splitMarker(sentence: string): { text: string; emotion: string | null } {
  let s = sentence.slice(0, SENTENCE_MAX).trim();
  let emotion: string | null = null;
  const m = LEAD.exec(s);
  if (m) {
    const value = (m[1] ?? m[2] ?? "").trim().toLowerCase();
    emotion = EMOTIONS.includes(value) ? value : null;
    s = s.slice(m[0].length);
  } else {
    s = s.replace(UNCLOSED, "");
  }
  return { text: stripTags(s.replace(INNER, "")), emotion };
}
