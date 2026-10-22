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
  sections: { id: string; title: string; start_para: number }[];
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

/**
 * Streams the spoken answer from /api/answer (NDJSON: `sentence` lines, then `done` or `error`).
 * Only the response headers have a deadline; the body can take as long as the model needs. `signal` aborts it.
 */
export async function streamAnswer(
  bookId: string, query: string, readerPara: number, speakerId: string,
  onSentence: (text: string) => void, signal?: AbortSignal, spoilers = false,
): Promise<void> {
  const res = await timedFetch("/api/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ book_id: bookId, query: query.slice(0, 500), reader_para: readerPara, speaker_id: speakerId, ...(spoilers ? { spoilers: true } : {}) }),
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

export type CloneResult = { voice: string; name: string; face: string | null };

/** Registers a custom voice. The server refuses without explicit consent. */
export async function cloneVoice(audio: Blob, filename: string, transcript: string, name: string, consent: boolean,
  face?: File | null): Promise<CloneResult> {
  const form = new FormData();
  form.append("audio", audio, filename);
  if (face) form.append("face", face, face.name);
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

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
export type BookKind = "novel" | "textbook";
/** What the upload form can send: "auto" lets the server decide. */
export type UploadKind = BookKind | "auto";
/** Novel enrichment (characters, voices, recap) that runs after a novel is readable. Absent for textbooks. */
export type EnrichStatus = {
  status: "awaiting_confirmation" | "running" | "done" | "refused" | "error";
  stage?: string;       // queued | cast | segment | summaries | index | done
  progress?: number;    // 0..1
  message?: string;
  degraded?: boolean;    // done, but without a working LLM (simple fallback cast)
  estimate_usd?: number; // only in testing mode (UPLOAD_CONFIRM)
};
/** The line shown when enrichment finished: the full-success wording only when an LLM really did the work. */
export function enrichDoneNote(e: EnrichStatus): string {
  return e.degraded && e.message ? e.message : "characters, voices and recap are ready";
}
export type BookStatus = {
  status: "processing" | "ready" | "error"; message?: string; enrich?: EnrichStatus;
  kind?: BookKind; kind_detected?: KindDetected;
};

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
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Upload failed (${res.status})`);
  }
  return res.json() as Promise<{ book_id: string }>;
}

export async function fetchBookStatus(id: string): Promise<BookStatus> {
  const res = await timedFetch(`/api/books/${encodeURIComponent(id)}/status`, { headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Status check failed (${res.status})`);
  }
  return res.json() as Promise<BookStatus>;
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
  while (!signal?.aborted) {
    let s: BookStatus | undefined;
    try {
      s = await fetchBookStatus(id);
      failures = 0;
    } catch (e) {
      if (signal?.aborted) return null;
      if (++failures > 3) throw e; // tolerate up to 3 failed calls in a row
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

export const fetchVoices = () => getJson<Record<string, string>>("/api/voices");

/** A cloned voice's face: portrait plus optional muted loops (speaking, idle). Shown for any speaker using the voice. */
export type VoiceFace = { image: string; talk: string | null; idle: string | null };
export const fetchVoiceFaces = () => getJson<Record<string, VoiceFace>>("/api/voices/faces");

/** Lip-synced clip of one answer sentence (the face speaking that exact audio). Takes ~10 s to render. */
export async function lipSync(audioUrl: string, voice: string): Promise<string> {
  const res = await timedFetch("/api/avatar/talk", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ audio_url: audioUrl, voice }),
  }, 120000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new Error(detail ?? `Lip-sync failed (${res.status})`);
  }
  return ((await res.json()) as { video_url: string }).video_url;
}

/** Streaming lip-sync: the response is fragmented MP4 as it renders, or JSON {video_url} when the clip is cached. */
export const lipSyncStream = (audioUrl: string, voice: string, signal: AbortSignal): Promise<Response> =>
  fetch("/api/avatar/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ audio_url: audioUrl, voice }),
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
export type Assignment = { voice: string | null; face: string | null };
export type Cast = { main: string[]; narrator: Assignment; characters: Record<string, Assignment> };

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
