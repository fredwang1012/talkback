export type Segment = { speaker: string; text: string; emotion: string | null };
export type TtsChunk = {
  id: string;
  speaker: string;
  voice: string;
  tts_text: string;
  char_start: number;
  char_end: number;
};
export type Paragraph = {
  idx: number;
  section_id: string;
  text: string;
  segments: Segment[];
  tts_chunks: TtsChunk[];
};
export type Character = {
  id: string;
  name: string;
  aliases: string[];
  first_para: number;
  voice: string;
  voice_tags: string;
  persona: string;
  portrait: string | null;
};
export type Book = {
  id: string;
  title: string;
  author: string;
  kind: "novel" | "textbook";
  license: string;
  attribution: string | null;
  sections: { id: string; title: string; start_para: number }[];
  paragraphs: Paragraph[];
  characters: Character[];
  narrator: { id: string; voice: string; is_character: string | null } | null;
};
export type BookSummary = {
  id: string;
  title: string;
  author: string;
  kind: "novel" | "textbook";
  license: string;
  attribution: string | null;
  paragraphs: number;
  sections: number;
  characters: number;
};
export type Health = { ok: boolean; boson_key_set: boolean };

/** Only needed when the server sets BOOKSON_ACCESS_TOKEN (public deployments). Read access never needs it. */
export function authHeaders(): Record<string, string> {
  const token = import.meta.env.VITE_BOOKSON_TOKEN as string | undefined;
  return token ? { "X-Bookson-Token": token } : {};
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchBooks = () => getJson<BookSummary[]>("/api/books");
export const fetchBook = (id: string) => getJson<Book>(`/api/books/${encodeURIComponent(id)}`);
export const fetchHealth = () => getJson<Health>("/api/health");

export type WordTiming = { start: number; end: number; char_start: number; char_end: number };
export type TtsResponse = { chunk_id: string; voice: string; audio_url: string; timestamps: WordTiming[] | null; cached: boolean };

/** Throws an Error carrying the server's readable message (e.g. a missing key or a billing refusal). */
export async function fetchTts(bookId: string, chunkId: string, voiceOverride?: string): Promise<TtsResponse> {
  const res = await fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, chunk_id: chunkId, voice_override: voiceOverride ?? null }),
  });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Audio request failed (${res.status})`);
  }
  return res.json() as Promise<TtsResponse>;
}

export type RetrieveResponse = {
  passages: { para_start: number; para_end: number; text: string }[];
  story_so_far: string;
  instructions: string;
  persona: string | null;
  reader_para: number;
};

export async function fetchRetrieve(bookId: string, query: string, readerPara: number, speakerId: string): Promise<RetrieveResponse> {
  const res = await fetch("/api/retrieve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId }),
  });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Lookup failed (${res.status})`);
  }
  return res.json() as Promise<RetrieveResponse>;
}

/** A fresh ephemeral Realtime key for one connection. Never cache the result. */
export async function fetchRealtimeKey(): Promise<string> {
  const res = await fetch("/api/realtime/token", { method: "POST", headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Could not start a voice session (${res.status})`);
  }
  return ((await res.json()) as { value: string }).value;
}
