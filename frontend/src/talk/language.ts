/** A small language guess from the transcript, shown as a badge ("Heard: Español"). Not used to steer the model:
 * Higgs Realtime detects the language itself and answers in kind. Returns null when there is too little to go on. */
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
