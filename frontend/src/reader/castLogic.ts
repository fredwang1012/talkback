import { saveCast, type AnimatedFace, type Assignment, type Book, type Cast, type Face, type Gender } from "../api";

/** Voice presets the server knows (kept in step with backend/app/voices.py). */
export const PRESETS = ["chloe", "eleanor", "jake", "marcus", "nora", "oliver"];
const FALLBACK_VOICE = "marcus";

/** Gender and a one-line description of each preset (from the casting prompt, backend/app/enrich/cast.py). */
export const VOICE_INFO: Record<string, { gender: Gender; blurb: string }> = {
  chloe: { gender: "female", blurb: "Friendly and clear" },
  eleanor: { gender: "female", blurb: "Calm and professional" },
  nora: { gender: "female", blurb: "Calm, a storyteller" },
  jake: { gender: "male", blurb: "Energetic and dramatic" },
  marcus: { gender: "male", blurb: "Enthusiastic, a professor" },
  oliver: { gender: "male", blurb: "Calm and thoughtful" },
};
/** The short line every voice preview speaks: one fixed text per voice, so it is synthesised once and then cached. */
export const PREVIEW_LINE = "Hello. This is how I sound when I read this book to you.";
/** Two-voice defaults when the server sent none (backend cast.DEFAULT_SLOT_VOICES). */
const DEFAULT_SLOTS: Record<Gender, string> = { male: "jake", female: "chloe" };
const STAND_INS: Record<string, Gender> = { minor_m: "male", minor_f: "female" }; // backend enrich/minor.py

// ---------- per-book choices kept in the browser ----------

/** voice: null = Auto. face: null = Auto, "none" = no face, else a face id. */
export type TalkChoice = { voice: string | null; face: string | null };

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

/** Talkback's Voice / Avatar picks per "Talk to" character id (#106). A character with no entry is on Auto. */
export type TalkChoices = Record<string, TalkChoice>;
const AUTO: TalkChoice = { voice: null, face: null };
const isAuto = (c: TalkChoice) => c.voice === null && c.face === null;

/** The picks for a book (this browser). The old single pick, which applied to every character, becomes the narrator's. */
export function loadTalkChoices(bookId: string): TalkChoices {
  const o = readJson(talkKey(bookId));
  if (!o) return {};
  const by = o.bySpeaker;
  if (by === undefined) { // before #106: { voice, face } for whoever was talked to
    const legacy = { voice: str(o.voice), face: str(o.face) };
    return isAuto(legacy) ? {} : { narrator: legacy };
  }
  const out: TalkChoices = {};
  if (by && typeof by === "object") {
    for (const [id, v] of Object.entries(by as Record<string, unknown>)) {
      if (!v || typeof v !== "object") continue;
      const c = { voice: str((v as Record<string, unknown>).voice), face: str((v as Record<string, unknown>).face) };
      if (!isAuto(c)) out[id] = c;
    }
  }
  return out;
}
export const saveTalkChoices = (bookId: string, cs: TalkChoices) => writeJson(talkKey(bookId), { bySpeaker: cs });

/** The pick for one character (Auto when there is none). */
export const choiceFor = (cs: TalkChoices, id: string): TalkChoice => cs[id] ?? AUTO;

/** `cs` with one character's pick changed; back to Auto drops the entry. */
export function withTalkChoice(cs: TalkChoices, id: string, c: TalkChoice): TalkChoices {
  const next = { ...cs };
  if (isAuto(c)) delete next[id];
  else next[id] = { voice: c.voice, face: c.face };
  return next;
}

/** After a voice or face is deleted, browser-side choices that pointed at it fall back to Auto (null). */
export function dropReferences(talk: TalkChoice, gone: { voice?: string; face?: string }): TalkChoice {
  return {
    voice: gone.voice && talk.voice === gone.voice ? null : talk.voice,
    face: gone.face && talk.face === gone.face ? null : talk.face,
  };
}

/** dropReferences for every character's pick. */
export function dropTalkReferences(cs: TalkChoices, gone: { voice?: string; face?: string }): TalkChoices {
  let next: TalkChoices = {};
  for (const [id, c] of Object.entries(cs)) next = withTalkChoice(next, id, dropReferences(c, gone));
  return next;
}

// ---------- resolving voices and faces ----------

export const emptyCast = (): Cast => ({ main: [], narrator: { voice: null, face: null }, characters: {} });

/** The book's own voice for a speaker, ignoring the cast. */
export function bookVoice(book: Book, id: string): string {
  return (id === "narrator" ? book.narrator?.voice : book.characters.find((c) => c.id === id)?.voice) ?? FALLBACK_VOICE;
}

export const isNovel = (book: Book) => book.kind === "novel";

// ---------- novels: a male and a female voice, each with a face; read in one or both (#93) ----------

/** "two" (the default): each speaker reads in the voice of their gender. "one": every line in the narrator's voice. */
export type VoiceMode = "one" | "two";

/** The mode of a cast. The removed Full cast ("cast"), a missing or an unknown value all read in two voices. */
export const voiceMode = (cast: Cast): VoiceMode => (cast.mode === "one" ? "one" : "two");
export const withVoiceMode = (cast: Cast, mode: VoiceMode): Cast => ({ ...cast, mode });

/** Which of the two voices narrates: the reader's pick, else the gender of the book's narrator voice. */
export function narratorGender(cast: Cast): Gender {
  return cast.narrator_gender ?? cast.defaults?.narrator_gender ?? "male";
}

/** The voice a speaker reads in: the reader's override, else the inferred gender; unknown speakers (and everyone in
 * one-voice mode) read in the narrator's voice. */
export function speakerGender(cast: Cast, id: string): Gender {
  if (id === "narrator" || voiceMode(cast) === "one") return narratorGender(cast);
  return STAND_INS[id] ?? cast.genders?.[id] ?? cast.auto_genders?.[id] ?? narratorGender(cast);
}

export function slotVoice(cast: Cast, g: Gender): string {
  return cast[g]?.voice ?? cast.defaults?.[g] ?? DEFAULT_SLOTS[g];
}
export const slotFace = (cast: Cast, g: Gender): string | null => cast[g]?.face ?? null;

/** The reading voice: a novel's voice for the speaker (speakerGender); a textbook's one narrator voice. */
export function castVoice(book: Book, cast: Cast, id: string): string {
  if (isNovel(book)) return slotVoice(cast, speakerGender(cast, id));
  return cast.narrator?.voice ?? bookVoice(book, "narrator");
}
export function castFace(book: Book, cast: Cast, id: string): string | null {
  if (isNovel(book)) return slotFace(cast, speakerGender(cast, id));
  return cast.narrator?.face ?? null;
}

/** `cast` with one of the two voices changed (voice and/or face). */
export function withSlot(cast: Cast, g: Gender, patch: Partial<Assignment>): Cast {
  return { ...cast, [g]: { voice: null, face: null, ...cast[g], ...patch } };
}
/** `cast` with a character's gender overridden, or back to the inferred one (null). */
export function withGender(cast: Cast, id: string, g: Gender | null): Cast {
  const { [id]: _old, ...rest } = cast.genders ?? {};
  void _old;
  return { ...cast, genders: g ? { ...rest, [id]: g } : rest };
}

/** Speakers whose gender the reader may override: every character except the generic stand-ins. */
export function genderRows(book: Book, cast: Cast): { id: string; name: string; auto: Gender | null; override: Gender | null }[] {
  return book.characters.filter((c) => !(c.id in STAND_INS))
    .map((c) => ({ id: c.id, name: c.name, auto: cast.auto_genders?.[c.id] ?? null, override: cast.genders?.[c.id] ?? null }));
}

/** Roving focus in a radio group: the option index an arrow / Home / End key moves to, or null for other keys. */
export function radioKeyTarget(key: string, index: number, count: number): number | null {
  if (count <= 0) return null;
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/** Id of the face to show while a chunk by `speaker` is read, or null. */
export function readingFaceId(book: Book, cast: Cast, speaker: string): string | null {
  return castFace(book, cast, isNovel(book) ? speaker : "narrator");
}

export function talkVoice(book: Book, cast: Cast, choice: TalkChoice, id: string): string {
  return choice.voice ?? castVoice(book, cast, id);
}

/** Auto = the face of the character's reading voice, else the face bound to the voice. */
export function talkFaceId(book: Book, cast: Cast, choice: TalkChoice, id: string, voice: string, faces: Face[]): string | null {
  if (choice.face === "none") return null;
  if (choice.face) return choice.face;
  return castFace(book, cast, id) ?? faces.find((f) => f.voice === voice)?.id ?? null;
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
