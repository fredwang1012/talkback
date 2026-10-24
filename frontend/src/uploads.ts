import { indexingLabel, MAX_UPLOAD_BYTES, type BookStatus } from "./api";

/** One file of a (possibly multi-file) upload, followed on its own card until it is ready. */
export type UploadJob = {
  key: string;
  title: string;
  phase: "waiting" | "uploading" | "processing" | "confirm" | "ready" | "error";
  bookId?: string;
  status?: BookStatus;
  message?: string;
  estimate?: number;
};

/** "my_book-final.pdf" -> "my book final" (max 120 chars, the server's limit). */
export const titleFromFile = (name: string) =>
  name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 120);

/** Why this file cannot be uploaded, or null. */
export function fileProblem(f: Pick<File, "name" | "size">): string | null {
  if (!/\.(pdf|txt)$/i.test(f.name)) return "Only PDF and .txt files can be added.";
  if (f.size > MAX_UPLOAD_BYTES) return "That file is over 20 MB. Choose a smaller one.";
  return null;
}

/** True once a book needs nothing more from the server: failed, or readable with indexing and enrichment settled. */
export function uploadSettled(s: BookStatus): boolean {
  if (s.status === "error") return true;
  if (s.status !== "ready") return false;
  const idx = s.index?.state;
  return idx !== "running" && idx !== "queued" && s.enrich?.status !== "running";
}

/** The one line on a job card: where this book is right now. */
export function jobLine(j: UploadJob): string {
  if (j.phase === "waiting") return j.message ?? "Waiting to upload";
  if (j.phase === "uploading") return "Uploading…";
  if (j.phase === "error") return j.message ?? "The book could not be processed.";
  if (j.phase === "confirm") return "Readable now. Characters, voices and recap need processing.";
  const s = j.status;
  if (!s || s.status === "processing") return s?.message === "Queued" ? "Queued" : (s?.message ? `${s.message}…` : "Reading…");
  if (s.index?.state === "queued") return "Readable now · Queued for indexing";
  if (s.index?.state === "running") return `Readable now · ${indexingLabel(s.index)}`;
  if (s.enrich?.status === "running") return `Readable now · ${s.enrich.message ?? "Adding characters"}`;
  if (j.phase === "ready") return j.message ? `Ready · ${j.message}` : "Ready";
  return "Readable now";
}

/** 0..1 for the card's progress bar, or undefined when there is nothing to measure (queued, reading). */
export function jobProgress(j: UploadJob): number | undefined {
  const s = j.status;
  if (j.phase !== "processing" || !s || s.status !== "ready") return undefined;
  if (s.index?.state === "running") return s.index.total > 0 ? s.index.done / s.index.total : 0;
  if (s.enrich?.status === "running") return s.enrich.progress ?? 0;
  return undefined;
}

/** A 429 from the upload endpoint (queue full or rate limit): wait and try again rather than fail the file. */
export const isBusyError = (e: unknown) => e instanceof Error && /\b(429|already being processed|Too many uploads)\b/i.test(e.message);
