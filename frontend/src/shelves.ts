import type { BookKind, BookSummary } from "./api";

export type SeriesGroup = { series: string; books: BookSummary[] };
export type Shelves = { series: SeriesGroup[]; standalone: BookSummary[]; textbooks: BookSummary[] };

const byTitle = (a: BookSummary, b: BookSummary) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id);

/** Novels (series first, each in series_index order, then standalone novels) and Textbooks, both sorted by title. */
export function groupShelves(books: BookSummary[]): Shelves {
  const groups = new Map<string, BookSummary[]>();
  const standalone: BookSummary[] = [];
  const textbooks: BookSummary[] = [];
  for (const b of books) {
    if (b.kind === "textbook") textbooks.push(b);
    else if (b.series) groups.set(b.series, [...(groups.get(b.series) ?? []), b]);
    else standalone.push(b);
  }
  const series = [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, list]) => ({
      series: name,
      books: [...list].sort((a, b) => (a.series_index ?? 0) - (b.series_index ?? 0) || byTitle(a, b)),
    }));
  return { series, standalone: standalone.sort(byTitle), textbooks: textbooks.sort(byTitle) };
}

/** Only books you uploaded (ids start with "u-") can be moved. */
export const canMove = (b: Pick<BookSummary, "id">) => b.id.startsWith("u-");

/** Same rule on the server: only your own uploads can be deleted (the shipped and sample books never). */
export const canDelete = (b: Pick<BookSummary, "id">) => b.id.startsWith("u-");

export const moveTarget = (b: Pick<BookSummary, "kind">): BookKind => (b.kind === "textbook" ? "novel" : "textbook");

export const moveLabel = (b: Pick<BookSummary, "kind">) => (b.kind === "textbook" ? "Move to Novels" : "Move to Textbooks");

export const kindLabel = (kind: BookKind) => (kind === "textbook" ? "Textbook" : "Novel");

/** "harry-potter" -> "Harry Potter" */
export const seriesTitle = (slug: string) =>
  slug.split(/[-_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
