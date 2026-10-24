/** The Talkback conversation, kept per book for the session. Pure functions plus a small in-memory store. */

export type ChatMessage = {
  id: number;
  role: "user" | "character" | "narrator"; // "narrator": a recap read out by the narrator (never sent to the model)
  speakerId: string; // who answered (role "character"); who was asked (role "user")
  text: string;
  via?: "voice" | "typed";
  pending?: boolean; // an answer still streaming in
  recap?: string; // role "narrator": what was recapped, e.g. "Last chapter: The Pool of Tears"
  loading?: boolean; // role "narrator": the recap is still being fetched
  quiet?: boolean; // role "narrator": nothing to recap (a note, not read aloud)
};

export const MAX_MESSAGES = 50;

const cap = (m: ChatMessage[]): ChatMessage[] => (m.length > MAX_MESSAGES ? m.slice(m.length - MAX_MESSAGES) : m);
const nextId = (m: ChatMessage[]) => (m.length ? m[m.length - 1].id + 1 : 1);

/** A question, followed by an empty pending answer from `speakerId` that sentences are appended to. */
export function addQuestion(m: ChatMessage[], text: string, speakerId: string, via: "voice" | "typed"): ChatMessage[] {
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

/** Append a sentence to the pending answer; ignored when none is pending. */
export function appendSentence(m: ChatMessage[], sentence: string): ChatMessage[] {
  const i = m.findLastIndex((x) => x.pending);
  if (i < 0 || !sentence.trim()) return m;
  const t = m[i].text;
  return m.map((x, j) => (j === i ? { ...x, text: t ? `${t} ${sentence.trim()}` : sentence.trim() } : x));
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
    .filter((x) => x.role !== "narrator" && x.speakerId === speakerId && !x.pending && x.text.trim())
    .slice(-n)
    .map((x) => ({ role: x.role as Turn["role"], text: x.text.trim().slice(0, HISTORY_CHARS) }));
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
      const m = cap(v.filter((x): x is ChatMessage => x && typeof x.text === "string" && (x.role === "user" || x.role === "character" || x.role === "narrator"))
        .filter((x) => !x.loading).map((x) => ({ ...x, pending: false })));
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
