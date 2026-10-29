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

/**
 * Who a question can be routed to by name (#34, talk/route): who the open view offers. Read mode never applies the
 * Talkback spoiler setting (#94: its Stage shows the unmet as locked), so routing there never picks one of them.
 */
export function routeCandidates(book: Book, para: number, spoilers: boolean, mode: Mode): { id: string; name: string; aliases?: string[] }[] {
  return speakerChoices(book, para, mode === "talk" && spoilers && book.kind === "novel")
    .map((c) => ({ ...c, aliases: book.characters.find((x) => x.id === c.id)?.aliases }));
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

type Timers = { set: (fn: () => void, ms: number) => unknown; clear: (h: unknown) => void };
const realTimers: Timers = { set: (fn, ms) => setTimeout(fn, ms), clear: (h) => clearTimeout(h as ReturnType<typeof setTimeout>) };

/**
 * Keeps the Talkback status line still during an answer (#81). The raw status follows the audio, which stops between
 * sentences (the next one is still being synthesised, or a replay moves to its next clip), so the line flickered
 * between "Speaking…" and "Thinking…" or "Ready…". Here:
 * - once an answer is speaking it stays "speaking" until the answer ends (`answering` false);
 * - a drop to "idle" waits `holdMs`, and is cancelled if anything else comes first (a short gap is not an end);
 * - every other change (listening, thinking, speaking) shows at once.
 */
export function createStatusGate(set: (s: TalkbackStatus) => void, holdMs = 600, timers: Timers = realTimers) {
  let shown: TalkbackStatus | null = null;
  let pending: unknown = null;
  const cancel = () => { if (pending !== null) { timers.clear(pending); pending = null; } };
  const show = (s: TalkbackStatus) => { cancel(); if (s !== shown) { shown = s; set(s); } };
  return {
    update(raw: TalkbackStatus, answering: boolean) {
      if (raw === "thinking" && answering && shown === "speaking") { cancel(); return; } // a pause between sentences
      if (raw === "idle" && shown !== null && shown !== "idle" && shown !== "listening") {
        if (pending === null) pending = timers.set(() => { pending = null; show("idle"); }, holdMs);
        return;
      }
      show(raw);
    },
    reset(s: TalkbackStatus) { show(s); },
    dispose: cancel,
  };
}

/** Whether a Space key press should start hold-to-talk (not while typing in a field or pressing a button). */
export function spaceStartsTalk(e: { code: string; repeat: boolean; target: { tagName?: string } | null }): boolean {
  if (e.code !== "Space" || e.repeat) return false;
  return !["SELECT", "INPUT", "TEXTAREA", "BUTTON"].includes(e.target?.tagName ?? "");
}
