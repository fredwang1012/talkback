import type { BookStatus } from "./api";
import type { UploadJob } from "./uploads";

export const REFRESH_MS = 10_000;

/** Same list content: the page keeps its state (scroll, open stacks) and does not re-render when nothing changed. */
export const sameList = <T,>(a: T[] | null, b: T[] | null): boolean =>
  a !== null && b !== null && JSON.stringify(a) === JSON.stringify(b);

/** True while an upload is still being worked on: a job not yet ready or failed, or `serverBusy` listed uploads whose
 * own status says enrichment or indexing is running or queued. */
export function isProcessing(jobs: UploadJob[], serverBusy: number): boolean {
  return serverBusy > 0 || jobs.some((j) => j.phase === "waiting" || j.phase === "uploading" || j.phase === "processing");
}

/** Changes when an upload moves to another stage (not on every progress tick). */
export const stageKey = (s: BookStatus): string =>
  [s.status, s.index?.state ?? "", s.enrich?.status ?? "", s.enrich?.stage ?? ""].join("|");

/** Re-fetch a list every `ms` (and on poke) until stopped; a failed fetch is skipped, a late answer after stop is dropped. */
export function startListRefresh<T>({ fetch, apply, ms = REFRESH_MS }: {
  fetch: () => Promise<T[]>; apply: (next: T[]) => void; ms?: number;
}): { poke: () => void; stop: () => void } {
  let stopped = false;
  const run = () => {
    fetch().then((next) => { if (!stopped) apply(next); }).catch(() => undefined);
  };
  const timer = setInterval(run, ms);
  return { poke: run, stop: () => { stopped = true; clearInterval(timer); } };
}
