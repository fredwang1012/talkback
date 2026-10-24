import { saveCast, type AnimatedFace, type Book, type Cast, type Face } from "../api";

/** Voice presets the server knows (kept in step with backend/app/voices.py). */
export const PRESETS = ["chloe", "eleanor", "jake", "marcus", "nora", "oliver"];
const FALLBACK_VOICE = "marcus";

// ---------- per-book choices kept in the browser ----------

export type ReadingStyle = { mode: "one" | "cast"; voice: string | null; face: string | null };
/** voice: null = Auto. face: null = Auto, "none" = no face, else a face id. */
export type TalkChoice = { voice: string | null; face: string | null };

const styleKey = (bookId: string) => `talkback:style:${bookId}`;
const talkKey = (bookId: string) => `talkback:talk:${bookId}`;

function readJson(key: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(localStorage.getItem(key) ?? "null");
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch { return null; }
}
function writeJson(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage blocked */ }
}
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export function loadStyle(bookId: string): ReadingStyle {
  const o = readJson(styleKey(bookId));
  return { mode: o?.mode === "one" ? "one" : "cast", voice: str(o?.voice), face: str(o?.face) };
}
export const saveStyle = (bookId: string, s: ReadingStyle) => writeJson(styleKey(bookId), s);

export function loadTalkChoice(bookId: string): TalkChoice {
  const o = readJson(talkKey(bookId));
  return { voice: str(o?.voice), face: str(o?.face) };
}
export const saveTalkChoice = (bookId: string, c: TalkChoice) => writeJson(talkKey(bookId), c);

/** After a voice or face is deleted, browser-side choices that pointed at it fall back to Auto (null). */
export function dropReferences(style: ReadingStyle, talk: TalkChoice, gone: { voice?: string; face?: string }) {
  const v = (x: string | null) => (gone.voice && x === gone.voice ? null : x);
  const f = (x: string | null) => (gone.face && x === gone.face ? null : x);
  return {
    style: { ...style, voice: v(style.voice), face: f(style.face) },
    talk: { voice: v(talk.voice), face: f(talk.face) },
  };
}

// ---------- resolving voices and faces ----------

export const emptyCast = (): Cast => ({ main: [], narrator: { voice: null, face: null }, characters: {} });

/** The book's own voice for a speaker, ignoring the cast. */
export function bookVoice(book: Book, id: string): string {
  return (id === "narrator" ? book.narrator?.voice : book.characters.find((c) => c.id === id)?.voice) ?? FALLBACK_VOICE;
}

/** The reading-cast voice: the cast setting, else the book default. */
export function castVoice(book: Book, cast: Cast, id: string): string {
  const a = id === "narrator" ? cast.narrator : cast.characters[id];
  return a?.voice ?? bookVoice(book, id);
}
export function castFace(cast: Cast, id: string): string | null {
  return (id === "narrator" ? cast.narrator : cast.characters[id])?.face ?? null;
}

export const isNovel = (book: Book) => book.kind === "novel";
/** Textbooks have only a narrator, so they behave as Full cast with one row (no toggle). */
export const effectiveMode = (book: Book, style: ReadingStyle) => (isNovel(book) ? style.mode : "cast");

/** The voice_override sent with every chunk in One voice mode; null (send nothing) in Full cast and for textbooks. */
export function oneVoiceOverride(book: Book, cast: Cast, style: ReadingStyle): string | null {
  if (effectiveMode(book, style) !== "one") return null;
  return style.voice ?? castVoice(book, cast, "narrator");
}

/** Rows of the Full cast table: the narrator, then every main character (novels only; textbooks have just the narrator). */
export function castRows(book: Book, cast: Cast): { id: string; name: string }[] {
  const main = isNovel(book) ? cast.main.map((id) => ({ id, name: book.characters.find((c) => c.id === id)?.name ?? id })) : [];
  return [{ id: "narrator", name: "Narrator" }, ...main];
}

/** Id of the face to show while a chunk by `speaker` is read, or null. */
export function readingFaceId(book: Book, cast: Cast, style: ReadingStyle, speaker: string): string | null {
  if (effectiveMode(book, style) === "one") return style.face;
  return castFace(cast, isNovel(book) ? speaker : "narrator");
}

export function talkVoice(book: Book, cast: Cast, choice: TalkChoice, id: string): string {
  return choice.voice ?? castVoice(book, cast, id);
}

/** Auto = the character's cast face, else the face bound to the voice (a clone made with a face). */
export function talkFaceId(cast: Cast, choice: TalkChoice, id: string, voice: string, faces: Face[]): string | null {
  if (choice.face === "none") return null;
  if (choice.face) return choice.face;
  return castFace(cast, id) ?? faces.find((f) => f.voice === voice)?.id ?? null;
}

// ---------- saving ----------

/** `cast` with one speaker's voice/face changed (a full copy, ready for saveCast). */
export function withAssignment(cast: Cast, id: string, patch: Partial<Cast["narrator"]>): Cast {
  if (id === "narrator") return { ...cast, narrator: { ...cast.narrator, ...patch } };
  const cur = cast.characters[id] ?? { voice: null, face: null };
  return { ...cast, characters: { ...cast.characters, [id]: { ...cur, ...patch } } };
}

/** Saves the latest cast one request at a time; a burst of changes sends only the newest state. */
export function createCastSaver(
  bookId: string, onSaved: (c: Cast) => void, onError: (m: string) => void,
  save: (bookId: string, c: Cast) => Promise<Cast> = saveCast,
) {
  let pending: Cast | null = null;
  let running = false;
  async function run() {
    if (running) return;
    running = true;
    while (pending) {
      const next = pending;
      pending = null;
      try { onSaved(await save(bookId, next)); } catch (e) { onError(e instanceof Error ? e.message : String(e)); }
    }
    running = false;
  }
  let current: Promise<void> = Promise.resolve();
  const push = (cast: Cast) => { pending = cast; if (!running) current = run(); return current; };
  /** Resolves when every queued save has been sent. */
  push.flush = async () => { await current; };
  return push;
}

// ---------- migration of the old series-wide voice overrides ----------

export const oldOverridesKey = (book: Pick<Book, "id" | "series">) => `talkback:voices:${book.series ?? book.id}`;

type Store = Pick<Storage, "getItem" | "removeItem">;
function defaultStore(): Store | null { try { return localStorage; } catch { return null; } }

/**
 * One-time move of the old localStorage voice overrides into the server cast. Only speakers whose server voice
 * is still null are written; the old key is removed afterwards. A failed save keeps the key to retry next load.
 * Returns the saved cast, or null when nothing was written.
 */
export async function migrateOldOverrides(
  book: Book, cast: Cast, save: (bookId: string, c: Cast) => Promise<Cast> = saveCast, storage: Store | null = defaultStore(),
): Promise<Cast | null> {
  if (!storage) return null;
  const key = oldOverridesKey(book);
  let old: Record<string, unknown> | null = null;
  try {
    const parsed = JSON.parse(storage.getItem(key) ?? "null");
    old = parsed && typeof parsed === "object" ? parsed : null;
  } catch { old = null; }
  const drop = () => { try { storage.removeItem(key); } catch { /* storage blocked */ } };
  if (!old) { drop(); return null; }
  let next = cast;
  let changed = false;
  for (const [id, voice] of Object.entries(old)) {
    if (typeof voice !== "string" || !voice) continue;
    const current = id === "narrator" ? cast.narrator : cast.characters[id];
    if (!current || current.voice) continue; // not a speaker of this book, or already set on the server
    next = withAssignment(next, id, { voice });
    changed = true;
  }
  let saved: Cast | null = null;
  if (changed) {
    try { saved = await save(book.id, next); } catch { return null; }
  }
  drop();
  return saved;
}

// ---------- animate button state ----------

/**
 * Runs one animation with the UI callbacks. A second call for the same face while it runs does nothing (the
 * button is disabled). Resolves with the animated face, or null when it failed (onError got a readable message).
 */
export async function runAnimate(
  id: string, running: ReadonlySet<string>,
  cb: { onStart: (id: string) => void; onEnd: (id: string) => void; onError: (id: string, message: string) => void },
  animate: (id: string, signal?: AbortSignal) => Promise<AnimatedFace>, signal?: AbortSignal,
): Promise<AnimatedFace | null> {
  if (running.has(id)) return null;
  cb.onStart(id);
  try {
    return await animate(id, signal);
  } catch (e) {
    if (!(e instanceof DOMException && e.name === "AbortError")) cb.onError(id, e instanceof Error ? e.message : String(e));
    return null;
  } finally {
    cb.onEnd(id);
  }
}
