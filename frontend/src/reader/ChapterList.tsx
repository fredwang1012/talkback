import { useEffect, useState } from "react";
import type { Book } from "../api";
import { openAround, visibleRows } from "./chapterTree";

type Props = { book: Book; current: number; onGo: (i: number) => void };

/** Nested, collapsible chapter list for textbooks; the chapter holding the reader's position is kept expanded. */
export default function ChapterList({ book, current, onGo }: Props) {
  const [shown, setShown] = useState(false);
  const [open, setOpen] = useState<ReadonlySet<number>>(() => openAround(book.sections, current, new Set()));
  useEffect(() => { setOpen((o) => openAround(book.sections, current, o)); }, [book.sections, current]);
  const toggle = (i: number) => setOpen((o) => { const n = new Set(o); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  const label = (i: number) => book.sections[i].title || `Part ${i + 1}`;
  return (
    <div className="chapter-tree">
      <button aria-expanded={shown} aria-controls="chapter-tree-list" onClick={() => setShown(!shown)}>
        Chapters: {label(current)}
      </button>
      {shown && (
        <ul id="chapter-tree-list" aria-label="Chapters">
          {visibleRows(book.sections, open).map((r) => (
            <li key={book.sections[r.index].id} style={{ paddingLeft: `${r.depth * 16}px` }}
                aria-current={r.index === current ? "location" : undefined}>
              {r.hasChildren
                ? <button className="twisty" aria-expanded={r.open} aria-label={`${r.open ? "Collapse" : "Expand"} ${label(r.index)}`}
                          onClick={() => toggle(r.index)}>{r.open ? "\u25BE" : "\u25B8"}</button>
                : <span className="twisty" aria-hidden="true" />}
              <button className="chapter-go" onClick={() => { onGo(r.index); setShown(false); }}>{label(r.index)}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
