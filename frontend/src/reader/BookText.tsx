import { Fragment, memo } from "react";
import type { Book } from "../api";
import type { FlatChunk } from "./position";

type Props = { book: Book; chunks: FlatChunk[]; index: number; onPick: (i: number) => void };

/** Renders the book with each TTS chunk as a clickable sentence span; the active one is lit. */
function BookText({ book, chunks, index, onPick }: Props) {
  const indexById = new Map(chunks.map((c, i) => [c.id, i]));
  const sectionAt = new Map(book.sections.map((s) => [s.start_para, s]));

  return (
    <article>
      <header className="book-title">
        <h1>{book.title}</h1>
        <p>by {book.author}</p>
      </header>
      {book.paragraphs.map((p) => {
        const parts: React.ReactNode[] = [];
        let cursor = 0;
        for (const c of p.tts_chunks) {
          if (c.char_start > cursor) parts.push(<span className="gap" key={`g${c.id}`}>{p.text.slice(cursor, c.char_start)}</span>);
          const i = indexById.get(c.id) ?? 0;
          const state = i === index ? "active" : i < index ? "past" : "";
          parts.push(
            <span
              key={c.id}
              className={`sentence ${state}`}
              data-active={i === index}
              role="button"
              tabIndex={-1}
              onClick={() => onPick(i)}
            >
              {p.text.slice(c.char_start, c.char_end)}
            </span>,
          );
          cursor = c.char_end;
        }
        if (cursor < p.text.length) parts.push(<span className="gap" key="tail">{p.text.slice(cursor)}</span>);
        const section = sectionAt.get(p.idx);
        return (
          <Fragment key={p.idx}>
            {section && section.title && <h2 className="section-mark">{section.title}</h2>}
            <p className="para" data-para={p.idx}>{parts}</p>
          </Fragment>
        );
      })}
    </article>
  );
}

export default memo(BookText);
