/** A small language guess from the transcript, shown as a badge ("Heard: Español"). Not used to steer the model:
 * Higgs Realtime detects the language itself and answers in kind. Returns null when there is too little to go on.
 * #135: a *clear* guess (`clearLanguage`) also sets the Realtime transcription hint and the TTS language of answers. */
export type Detected = { code: string; name: string };

const SCRIPTS: { code: string; name: string; re: RegExp }[] = [
  { code: "ja", name: "日本語", re: /[぀-ヿ]/ },
  { code: "ko", name: "한국어", re: /[가-힯]/ },
  { code: "zh", name: "中文", re: /[一-鿿]/ },
  { code: "ru", name: "Русский", re: /[Ѐ-ӿ]/ },
  { code: "ar", name: "العربية", re: /[؀-ۿ]/ },
  { code: "hi", name: "हिन्दी", re: /[ऀ-ॿ]/ },
  { code: "he", name: "עברית", re: /[֐-׿]/ },
  { code: "el", name: "Ελληνικά", re: /[Ͱ-Ͽ]/ },
  { code: "th", name: "ไทย", re: /[฀-๿]/ },
];

const STOPWORDS: { code: string; name: string; words: string[] }[] = [
  { code: "en", name: "English", words: ["the", "is", "and", "what", "who", "why", "how", "does", "did", "this", "that", "of", "to", "he", "she", "do", "you", "was", "are", "it"] },
  { code: "es", name: "Español", words: ["el", "la", "los", "las", "que", "quién", "quien", "por", "qué", "es", "una", "un", "del", "con", "cómo", "como", "y", "se", "para", "está"] },
  { code: "fr", name: "Français", words: ["le", "la", "les", "est", "que", "qui", "pourquoi", "quoi", "une", "un", "des", "du", "et", "comment", "pas", "ce", "cette", "dans", "sur", "elle"] },
  { code: "de", name: "Deutsch", words: ["der", "die", "das", "ist", "und", "was", "wer", "warum", "wie", "nicht", "ein", "eine", "mit", "ich", "sie", "er", "zu", "den", "dem", "auf"] },
  { code: "pt", name: "Português", words: ["o", "os", "as", "que", "quem", "por", "porque", "é", "uma", "um", "do", "da", "com", "como", "não", "se", "para", "está", "ele", "ela"] },
  { code: "it", name: "Italiano", words: ["il", "lo", "gli", "che", "chi", "perché", "è", "una", "un", "del", "della", "con", "come", "non", "si", "per", "sono", "questo", "lei", "lui"] },
];

export function detectLanguage(text: string): Detected | null {
  const t = text.trim();
  if (t.length < 2) return null;
  for (const s of SCRIPTS) if (s.re.test(t)) return { code: s.code, name: s.name };
  const words = t.toLowerCase().match(/[\p{L}']+/gu) ?? [];
  if (words.length === 0) return null;
  let best: { code: string; name: string; score: number } | null = null;
  for (const lang of STOPWORDS) {
    const set = new Set(lang.words);
    const score = words.filter((w) => set.has(w)).length;
    if (score > (best?.score ?? 0)) best = { code: lang.code, name: lang.name, score };
  }
  if (best) return { code: best.code, name: best.name };
  return words.length >= 3 ? { code: "en", name: "English" } : null; // Latin script, no stopwords: assume English
}

/**
 * A guess confident enough to act on (#135): a script, or at least two stopwords of one language and more than of any
 * other. A wrong transcription hint is costly (live: an "es" hint on English speech gave no transcript at all), so
 * anything less clear is null.
 */
export function clearLanguage(text: string): Detected | null {
  const t = text.trim();
  if (t.length < 2) return null;
  for (const s of SCRIPTS) if (s.re.test(t)) return { code: s.code, name: s.name };
  const words = t.toLowerCase().match(/[\p{L}']+/gu) ?? [];
  const scores = STOPWORDS.map((lang) => { const set = new Set(lang.words); return { lang, score: words.filter((w) => set.has(w)).length }; })
    .sort((a, b) => b.score - a.score);
  const [best, second] = scores;
  if (!best || best.score < 2 || best.score <= (second?.score ?? 0)) return null;
  return { code: best.lang.code, name: best.lang.name };
}

/** The newest clear language of the reader's last three questions (oldest first), as ISO 639-1, or null. */
function newestClear(recent: string[]): string | null {
  for (const q of recent.slice(-3).reverse()) {
    const d = clearLanguage(q);
    if (d) return d.code;
  }
  return null;
}

/**
 * The Realtime transcription hint (ISO 639-1) from the reader's recent questions, oldest first. A wrong hint costs a
 * whole turn (live: no transcript at all), so it needs the two newest clear questions of the last three to be in the
 * same language, and not English; anything else gives null (no hint).
 */
export function transcriptionHint(recent: string[]): string | null {
  const clear = recent.slice(-3).map((q) => clearLanguage(q)?.code ?? null).filter((c): c is string => c !== null);
  const [a, b] = clear.slice(-2);
  return clear.length >= 2 && a === b && a !== "en" ? a : null;
}

/** ISO 639-1 (the detector's codes) -> ISO 639-3 for /api/speak `language`; null for English and anything unknown. */
const ISO_639_3: Record<string, string> = {
  es: "spa", fr: "fra", de: "deu", pt: "por", it: "ita", ja: "jpn", ko: "kor", zh: "zho", ru: "rus", ar: "ara",
  hi: "hin", he: "heb", el: "ell", th: "tha",
};
export function toIso6393(code: string | null | undefined): string | null {
  return (code && ISO_639_3[code]) || null;
}

/** The language to voice an answer in (ISO 639-3), when the reader's recent questions are clearly not English. */
export function answerLanguage(recent: string[]): string | null {
  const code = newestClear(recent); // the voice follows the newest clear question at once (a wrong guess only sounds odd)
  return code === "en" ? null : toIso6393(code);
}
