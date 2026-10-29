/** #79 group scene: who may join a question to the character talked to, and the picks (in the order picked). */
import type { Book } from "../api";
import { MAX_GROUP } from "../talk/TalkController";
import { speakerChoices } from "./talkbackLogic";

/** The others that can be added: the same rule as the Talk to picker (met so far, everyone with spoilers on), minus the one talked to. */
export function groupChoices(book: Book, para: number, spoilers: boolean, speakerId: string): { id: string; name: string }[] {
  return speakerChoices(book, para, spoilers).filter((c) => c.id !== speakerId);
}

/** Add `id` at the end, or take it out; full (MAX_GROUP with the one talked to) adds nothing. */
export function togglePick(picks: readonly string[], id: string): string[] {
  if (picks.includes(id)) return picks.filter((p) => p !== id);
  return picks.length >= MAX_GROUP - 1 ? [...picks] : [...picks, id];
}

/** The picks still allowed now (spoilers turned off, an earlier place, a new character talked to drop out), in order. */
export function validPicks(picks: readonly string[], choices: readonly { id: string }[]): string[] {
  return picks.filter((id) => choices.some((c) => c.id === id)).slice(0, MAX_GROUP - 1);
}
