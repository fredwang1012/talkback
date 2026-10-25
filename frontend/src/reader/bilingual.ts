import type { Book } from "../api";
import { prefetchEnd } from "./prefetch";
import { flattenChunks, type FlatChunk } from "./position";

/**
 * Bilingual reading (B3): each paragraph read in the book's language and then in a second one, in the same voice
 * ("bilingual"), or only in the second language ("translated"). A translation slot is a FlatChunk that stands for
 * one original chunk read in `lang` (same speaker, voice and paragraph), so the engine, lip-sync and voice choices
 * work on it unchanged. Its text arrives lazily (POST /api/translate), only for paragraphs about to be read.
 */
export type BiMode = "off" | "bilingual" | "translated";
export type BiSettings = { mode: BiMode; lang: string }; // lang: ISO 639-3, as Boson's `language`

export const BILINGUAL_LANGUAGES: { code: string; label: string; html: string }[] = [
  { code: "spa", label: "Spanish", html: "es" },
  { code: "fra", label: "French", html: "fr" },
  { code: "deu", label: "German", html: "de" },
  { code: "ita", label: "Italian", html: "it" },
  { code: "por", label: "Portuguese", html: "pt" },
  { code: "zho", label: "Chinese", html: "zh" },
  { code: "jpn", label: "Japanese", html: "ja" },
  { code: "kor", label: "Korean", html: "ko" },
];
const OFF: BiSettings = { mode: "off", lang: "spa" };
const UNTRANSLATED = new Set(["code", "math"]); // a code note / an equation reading stays as it is

export function htmlLang(code: string): string {
  return BILINGUAL_LANGUAGES.find((l) => l.code === code)?.html ?? code;
}

export function languageLabel(code: string): string {
  return BILINGUAL_LANGUAGES.find((l) => l.code === code)?.label ?? code;
}

const key = (bookId: string) => `talkback:bilingual:${bookId}`;

export function loadBilingual(bookId: string): BiSettings {
  try {
    const raw = localStorage.getItem(key(bookId));
    if (!raw) return OFF;
    const v = JSON.parse(raw) as Partial<BiSettings>;
    const okMode = v.mode === "off" || v.mode === "bilingual" || v.mode === "translated";
    const okLang = BILINGUAL_LANGUAGES.some((l) => l.code === v.lang);
    return okMode && okLang ? { mode: v.mode as BiMode, lang: v.lang as string } : OFF;
  } catch {
    return OFF;
  }
}

export function saveBilingual(bookId: string, s: BiSettings): void {
  try { localStorage.setItem(key(bookId), JSON.stringify(s)); } catch { /* storage can be blocked */ }
}

/** The chunk list the reader plays for these settings ("off": exactly flattenChunks). */
export function readingChunks(book: Book, s: BiSettings): FlatChunk[] {
  const flat = flattenChunks(book);
  if (s.mode === "off") return flat;
  const out: FlatChunk[] = [];
  let i = 0;
  for (const p of book.paragraphs) {
    const own = flat.slice(i, i + p.tts_chunks.length);
    i += p.tts_chunks.length;
    if (UNTRANSLATED.has(p.kind ?? "")) { out.push(...own); continue; }
    if (s.mode === "bilingual") out.push(...own);
    out.push(...own.map((c) => ({ ...c, id: `${c.id}@${s.lang}`, translation: { lang: s.lang, of: c.id } })));
  }
  return out;
}

/** The original chunk a reading chunk stands for (itself, or the chunk a translation slot translates). */
export function sourceId(c: FlatChunk): string {
  return c.translation?.of ?? c.id;
}

/** Index in `to` of the sentence the reader is on in `from` (a mode or language change keeps the place). */
export function remapIndex(from: FlatChunk[], to: FlatChunk[], i: number): number {
  const cur = from[i];
  if (!cur || to.length === 0) return 0;
  const want = sourceId(cur);
  const sameKind = to.findIndex((c) => sourceId(c) === want && !!c.translation === !!cur.translation);
  if (sameKind >= 0) return sameKind;
  const any = to.findIndex((c) => sourceId(c) === want);
  return any >= 0 ? any : Math.min(i, to.length - 1);
}

/** Paragraphs whose translation is needed while chunk `i` plays: those of the translation slots in the TTS
 * prefetch window (never the whole book; only what the reader is about to reach, so no spoilers). */
export function parasToTranslate(chunks: FlatChunk[], i: number): number[] {
  if (chunks.length === 0) return [];
  const end = prefetchEnd(chunks, Math.max(0, Math.min(i, chunks.length - 1)));
  const paras: number[] = [];
  for (let n = i; n <= end; n++) {
    const c = chunks[n];
    if (c.translation && !paras.includes(c.para)) paras.push(c.para);
  }
  return paras;
}

/** The player bar's language picker: "" turns bilingual reading off (the language is kept for next time); a language
 * picked while off starts in "bilingual" mode. */
export function pickLanguage(s: BiSettings, code: string): BiSettings {
  if (!code) return { ...s, mode: "off" };
  return { lang: code, mode: s.mode === "off" ? "bilingual" : s.mode };
}
