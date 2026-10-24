import type { EnrichStatus } from "../api";

export type MoveNote = { id: string; text: string; progress?: number; confirm?: number; retry?: boolean };

const pct = (p: number | undefined) => Math.round(Math.max(0, Math.min(1, p ?? 0)) * 100);

/** The card note for a settled enrichment status: confirm (testing mode), refused, or an interrupted/failed job that can be retried. */
export function noteFromEnrich(id: string, e: EnrichStatus | undefined): MoveNote | null {
  if (e?.status === "awaiting_confirmation") return { id, text: e.message ?? "Characters, voices and recap need processing.", confirm: e.estimate_usd ?? 0 };
  if (e?.status === "refused") return { id, text: e.message ?? "Character processing did not run.", retry: true };
  if (e?.status === "error") return { id, text: e.message ?? "Character processing did not run.", retry: true };
  return null;
}

/** The status line under a library card, with its progress bar and its confirm or "Try again" button. */
export function VolumeNote({ title, note, onConfirm, onRetry }: { title: string; note: MoveNote; onConfirm: () => void; onRetry: () => void }) {
  return (
    <div className="volume-note" role="status">
      <span>{note.text}{note.progress !== undefined && ` (${pct(note.progress)}%)`}</span>
      {note.progress !== undefined && <progress max={100} value={pct(note.progress)} aria-label={`Processing ${title}`} />}
      {note.confirm !== undefined && (
        <button type="button" className="volume-move" onClick={onConfirm}>
          Process with characters (~${note.confirm.toFixed(2)})
        </button>
      )}
      {note.retry && (
        <button type="button" className="volume-move" aria-label={`Try processing ${title} again`} onClick={onRetry}>Try again</button>
      )}
    </div>
  );
}
