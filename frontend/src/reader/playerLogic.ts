/**
 * The avatar player (#33): the face as a podcast-style mini player while reading, expanded to a near full-screen view,
 * or hidden behind a small tab. Pure parts: the remembered mode, the keyboard and swipe rules, the answer bubble (#34)
 * and the caption of the sentence being read.
 */
import { useState } from "react";
import type { Book } from "../api";
import type { ChatMessage } from "../talk/chatLog";
import type { FlatChunk } from "./position";

export type PlayerMode = "mini" | "expanded" | "hidden";
const MODES: PlayerMode[] = ["mini", "expanded", "hidden"];
const KEY = "talkback:player";

/** The mode this device last left the player in (mini by default; storage may be blocked). */
export function loadPlayerMode(): PlayerMode {
  try {
    const v = localStorage.getItem(KEY);
    return MODES.includes(v as PlayerMode) ? (v as PlayerMode) : "mini";
  } catch { return "mini"; }
}

export function savePlayerMode(m: PlayerMode): void {
  try { localStorage.setItem(KEY, m); } catch { /* storage can be blocked */ }
}

/** What a key does to the player: Esc collapses the expanded view; anything else leaves it alone (null). */
export function playerKey(mode: PlayerMode, key: string): PlayerMode | null {
  return mode === "expanded" && (key === "Escape" || key === "Esc") ? "mini" : null;
}

/**
 * Whether going from `from` to `to` (or into the Talkback view) must stop the answer being given: leaving the expanded
 * view releases its live face (#140), so a live answer on it stops. Talkback keeps the live face; clips play on anyway.
 */
export function leavingStopsAnswer(from: PlayerMode, to: PlayerMode | "talkback", now: { live: boolean; answering: boolean }): boolean {
  return from === "expanded" && to !== "expanded" && to !== "talkback" && now.live && now.answering;
}

/** The portrait the player shows for `who`: a character's own (none: the letter circle), the narrator's for the narrator. */
export function playerPortrait(book: Pick<Book, "narrator" | "characters">, who: string): string | null {
  return who === "narrator" ? book.narrator?.portrait ?? null : book.characters.find((c) => c.id === who)?.portrait ?? null;
}

export type Point = { x: number; y: number; t: number };
/** A downward swipe (collapses the expanded view): mostly vertical, at least 80 px, within a second. */
export function isSwipeDown(from: Point, to: Point): boolean {
  const dy = to.y - from.y;
  const dx = Math.abs(to.x - from.x);
  return dy >= 80 && dy > dx * 1.5 && to.t - from.t <= 1000;
}

export type Exchange = { replyId: number; speakerId: string; question: string; answer: string; pending: boolean };

/**
 * The latest question asked after message `sinceId` (older chat is not news) and the reply to it so far, for the
 * answer bubble shown while reading. Null when nothing was asked since.
 */
export function latestExchange(messages: ChatMessage[], sinceId: number): Exchange | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const q = messages[i];
    if (q.id <= sinceId) return null;
    if (q.role !== "user") continue;
    const reply = messages.slice(i + 1).find((m) => m.role === "character");
    if (!reply) return null;
    return { replyId: reply.id, speakerId: reply.speakerId, question: q.text, answer: reply.text, pending: !!reply.pending };
  }
  return null;
}

/**
 * The `sinceId` for `latestExchange`: the last chat message from before this visit (the chat as first loaded). Clear
 * chat empties it and the ids start again at 1, so from then on everything is news (0).
 */
export function useChatSince(messages: ChatMessage[]): number {
  const [since, setSince] = useState(() => messages.at(-1)?.id ?? 0);
  if (since > 0 && messages.length === 0) setSince(0); // adjusted while rendering: the cleared chat's first bubble shows
  return since;
}

/** The text of the sentence being read (the caption of the expanded view). */
export function sentenceText(book: Pick<Book, "paragraphs">, chunk: FlatChunk | undefined): string {
  if (!chunk) return "";
  const p = book.paragraphs[chunk.para]?.idx === chunk.para ? book.paragraphs[chunk.para] : book.paragraphs.find((x) => x.idx === chunk.para);
  const t = p?.text.slice(chunk.char_start, chunk.char_end).trim();
  return t || chunk.tts_text.replace(/\[[^\]]*\]/g, "").trim();
}
