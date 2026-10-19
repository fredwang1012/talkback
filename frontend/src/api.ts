import { getAccessToken } from "./accessToken";

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
  narrator: { id: string; voice: string; is_character: string | null; portrait: string | null } | null;
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
  cover: string | null;
};
export type Health = { ok: boolean; boson_key_set: boolean; access_required: boolean };

/** Sent when the server requires an access code (TALKBACK_ACCESS_TOKEN). Typed in by the visitor, never built in. */
export function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { "X-Talkback-Token": token } : {};
}

/** fetch with a deadline, so a stalled server shows an error instead of an endless spinner. */
export async function timedFetch(url: string, init: RequestInit = {}, timeoutMs = 15000): Promise<Response> {
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeoutMs);
  // a caller-supplied signal (barge-in) aborts the request too, and keeps its own AbortError
  const outer = init.signal;
  const onOuterAbort = () => ctl.abort();
  if (outer) { if (outer.aborted) ctl.abort(); else outer.addEventListener("abort", onOuterAbort); }
  try {
    return await fetch(url, { ...init, signal: ctl.signal });
  } catch (e) {
    if (timedOut) throw new Error("The server took too long to respond. Check that the backend is running.");
    throw e;
  } finally {
    clearTimeout(timer);
    outer?.removeEventListener("abort", onOuterAbort);
  }
}

async function getJson<T>(url: string): Promise<T> {
  const res = await timedFetch(url);
  if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchBooks = () => getJson<BookSummary[]>("/api/books");
export const fetchBook = (id: string) => getJson<Book>(`/api/books/${encodeURIComponent(id)}`);
export const fetchHealth = () => getJson<Health>("/api/health");

export type WordTiming = { start: number; end: number; char_start: number; char_end: number };
export type TtsResponse = { chunk_id: string; voice: string; audio_url: string; timestamps: WordTiming[] | null; cached: boolean };

/** Throws an Error carrying the server's readable message (e.g. a missing key or a billing refusal). */
export async function fetchTts(bookId: string, chunkId: string, voiceOverride?: string, cachedOnly = false): Promise<TtsResponse> {
  const res = await timedFetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, chunk_id: chunkId, voice_override: voiceOverride ?? null, cached_only: cachedOnly }),
  }, 60000); // a cache miss synthesises speech, which can take a while
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
  const res = await timedFetch("/api/retrieve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId }),
  }, 10000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Lookup failed (${res.status})`);
  }
  return res.json() as Promise<RetrieveResponse>;
}

/**
 * Streams the spoken answer from /api/answer (NDJSON: `sentence` lines, then `done` or `error`).
 * Only the response headers have a deadline; the body can take as long as the model needs. `signal` aborts it.
 */
export async function streamAnswer(
  bookId: string, query: string, readerPara: number, speakerId: string,
  onSentence: (text: string) => void, signal?: AbortSignal,
): Promise<void> {
  const res = await timedFetch("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId }),
    signal,
  }, 20000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Answer request failed (${res.status})`);
  }
  if (!res.body) throw new Error("The answer stream was empty.");
  const reader = res.body.getReader();
  // timedFetch stops watching the signal once headers arrive, so cancel the body ourselves on barge-in
  const onAbort = () => { reader.cancel().catch(() => {}); };
  if (signal?.aborted) onAbort(); else signal?.addEventListener("abort", onAbort);
  const decoder = new TextDecoder();
  let buf = "";
  const handle = (line: string) => {
    if (!line.trim()) return;
    let msg: { type?: string; text?: string; message?: string };
    try { msg = JSON.parse(line); } catch { return; } // tolerate a garbled line
    if (msg.type === "sentence" && typeof msg.text === "string") onSentence(msg.text);
    else if (msg.type === "error") throw new Error(msg.message || "The answer failed.");
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (signal?.aborted) throw new DOMException("aborted", "AbortError");
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        handle(line);
      }
    }
    buf += decoder.decode();
    handle(buf); // a last line without a newline
  } finally {
    signal?.removeEventListener("abort", onAbort);
    reader.cancel().catch(() => {}); // always release the connection, whatever the exit path
  }
}

/** Synthesises one sentence in a voice (cached server-side) and returns where to fetch the audio. */
export async function speak(text: string, voice: string): Promise<TtsResponse> {
  const res = await timedFetch("/api/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ text, voice }),
  }, 30000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Speech request failed (${res.status})`);
  }
  return res.json() as Promise<TtsResponse>;
}

/** A fresh ephemeral Realtime key for one connection. Never cache the result. */
export async function fetchRealtimeKey(): Promise<string> {
  const res = await timedFetch("/api/realtime/token", { method: "POST", headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Could not start a voice session (${res.status})`);
  }
  return ((await res.json()) as { value: string }).value;
}

export type CloneResult = { voice: string; name: string };

/** Registers a custom voice. The server refuses without explicit consent. */
export async function cloneVoice(audio: Blob, filename: string, transcript: string, name: string, consent: boolean): Promise<CloneResult> {
  const form = new FormData();
  form.append("audio", audio, filename);
  form.append("transcript", transcript);
  form.append("name", name);
  form.append("consent", consent ? "true" : "false");
  const res = await timedFetch("/api/voices/clone", { method: "POST", headers: authHeaders(), body: form }, 60000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Voice upload failed (${res.status})`);
  }
  return res.json() as Promise<CloneResult>;
}

export const fetchVoices = () => getJson<Record<string, string>>("/api/voices");

/** Chunk ids that have a pre-rendered avatar clip (stretch feature). Empty when there are none or the call fails. */
export async function fetchAvatars(bookId: string): Promise<string[]> {
  try { return await getJson<string[]>(`/api/avatars/${encodeURIComponent(bookId)}`); } catch { return []; }
}
export const avatarUrl = (bookId: string, chunkId: string) => `/api/avatar/${encodeURIComponent(chunkId)}?book=${encodeURIComponent(bookId)}`;
