/**
 * #79 group scene: one question, answered in turn by up to MAX_GROUP characters. The round itself runs in
 * TalkController (who answers when, cancel); these are the chat pieces: a bubble per answer, and the history each
 * character is given (their own chat, plus what the others just said in this round, so they can react).
 */
import { HISTORY_TURNS, priorTurns, type ChatMessage, type Turn } from "./chatLog";

/** A line of the history: a reader question, this character's reply, or (with `name`) another character's reply. */
export type HistoryTurn = Turn & { name?: string };

/** The server takes at most this many history lines (ChatTurn list, max_length 8). */
export const MAX_HISTORY_LINES = 8;
/** An earlier answer of the round is given back this short: enough to react to, cheap in the prompt. */
export const ROUND_LINE_CHARS = 300;

/** The next character of the round starts: the previous answer closes (dropped if it said nothing), a new one opens. */
export function addRoundAnswer(m: ChatMessage[], speakerId: string): ChatMessage[] {
  const via = m.findLast((x) => x.role === "user")?.via;
  const kept = m.filter((x) => !(x.pending && !x.text.trim())).map((x) => (x.pending ? { ...x, pending: false } : x));
  const id = (kept.length ? kept[kept.length - 1].id : m.length ? m[m.length - 1].id : 0) + 1;
  return [...kept, { id, role: "character", speakerId, text: "", ...(via ? { via } : {}), pending: true }];
}

/**
 * The answers already given in this round (`roundIds`, in order) by the others, as named history lines for `speakerId`.
 * The answer still being given and an empty one are left out.
 */
export function roundLines(m: ChatMessage[], roundIds: readonly number[], speakerId: string, nameOf: (id: string) => string): HistoryTurn[] {
  const out: HistoryTurn[] = [];
  for (const id of roundIds) {
    const x = m.find((y) => y.id === id);
    if (!x || x.role !== "character" || x.pending || x.speakerId === speakerId || !x.text.trim()) continue;
    out.push({ role: "character", name: nameOf(x.speakerId), text: x.text.trim().slice(0, ROUND_LINE_CHARS) });
  }
  return out;
}

/** What `speakerId` is given back: their own earlier chat, then this round's answers so far (the newest lines win). */
export function historyFor(m: ChatMessage[], speakerId: string, roundIds: readonly number[] = [], nameOf: (id: string) => string = (id) => id): HistoryTurn[] {
  const others = roundLines(m, roundIds, speakerId, nameOf);
  const own = priorTurns(m, speakerId, HISTORY_TURNS);
  return [...own, ...others].slice(-MAX_HISTORY_LINES);
}
