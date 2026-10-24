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
/** A display equation of an uploaded textbook: its cropped picture and, once Gemini has read it, LaTeX and a spoken form. */
export type MathBlock = {
  id: string;
  page: number;
  bbox: number[];
  number?: string | null; // "3.4"
  garbled?: boolean;      // the paragraph text is not a faithful reading of the equation
  image?: string | null;
  latex?: string | null;
  spoken?: string | null;
};
/** A formula inside a sentence whose extracted text is garbled: p.text[start:end] is that text. */
export type InlineMath = {
  id: string;
  start: number;
  end: number;
  page: number;
  bbox: number[];
  image?: string | null;
  latex?: string | null;
  spoken?: string | null;
};
export type Paragraph = {
  idx: number;
  section_id: string;
  text: string;
  segments: Segment[];
  tts_chunks: TtsChunk[];
  kind?: "code" | "math" | "figure" | null; // code: a textbook listing, shown in a code font, not read aloud; math: an equation; figure (#40): `text` is its caption
  math?: MathBlock | null;
  inline_math?: InlineMath[];
  figure?: string | null; // the figure's image file (kind "figure"), see figureUrl
};
export type Character = {
  id: string;
  name: string;
  aliases: string[];
  first_para: number;
  voice: string;
  voice_tags: string;
  base_voice?: string | null; // preset the character was cast with, when `voice` is a baked custom voice (scripts/bake_voices.py)
  base_voice_tags?: string | null;
  persona: string;
  portrait: string | null;
  main?: boolean | null; // false = a minor speaker: read aloud, but not someone to talk to
};
export type Book = {
  id: string;
  title: string;
  author: string;
  kind: "novel" | "textbook";
  kind_detected?: KindDetected | null;
  license: string;
  attribution: string | null;
  sections: { id: string; title: string; start_para: number; level?: number }[];
  paragraphs: Paragraph[];
  characters: Character[];
  narrator: { id: string; voice: string; is_character: string | null; portrait: string | null } | null;
  series?: string | null;
  series_index?: number | null;
};
export type KindDetected = { kind: "novel" | "textbook"; confidence: number; method: string };
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
  series?: string | null;
  series_index?: number | null;
};
export type Health = { ok: boolean; boson_key_set: boolean; access_required: boolean; answer_backend: "gemini" | "realtime" };

/** Sent when the server requires an access code (TALKBACK_ACCESS_TOKEN). Typed in by the visitor, never built in. */
export function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { "X-Talkback-Token": token } : {};
}

/** A textbook figure's image (#40), served with the book's own gate. */
export function figureUrl(bookId: string, name: string): string {
  return `/api/books/${encodeURIComponent(bookId)}/figures/${encodeURIComponent(name)}`;
}

/** The figure as an object URL: fetched with the access code, which an <img src> cannot send. Revoke it when done. */
export async function fetchFigure(bookId: string, name: string): Promise<string> {
  const res = await timedFetch(figureUrl(bookId, name), { headers: authHeaders() });
  if (!res.ok) throw new Error(`Figure failed: ${res.status}`);
  return URL.createObjectURL(await res.blob());
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
  const res = await timedFetch(url, { headers: authHeaders() }); // books may be gated on a public deployment
  if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export type Me = { email: string | null; per_user: boolean };
/** Who is signed in (live deploy behind Google sign-in only). Never throws: no account is the local default. */
export const fetchMe = (): Promise<Me> =>
  getJson<Me>("/api/me").catch((): Me => ({ email: null, per_user: false }));
/** IAP's sign-out: clears its login cookie and returns to the app. */
export const SIGN_OUT_URL = "/?gcp-iap-mode=CLEAR_LOGIN_COOKIE";
/** The signed-in email to show, or null (local runs, anonymous visitors). */
export const accountLabel = (me: Me | null): string | null => (me && me.per_user && me.email ? me.email : null);

export const fetchBooks = () => getJson<BookSummary[]>("/api/books");
export const fetchBook = (id: string) => getJson<Book>(`/api/books/${encodeURIComponent(id)}`);
export const fetchHealth = () => getJson<Health>("/api/health");

export type Recap = { upto_para: number | null; chapter: string; text: string; earlier: string; earlier_full?: string };
/** "Previously...": the latest stored summary written from text at or before `para`. */
export const fetchRecap = (bookId: string, para: number) =>
  getJson<Recap>(`/api/books/${encodeURIComponent(bookId)}/recap?para=${Math.max(0, Math.floor(para))}`);

/** Recap menu scopes: the last chapter, this book so far, the series so far (earlier books + this one so far). */
export type RecapScope = "chapter" | "book" | "series";
/** One scope's recap; `chapter` is the chapter it covers (scope "chapter") or the one the reader is in. Spoiler-safe. */
export type ScopedRecap = { scope: RecapScope; upto_para: number | null; chapter: string; text: string };
/** `section`: that chapter / subsection instead (a textbook: any; a novel: only as far as the reader has read). */
export const fetchScopedRecap = (bookId: string, para: number, scope: RecapScope, section?: string) =>
  getJson<ScopedRecap>(`/api/books/${encodeURIComponent(bookId)}/recap?para=${Math.max(0, Math.floor(para))}&scope=${scope}`
    + (section ? `&section=${encodeURIComponent(section)}` : ""));

export type WordTiming = { start: number; end: number; char_start: number; char_end: number };
export type TtsResponse = { chunk_id: string; voice: string; audio_url: string; timestamps: WordTiming[] | null; cached: boolean };

/** Throws an Error carrying the server's readable message (e.g. a missing key or a billing refusal). */
export async function fetchTts(bookId: string, chunkId: string, voiceOverride?: string, cachedOnly = false, speed?: string | null): Promise<TtsResponse> {
  const res = await timedFetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, chunk_id: chunkId, voice_override: voiceOverride ?? null, cached_only: cachedOnly, ...(speed ? { speed } : {}) }),
  }, 60000); // a cache miss synthesises speech, which can take a while
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Audio request failed (${res.status})`);
  }
  return res.json() as Promise<TtsResponse>;
}

export type TranslationResponse = { para: number; language: string; chunk_ids: string[]; pieces: string[] };

async function postJson<T>(path: string, body: unknown, timeoutMs: number, what: string): Promise<T> {
  const res = await timedFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  }, timeoutMs);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(typeof detail === "string" ? detail : `${what} failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

/** Bilingual reading (B3): one paragraph in `language` (ISO 639-3), a translated piece per TTS chunk (server-cached). */
export function fetchTranslation(bookId: string, para: number, language: string): Promise<TranslationResponse> {
  return postJson("/api/translate", { book_id: bookId, para, language }, 30000, "Translation");
}

/** A chunk's translation read aloud in the chunk's own voice (no word timestamps: the piece is lit as a whole). */
export function fetchTranslatedTts(bookId: string, chunkId: string, language: string, voiceOverride?: string,
  cachedOnly = false, speed?: string | null): Promise<TtsResponse & { text: string }> {
  return postJson("/api/tts/translated", {
    book_id: bookId, chunk_id: chunkId, language, voice_override: voiceOverride ?? null, cached_only: cachedOnly,
    ...(speed ? { speed } : {}),
  }, 90000, "Audio request");
}

export type RetrieveResponse = {
  passages: { para_start: number; para_end: number; text: string }[];
  story_so_far: string;
  instructions: string;
  persona: string | null;
  reader_para: number;
};

export async function fetchRetrieve(bookId: string, query: string, readerPara: number, speakerId: string, spoilers = false): Promise<RetrieveResponse> {
  const res = await timedFetch("/api/retrieve", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId, ...(spoilers ? { spoilers: true } : {}) }),
  }, 10000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Lookup failed (${res.status})`);
  }
  return res.json() as Promise<RetrieveResponse>;
}

/** A web page a grounded answer used (#web search): shown as a link under the reply, never spoken. */
export type Source = { uri: string; title: string };
export const MAX_SOURCES = 5;
export const MAX_SUGGESTIONS = 32_000;
/** What a grounded answer sends in its `sources` line: the web links and Google's Search Suggestions markup. */
export type Grounding = { sources: Source[]; suggestions?: string };

/**
 * Google's Search Suggestions HTML (search_entry_point.rendered_content), unmodified: Google's terms require showing it
 * as provided. Only a string of plausible size is kept; it is displayed only inside a sandboxed iframe (SourceLinks).
 */
export function cleanSuggestions(raw: unknown): string | undefined {
  return typeof raw === "string" && raw.trim() && raw.length <= MAX_SUGGESTIONS ? raw : undefined;
}

/** Plain http(s) links from untrusted data (the stream, sessionStorage): no other schemes, no duplicates, short. */
export function cleanSources(raw: unknown): Source[] {
  const out: Source[] = [];
  if (!Array.isArray(raw)) return out;
  for (const s of raw) {
    const uri = (s as { uri?: unknown })?.uri;
    const title = (s as { title?: unknown })?.title;
    if (typeof uri !== "string" || uri.length > 2048 || !/^https?:\/\/[^\s]+$/i.test(uri) || out.some((o) => o.uri === uri)) continue;
    let host = "";
    try { host = new URL(uri).hostname; } catch { continue; }
    out.push({ uri, title: (typeof title === "string" && title.trim() ? title.trim() : host).slice(0, 120) });
    if (out.length >= MAX_SOURCES) break;
  }
  return out;
}

/**
 * Streams the spoken answer from /api/answer (NDJSON: `sentence` lines, then `done` or `error`). A grounded answer
 * (web search: textbooks, or novels with spoilers on) also sends one `sources` line, reported through `onSources`.
 * Only the response headers have a deadline; the body can take as long as the model needs. `signal` aborts it.
 */
export async function streamAnswer(
  bookId: string, query: string, readerPara: number, speakerId: string,
  onSentence: (text: string, emotion?: string) => void, signal?: AbortSignal, spoilers = false,
  history: { role: "user" | "character"; text: string }[] = [],
  onSources?: (grounding: Grounding) => void,
): Promise<void> {
  const res = await timedFetch("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId, ...(spoilers ? { spoilers: true } : {}), ...(history.length ? { history } : {}) }),
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
    let msg: { type?: string; text?: string; message?: string; emotion?: unknown; sources?: unknown; search_suggestions?: unknown };
    try { msg = JSON.parse(line); } catch { return; } // tolerate a garbled line
    if (msg.type === "sentence" && typeof msg.text === "string") {
      onSentence(msg.text, typeof msg.emotion === "string" && msg.emotion ? msg.emotion : undefined); // #14
    }
    else if (msg.type === "sources") {
      const sources = cleanSources(msg.sources);
      const suggestions = cleanSuggestions(msg.search_suggestions);
      if (sources.length || suggestions) onSources?.({ sources, ...(suggestions ? { suggestions } : {}) });
    }
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

/**
 * Synthesises one sentence in a voice (cached server-side) and returns where to fetch the audio. `emotion` (#14) is an
 * answer sentence's feeling; the server checks it and sends it to Boson as a delivery tag.
 */
/** `language`: ISO 639-3 of a non-English answer (#135); the backend then asks Boson for normalized speech without
 * word timestamps (the response's `timestamps` is null). Never sent for English. */
export async function speak(text: string, voice: string, emotion?: string, language?: string | null): Promise<TtsResponse> {
  const res = await timedFetch("/api/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ text, voice, ...(emotion ? { emotion } : {}), ...(language && language !== "eng" ? { language } : {}) }),
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

export type CloneResult = { voice: string; name: string; face: string | null };

/** Registers a custom voice. The server refuses without explicit consent. `face`: an image file stored as a new face
 * bound to the voice; `faceId`: an existing library face linked to it instead (#113, no copy). */
export async function cloneVoice(audio: Blob, filename: string, transcript: string, name: string, consent: boolean,
  face?: File | null, faceId?: string | null): Promise<CloneResult> {
  const form = new FormData();
  form.append("audio", audio, filename);
  if (face) form.append("face", face, face.name);
  else if (faceId) form.append("face_id", faceId);
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

/**
 * Permanently deletes an uploaded book (files, search entries, caches). 409 while it is still being processed.
 * Resolves as soon as the book has left the library (202); the server finishes in the background (waitForDeletion).
 */
export async function deleteBook(id: string): Promise<void> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}`, { method: "DELETE", headers: authHeaders() }, 30000);
  if (res.status === 404) return; // already gone (e.g. a retry after a timed-out request that still finished)
  if (!res.ok) throw await readError(res, "Could not delete the book");
}

/** Polls a deleted book's status until its background removal ends: {ok:true} when gone, the message on failure,
 * null when aborted. A 404 means the book is fully removed. */
export async function waitForDeletion(
  id: string, signal?: AbortSignal, intervalMs = 2000, maxMs = 10 * 60 * 1000,
): Promise<{ ok: true } | { ok: false; message: string } | null> {
  const deadline = Date.now() + maxMs;
  let failures = 0;
  while (!signal?.aborted) {
    try {
      const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/status`, { headers: authHeaders(), signal });
      if (res.status === 404) return { ok: true };
      if (res.ok) {
        failures = 0;
        const s = (await res.json()) as { status: string; message?: string };
        if (s.status === "delete_error") return { ok: false, message: s.message ?? "The delete did not finish." };
        if (s.status !== "deleting") return { ok: true };
      } else if (++failures > 3) return { ok: false, message: `Could not check the delete (${res.status}).` };
    } catch (e) {
      if (signal?.aborted) return null;
      if (++failures > 3) return { ok: false, message: (e as Error).message };
    }
    if (Date.now() + intervalMs > deadline) return { ok: false, message: "The delete is taking too long; try again." };
    await new Promise<void>((resolve) => {
      const t = setTimeout(done, intervalMs);
      function done() { clearTimeout(t); signal?.removeEventListener("abort", done); resolve(); }
      signal?.addEventListener("abort", done);
    });
  }
  return null;
}

/** Built-in books (classics, Harry Potter) are hidden for this account instead of deleted; restorable. */
export async function hideBook(id: string): Promise<void> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/hide`, { method: "POST", headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Could not hide the book");
}

export async function restoreBook(id: string): Promise<void> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/restore`, { method: "POST", headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Could not restore the book");
}

export type HiddenBook = { id: string; title: string; author: string };
export const fetchHiddenBooks = () => getJson<HiddenBook[]>("/api/hidden-books");

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
export type BookKind = "novel" | "textbook";
/** What the upload form can send: "auto" lets the server decide. */
export type UploadKind = BookKind | "auto";
/** Enrichment that runs once a book is readable: characters, voices and recap for a novel; only the recap study
 * notes for a textbook (`summaries_only`). */
export type EnrichStatus = {
  status: "awaiting_confirmation" | "running" | "done" | "refused" | "error";
  stage?: string;       // queued | cast | segment | summaries | index | done
  progress?: number;    // 0..1
  message?: string;
  degraded?: boolean;    // done, but without a working LLM (simple fallback cast)
  index_failed?: boolean; // done, but the cloud search index could not be rebuilt (basic search until it is retried)
  notes_skipped?: number; // done, but this many recap notes could not be written
  estimate_usd?: number; // only in testing mode (UPLOAD_CONFIRM)
  summaries_only?: boolean; // a textbook: recap study notes only
};
/** The line shown when enrichment finished: the full-success wording only when everything really landed; otherwise
 * the server's own note (fallback cast, index pending, skipped recap notes). */
export function enrichDoneNote(e: EnrichStatus): string {
  if (e.summaries_only) return "study notes for the recap are ready";
  const partial = e.degraded || e.index_failed || (e.notes_skipped ?? 0) > 0;
  return partial && e.message ? e.message : "characters, voices and recap are ready";
}
/** Cloud search indexing after the book is readable (it uses basic search until state is "done"). */
export type IndexStatus = { state: "queued" | "running" | "done" | "error"; done: number; total: number; message?: string };
export const indexingLabel = (i: IndexStatus): string =>
  i.state === "queued" ? "Queued for indexing" : `Indexing ${i.done} / ${i.total}`;
export type BookStatus = {
  status: "processing" | "ready" | "error" | "deleting" | "delete_error"; message?: string; enrich?: EnrichStatus; index?: IndexStatus;
  kind?: BookKind; kind_detected?: KindDetected;
};

/** A 409 from the upload: this account already has the file ("duplicate"), is adding it right now ("processing"), or
 * is still deleting its earlier copy ("deleting"). `bookId` is that existing book. */
export class UploadConflict extends Error {
  readonly reason: "duplicate" | "processing" | "deleting";
  readonly bookId: string;
  readonly title?: string;
  constructor(message: string, reason: UploadConflict["reason"], bookId: string, title?: string) {
    super(message);
    this.name = "UploadConflict";
    this.reason = reason;
    this.bookId = bookId;
    this.title = title;
  }
}

/** Sends a PDF or .txt for ingest. Resolves with the new book id; ingest continues server-side (poll fetchBookStatus). */
export async function uploadBook(file: File, title: string, author: string, kind: UploadKind): Promise<{ book_id: string }> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("title", title);
  form.append("author", author);
  form.append("kind", kind);
  // no Content-Type: the browser adds the multipart boundary
  const res = await timedFetch("/api/books/upload", { method: "POST", headers: authHeaders(), body: form }, 120000);
  if (!res.ok) {
    const body = await res.json().catch(() => ({})) as { detail?: string; reason?: string; book_id?: string; title?: string };
    const detail = typeof body.detail === "string" ? body.detail : undefined;
    if (res.status === 409 && body.book_id && (body.reason === "duplicate" || body.reason === "processing" || body.reason === "deleting"))
      throw new UploadConflict(detail ?? "This file is already in your library.", body.reason, body.book_id, body.title);
    throw new Error(detail ?? `Upload failed (${res.status})`);
  }
  return res.json() as Promise<{ book_id: string }>;
}

export const DESCRIBE_TEXT_BYTES = 24_000; // backend describe.TEXT_PREFIX_BYTES
export type BookGuess = { title: string; author: string; source: "metadata" | "llm" | "none" };

/** The title and author the server reads from a chosen file (PDF metadata, else its first pages); null on any failure
 * (the form keeps the file-name title). Nothing is stored server-side. */
export async function describeBook(file: File, signal?: AbortSignal): Promise<BookGuess | null> {
  const form = new FormData();
  // a text file's title is in its opening lines: send only those (the server reads no more); a PDF needs all of it
  const body = /\.txt$/i.test(file.name) ? new File([file.slice(0, DESCRIBE_TEXT_BYTES)], file.name, { type: file.type }) : file;
  form.append("file", body, file.name);
  try {
    const res = await timedFetch("/api/books/describe", { method: "POST", headers: authHeaders(), body: form, signal }, 30000);
    if (!res.ok) return null;
    const j = (await res.json()) as Partial<BookGuess>;
    return { title: j.title ?? "", author: j.author ?? "", source: j.source ?? "none" };
  } catch {
    return null;
  }
}

export async function fetchBookStatus(id: string): Promise<BookStatus> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/status`, { headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw Object.assign(new Error(detail ?? `Status check failed (${res.status})`), { status: res.status });
  }
  return res.json() as Promise<BookStatus>;
}

/** The backend is restarting or briefly unreachable (proxy 502/503/504, or no response at all): keep waiting. */
export const UNREACHABLE_GRACE_MS = 60_000;
export function isUnreachable(e: unknown): boolean {
  const status = (e as { status?: number })?.status;
  return status === undefined ? !(e instanceof Error && /taking too long/.test(e.message)) : [502, 503, 504].includes(status);
}

/**
 * Polls a book's status until `settled(status)` is true, then resolves with that status.
 * Stops quietly (resolves null) when `signal` aborts; rejects if it is still unsettled after `maxMs`.
 * A failed status call rejects too (after 3 in a row), so the caller can show the message.
 */
async function pollStatus(
  id: string, settled: (s: BookStatus) => boolean, signal: AbortSignal | undefined, intervalMs: number, maxMs: number,
  onUpdate?: (s: BookStatus) => void,
): Promise<BookStatus | null> {
  const deadline = Date.now() + maxMs;
  let failures = 0;
  let unreachableSince: number | null = null;
  while (!signal?.aborted) {
    let s: BookStatus | undefined;
    try {
      s = await fetchBookStatus(id);
      failures = 0;
      unreachableSince = null;
    } catch (e) {
      if (signal?.aborted) return null;
      if (isUnreachable(e)) {   // a backend restart: wait it out instead of giving up after a few seconds
        unreachableSince ??= Date.now();
        if (Date.now() - unreachableSince > UNREACHABLE_GRACE_MS) throw e;
      } else if (++failures > 3) throw e; // tolerate up to 3 failed calls in a row
    }
    if (signal?.aborted) return null;
    if (s) {
      if (settled(s)) return s;
      onUpdate?.(s);
    }
    if (Date.now() + intervalMs > deadline) throw new Error("This is taking too long. Check the backend and try again.");
    await new Promise<void>((resolve) => {
      const t = setTimeout(done, intervalMs);
      function done() { clearTimeout(t); signal?.removeEventListener("abort", done); resolve(); }
      signal?.addEventListener("abort", done);
    });
  }
  return null;
}

/** Polls until the book is ready (readable) or errored. */
export function pollBookStatus(
  id: string, signal?: AbortSignal, intervalMs = 1500, maxMs = 10 * 60 * 1000,
): Promise<BookStatus | null> {
  return pollStatus(id, (s) => s.status !== "processing", signal, intervalMs, maxMs);
}

/** After a book is ready: polls until its search indexing is no longer running (`onUpdate` sees each running status). */
export function pollIndexing(
  id: string, onUpdate?: (s: BookStatus) => void, signal?: AbortSignal, intervalMs = 2000, maxMs = 30 * 60 * 1000,
): Promise<BookStatus | null> {
  return pollStatus(id, (s) => s.status !== "ready" || (s.index?.state !== "running" && s.index?.state !== "queued"),
    signal, intervalMs, maxMs, onUpdate);
}

/** One poll for a whole upload: until `settled` (see uploads.uploadSettled); `onUpdate` sees every other status. */
export function pollUpload(
  id: string, settled: (s: BookStatus) => boolean, onUpdate?: (s: BookStatus) => void, signal?: AbortSignal,
  intervalMs = 2000, maxMs = 60 * 60 * 1000,
): Promise<BookStatus | null> {
  return pollStatus(id, settled, signal, intervalMs, maxMs, onUpdate);
}

/**
 * After a book is ready: polls until its enrichment is done, refused, errored or waiting for confirmation
 * (resolves with that status; `onUpdate` sees every running status). Textbooks resolve at once.
 */
export function pollEnrichment(
  id: string, onUpdate?: (s: BookStatus) => void, signal?: AbortSignal, intervalMs = 2500, maxMs = 45 * 60 * 1000,
): Promise<BookStatus | null> {
  return pollStatus(id, (s) => s.status !== "ready" || s.enrich?.status !== "running", signal, intervalMs, maxMs, onUpdate);
}

/** Testing mode: confirm the cost estimate and start the character processing. */
export async function startEnrichment(id: string): Promise<BookStatus> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/enrich`, { method: "POST", headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Could not start processing (${res.status})`);
  }
  return res.json() as Promise<BookStatus>;
}

/** Deletes a cloned voice at Boson, from the saved list, and its linked face. 403 for built-in presets. */
export async function deleteVoice(id: string): Promise<void> {
  const res = await timedFetch(`/api/voices/${encodeURIComponent(id)}`, { method: "DELETE", headers: authHeaders() }, 60000);
  if (!res.ok) throw await readError(res, "Could not delete the voice");
}

export const fetchVoices = () => getJson<Record<string, string>>("/api/voices");

/** A cloned voice's face: portrait plus optional muted loops (speaking, idle). Shown for any speaker using the voice. */
export type VoiceFace = { image: string; talk: string | null; idle: string | null };
export const fetchVoiceFaces = () => getJson<Record<string, VoiceFace>>("/api/voices/faces");

/** The body of a lip-sync request: the cached audio, the voice that spoke it and the face shown (#113). Without a
 * face id the server falls back to the face bound to the voice. */
export const lipSyncBody = (audioUrl: string, voice: string, faceId: string | null) =>
  JSON.stringify({ audio_url: audioUrl, voice, ...(faceId ? { face_id: faceId } : {}) });

/** Lip-synced clip of one answer sentence (the face shown speaking that exact audio). Takes ~10 s to render. */
export async function lipSync(audioUrl: string, voice: string, faceId: string | null = null): Promise<string> {
  const res = await timedFetch("/api/avatar/talk", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: lipSyncBody(audioUrl, voice, faceId),
  }, 120000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Lip-sync failed (${res.status})`);
  }
  return ((await res.json()) as { video_url: string }).video_url;
}

/** Streaming lip-sync: the response is fragmented MP4 as it renders, or JSON {video_url} when the clip is cached. */
export const lipSyncStream = (audioUrl: string, voice: string, faceId: string | null, signal: AbortSignal): Promise<Response> =>
  fetch("/api/avatar/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: lipSyncBody(audioUrl, voice, faceId),
    signal,
  });

/** Chunk ids that have a pre-rendered avatar clip (stretch feature). Empty when there are none or the call fails. */
export async function fetchAvatars(bookId: string): Promise<string[]> {
  try { return await getJson<string[]>(`/api/avatars/${encodeURIComponent(bookId)}`); } catch { return []; }
}
export const avatarUrl = (bookId: string, chunkId: string) => `/api/avatar/${encodeURIComponent(chunkId)}?book=${encodeURIComponent(bookId)}`;

// ---------- face library and per-book cast settings ----------

/** A saved face. `talk` / `idle` are muted loops that exist only after "Animate"; `voice` binds it to a cloned voice. */
export type Face = { id: string; name: string; image: string; voice: string | null; talk: string | null; idle: string | null };
export type Assignment = { voice: string | null; face: string | null; tags?: string | null }; // tags: pitch/speed override, "" = none
export type Gender = "male" | "female";
/**
 * Per-book cast. Novels read in the male and the female voice (each with a face). Mode "two" (the default): every
 * speaker reads in the voice of their gender (`genders` overrides the read-only `auto_genders`), the narrator in the
 * one `narrator_gender` picks. Mode "one": every line reads in the narrator's voice. "cast" is the removed Full cast
 * (#93), read as "two" (castLogic.voiceMode); `characters` is kept but no longer read. Textbooks use `narrator` only.
 * `auto_genders` and `defaults` come from the server and are ignored when sent back.
 */
export type Cast = {
  main: string[]; narrator: Assignment; characters: Record<string, Assignment>;
  mode?: "two" | "one" | "cast"; male?: Assignment; female?: Assignment; narrator_gender?: Gender | null;
  genders?: Record<string, Gender>; auto_genders?: Record<string, Gender | null>;
  defaults?: { male: string; female: string; narrator_gender: Gender };
};

async function readError(res: Response, fallback: string): Promise<Error> {
  const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
  return new Error(typeof detail === "string" ? detail : `${fallback} (${res.status})`);
}

export const fetchFaces = () => getJson<Face[]>("/api/faces");

export async function uploadFace(image: File, name: string): Promise<Face> {
  const form = new FormData();
  form.append("image", image, image.name);
  form.append("name", name);
  const res = await timedFetch("/api/faces", { method: "POST", headers: authHeaders(), body: form }, 60000);
  if (!res.ok) throw await readError(res, "Face upload failed");
  return res.json() as Promise<Face>;
}

/** The main face's box as fractions of the image (x, y of the top-left corner). */
export type FaceBox = { x: number; y: number; w: number; h: number };

/** The main face's box (fractions of the image) for the head-and-shoulders crop; null on no face or any failure. */
export async function detectFace(image: File, signal?: AbortSignal): Promise<FaceBox | null> {
  const form = new FormData();
  form.append("image", image, image.name);
  try {
    const res = await timedFetch("/api/faces/detect", { method: "POST", headers: authHeaders(), body: form, signal }, 30000);
    if (!res.ok) return null;
    const f = ((await res.json()) as { face?: Partial<FaceBox> | null }).face;
    if (!f || ![f.x, f.y, f.w, f.h].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
    return { x: f.x!, y: f.y!, w: f.w!, h: f.h! };
  } catch {
    return null;
  }
}

export async function deleteFace(id: string): Promise<void> {
  const res = await timedFetch(`/api/faces/${encodeURIComponent(id)}`, { method: "DELETE", headers: authHeaders() });
  if (!res.ok) throw await readError(res, "Could not delete the face");
}

export type AnimatedFace = Face & { animated: boolean; approx_cost_usd: number };
export const ANIMATE_TIMEOUT_MS = 4 * 60 * 1000; // rendering takes 1 to 2 minutes

/** Renders the talk and idle loops (a few cents of Boson credit). Slow; `signal` cancels the wait. */
export async function animateFace(id: string, signal?: AbortSignal): Promise<AnimatedFace> {
  const res = await timedFetch(`/api/faces/${encodeURIComponent(id)}/animate`, { method: "POST", headers: authHeaders(), signal }, ANIMATE_TIMEOUT_MS);
  if (!res.ok) {
    if (res.status === 402) throw new Error("The Boson account is out of credit, so the face could not be animated.");
    if (res.status === 429) throw new Error("Too many animations right now. Wait a minute and try again.");
    throw await readError(res, "Animation failed");
  }
  return res.json() as Promise<AnimatedFace>;
}

export const fetchCast = (bookId: string) => getJson<Cast>(`/api/books/${encodeURIComponent(bookId)}/cast`);

/** Replaces the whole cast of a book (send every field). */
export async function saveCast(bookId: string, cast: Cast): Promise<Cast> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(bookId)}/cast`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(cast),
  });
  if (!res.ok) throw await readError(res, "Could not save the cast");
  return res.json() as Promise<Cast>;
}

/** Moves an uploaded book to the other shelf. Moving a textbook to Novels may start character processing. */
export async function changeKind(id: string, kind: BookKind): Promise<BookStatus> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/kind`, {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ kind }),
  });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Could not move the book (${res.status})`);
  }
  return res.json() as Promise<BookStatus>;
}
