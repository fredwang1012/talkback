import type { Book, Character } from "../api";
import type { TalkState } from "../talk/TalkController";
import { splitCharacters } from "./position";
import { cleanTitle } from "./cleanTitle";

export type Mode = "read" | "talk";
export const toggleMode = (m: Mode): Mode => (m === "read" ? "talk" : "read");

/** The chapter (section) a paragraph belongs to, or null for a book with no sections. */
export function chapterAt(book: Pick<Book, "sections">, para: number): string | null {
  let title: string | null = null;
  for (const s of book.sections) if (s.start_para <= para) title = cleanTitle(s.title); else break;
  return title ?? (book.sections[0] ? cleanTitle(book.sections[0].title) : null);
}

/**
 * Who can be asked in Talkback. Textbooks talk to the narrator only (no dropdown, `choices` is empty).
 * Novels: the narrator plus the characters met so far, or everyone when spoilers are on.
 */
export function speakerChoices(book: Book, para: number, spoilers: boolean): { id: string; name: string }[] {
  if (book.kind === "textbook") return [];
  const { met } = splitCharacters(book.characters, para, spoilers);
  return [{ id: "narrator", name: "Narrator" }, ...met.map((c: Character) => ({ id: c.id, name: c.name }))];
}

/** The speaker to keep: the current one if still offered, else the narrator (spoilers off, position moved back, no longer main). */
export function validSpeaker(book: Book, para: number, spoilers: boolean, id: string): string {
  if (id === "narrator") return id;
  return speakerChoices(book, para, spoilers && book.kind === "novel").some((c) => c.id === id) ? id : "narrator";
}

export const showSpoilerToggle = (book: Pick<Book, "kind">) => book.kind === "novel";

export function speakerName(book: Book, id: string): string {
  if (id === "narrator") return book.kind === "textbook" ? "Tutor" : "Narrator";
  return book.characters.find((c) => c.id === id)?.name ?? id;
}

export type TalkbackStatus = "idle" | "listening" | "thinking" | "speaking";

export function talkbackStatus(state: TalkState, speaking: boolean): TalkbackStatus {
  if (speaking) return "speaking";
  if (state === "listening") return "listening";
  if (["transcribing", "retrieving", "answering"].includes(state)) return "thinking";
  return "idle";
}

/** Whether a Space key press should start hold-to-talk (not while typing in a field or pressing a button). */
export function spaceStartsTalk(e: { code: string; repeat: boolean; target: { tagName?: string } | null }): boolean {
  if (e.code !== "Space" || e.repeat) return false;
  return !["SELECT", "INPUT", "TEXTAREA", "BUTTON"].includes(e.target?.tagName ?? "");
}
