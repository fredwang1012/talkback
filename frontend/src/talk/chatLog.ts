/** The Talkback conversation, kept per book for the session. Pure functions plus a small in-memory store. */

export type ChatMessage = {
  id: number;
  role: "user" | "character";
  speakerId: string; // who answered (role "character"); who was asked (role "user")
  text: string;
  via?: "voice" | "typed";
  pending?: boolean; // an answer still streaming in
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

// ---- per-book store (memory, mirrored to sessionStorage) ----

const mem = new Map<string, ChatMessage[]>();
const key = (bookId: string) => `talkback:chat:${bookId}`;

export function loadChat(bookId: string): ChatMessage[] {
  const hit = mem.get(bookId);
  if (hit) return hit;
  try {
    const v = JSON.parse(sessionStorage.getItem(key(bookId)) ?? "null");
    if (Array.isArray(v)) {
      const m = cap(v.filter((x): x is ChatMessage => x && typeof x.text === "string" && (x.role === "user" || x.role === "character")).map((x) => ({ ...x, pending: false })));
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
