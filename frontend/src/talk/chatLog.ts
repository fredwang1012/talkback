/** The Talkback conversation, kept per book for the session. Pure functions plus a small in-memory store. */
import { cleanSources, cleanSuggestions, type Grounding, type Source } from "../api";

export type ChatMessage = {
  id: number;
  role: "user" | "character" | "narrator" | "tool"; // "narrator": a recap read out by the narrator; "tool": a chip for an action the character took (#72); neither is sent to the model
  speakerId: string; // who answered (role "character"); who was asked (role "user")
  text: string;
  via?: "voice" | "typed";
  pending?: boolean; // an answer still streaming in
  recap?: string; // role "narrator": what was recapped, e.g. "Last chapter: The Pool of Tears"
  loading?: boolean; // role "narrator": the recap is still being fetched
  quiet?: boolean; // role "narrator": nothing to recap (a note, not read aloud)
  interrupted?: boolean; // role "character": the reader cut this answer off (barge-in, #10); the text is what was heard
  sources?: Source[]; // role "character": web pages a grounded answer used (shown as links, never spoken or sent back)
  searchSuggestions?: string; // role "character": Google's Search Suggestions markup of a grounded answer (sandboxed frame)
};

export const MAX_MESSAGES = 50;

const cap = (m: ChatMessage[]): ChatMessage[] => (m.length > MAX_MESSAGES ? m.slice(m.length - MAX_MESSAGES) : m);
const nextId = (m: ChatMessage[]) => (m.length ? m[m.length - 1].id + 1 : 1);

/** A question, followed by an empty pending answer from `speakerId` that sentences are appended to. */
export function addQuestion(m: ChatMessage[], text: string, speakerId: string, via: "voice" | "typed"): ChatMessage[] {
  if (!text.trim()) return m; // never an empty question bubble
  const id = nextId(m);
  return cap([
    ...m.map((x) => (x.pending ? { ...x, pending: false } : x)),
    { id, role: "user", speakerId, text, via },
    { id: id + 1, role: "character", speakerId, text: "", via, pending: true },
  ]);
}

/** A recap from the narrator, shown at once while it loads (`fillNarration` sets its text). Not a turn of the conversation. */
export function addNarration(m: ChatMessage[], recap: string): ChatMessage[] {
  return cap([...m, { id: nextId(m), role: "narrator", speakerId: "narrator", text: "", recap, loading: true }]);
}

/** The recap arrived: its text (and heading); `null` drops the message (the recap could not be loaded). */
export function fillNarration(m: ChatMessage[], id: number, text: string | null, recap?: string, quiet = false): ChatMessage[] {
  if (text === null) return m.filter((x) => x.id !== id);
  return m.map((x) => (x.id === id ? { ...x, text, loading: false, ...(recap ? { recap } : {}), ...(quiet ? { quiet } : {}) } : x));
}

/** A chip for a tool the character ran ("Jumped to Chapter 3"), placed before the answer still being spoken. */
export function addToolChip(m: ChatMessage[], text: string): ChatMessage[] {
  const chip: ChatMessage = { id: nextId(m), role: "tool", speakerId: "tool", text };
  const i = m.findLastIndex((x) => x.pending);
  return cap(i < 0 ? [...m, chip] : [...m.slice(0, i), chip, ...m.slice(i)]);
}

/**
 * The reader barged in: the pending answer is marked as cut off (its text stays as what was heard) and closed at once,
 * so late text of the cancelled answer can never land in it; one with no text yet is removed (no empty bubble).
 */
export function markInterrupted(m: ChatMessage[]): ChatMessage[] {
  const i = m.findLastIndex((x) => x.pending);
  if (i < 0) return m;
  if (!m[i].text.trim()) return m.filter((_, j) => j !== i);
  return m.map((x, j) => (j === i ? { ...x, interrupted: true, pending: false } : x));
}

/**
 * The reply the reader cut off (#135) keeps only the words actually heard (from the voice-timed caption at the moment
 * of the cut), so the bubble and the character's memory (priorTurns) match what was said. The target is the newest
 * character reply that was interrupted, or that is still pending with text (Stop); a new empty pending reply (a typed
 * question asked over the old one) is skipped. An empty `heard` changes nothing.
 */
export function cutHeard(m: ChatMessage[], heard: string): ChatMessage[] {
  const text = heard.trim();
  if (!text) return m;
  // the newest reply (skipping a new empty pending one): never an older interrupted reply
  const i = m.findLastIndex((x) => x.role === "character" && !(x.pending && !x.text.trim()));
  if (i < 0 || !(m[i].interrupted || m[i].pending)) return m;
  return m.map((x, j) => (j === i ? { ...x, text } : x));
}

/** Replace the text of answer `id`, only while it is still pending (a closed or interrupted one never changes). */
export function setTextOf(m: ChatMessage[], id: number | undefined, text: string): ChatMessage[] {
  const i = id === undefined ? -1 : m.findIndex((x) => x.id === id && x.pending);
  if (i < 0) return m;
  return m.map((x, j) => (j === i ? { ...x, text } : x));
}

/** Append a sentence to answer `id`, only while it is still pending. */
export function appendSentenceTo(m: ChatMessage[], id: number | undefined, sentence: string): ChatMessage[] {
  const i = id === undefined ? -1 : m.findIndex((x) => x.id === id && x.pending);
  if (i < 0 || !sentence.trim()) return m;
  const t = m[i].text;
  return m.map((x, j) => (j === i ? { ...x, text: t ? `${t} ${sentence.trim()}` : sentence.trim() } : x));
}

/** Append a sentence to the pending answer; ignored when none is pending. */
export function appendSentence(m: ChatMessage[], sentence: string): ChatMessage[] {
  const i = m.findLastIndex((x) => x.pending);
  if (i < 0 || !sentence.trim()) return m;
  const t = m[i].text;
  return m.map((x, j) => (j === i ? { ...x, text: t ? `${t} ${sentence.trim()}` : sentence.trim() } : x));
}

/** The web sources and Google's Search Suggestions of the pending answer (a grounded answer's last line); ignored when none is pending. */
export function setSources(m: ChatMessage[], g: Grounding): ChatMessage[] {
  const i = m.findLastIndex((x) => x.pending);
  const clean = cleanSources(g.sources);
  const html = cleanSuggestions(g.suggestions);
  if (i < 0 || (!clean.length && !html)) return m;
  return m.map((x, j) => (j === i ? { ...x, ...(clean.length ? { sources: clean } : {}), ...(html ? { searchSuggestions: html } : {}) } : x));
}

/** Replace the pending answer's text (the realtime path reports the whole caption so far). */
export function setPendingText(m: ChatMessage[], text: string): ChatMessage[] {
  const i = m.findLastIndex((x) => x.pending);
  if (i < 0) return m;
  return m.map((x, j) => (j === i ? { ...x, text } : x));
}

/** The answer is over: stop streaming; an answer that never produced text is dropped. */
export function finishAnswer(m: ChatMessage[]): ChatMessage[] {
  if (!m.some((x) => x.pending)) return m;
  return m.filter((x) => !(x.pending && !x.text)).map((x) => (x.pending ? { ...x, pending: false } : x));
}

export type Turn = { role: "user" | "character"; text: string };
/** Appended to a cut-off reply in the history, so the next answer knows the reader did not hear the rest (#10). */
export const INTERRUPTED_MARK = " [the reader interrupted here]";
export const HISTORY_TURNS = 6;
const HISTORY_CHARS = 500;

/**
 * The finished turns of the chat with `speakerId` (their questions and replies), oldest first, for a character that
 * must remember them. Text only, capped in count and length. A pending answer and the question it belongs to are left out:
 * that is the turn being asked right now. Narrator recaps are left out too: they are not something the reader said or
 * the character answered, and the answer prompt already carries the spoiler-safe story so far.
 */
export function priorTurns(m: ChatMessage[], speakerId: string, n = HISTORY_TURNS): Turn[] {
  let end = m.length;
  if (end && m[end - 1].pending) end = end >= 2 && m[end - 2].role === "user" ? end - 2 : end - 1;
  return m.slice(0, end)
    .filter((x) => (x.role === "user" || x.role === "character") && x.speakerId === speakerId && !x.pending && x.text.trim())
    .slice(-n)
    .map((x) => ({ role: x.role as Turn["role"], text: x.text.trim().slice(0, HISTORY_CHARS) + (x.interrupted ? INTERRUPTED_MARK : "") }));
}

// ---- per-book store (memory, mirrored to sessionStorage) ----

const mem = new Map<string, ChatMessage[]>();
const key = (bookId: string) => `talkback:chat:${bookId}`;

export function loadChat(bookId: string): ChatMessage[] {
  const hit = mem.get(bookId);
  if (hit) return hit;
  try {
    const v = JSON.parse(sessionStorage.getItem(key(bookId)) ?? "null");
    if (Array.isArray(v)) {
      const m = cap(v.filter((x): x is ChatMessage => x && typeof x.text === "string" && (x.role === "user" || x.role === "character" || x.role === "narrator" || x.role === "tool"))
        .filter((x) => !x.loading).map((x) => {
          const { sources, searchSuggestions, ...rest } = x;
          const clean = cleanSources(sources);
          const html = cleanSuggestions(searchSuggestions);
          return { ...rest, pending: false, ...(clean.length ? { sources: clean } : {}), ...(html ? { searchSuggestions: html } : {}) };
        }));
      mem.set(bookId, m);
      return m;
    }
  } catch { /* storage blocked or garbled */ }
  return [];
}

export function saveChat(bookId: string, m: ChatMessage[]): void {
  mem.set(bookId, m);
  try {
    if (m.length) sessionStorage.setItem(key(bookId), JSON.stringify(m)); else sessionStorage.removeItem(key(bookId));
  } catch { /* storage blocked: memory still holds it */ }
}

/** For tests. */
export function resetChatMemory(): void { mem.clear(); }
