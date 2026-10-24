import { Fragment, memo } from "react";
import "katex/dist/katex.min.css";
import type { Book, Paragraph } from "../api";
import { equationView, imageHeight, imageWidth, inlineView, pieces } from "./equation";
import type { FlatChunk } from "./position";
import ChapterList from "./ChapterList";
import Figure from "./Figure";
import { isNested } from "./chapterTree";
import { cleanTitle } from "./cleanTitle";

type Props = {
  book: Book; chunks: FlatChunk[]; index: number; onPick: (i: number) => void;
  word?: { charStart: number; charEnd: number } | null;
};

/** Index of the section containing paragraph `para` (sections are ordered by start_para). */
export function sectionIndexAt(book: Book, para: number): number {
  let at = 0;
  book.sections.forEach((s, i) => { if (s.start_para <= para) at = i; });
  return at;
}

/** Paragraph range [start, end) of section `i`. */
export function sectionRange(book: Book, i: number): [number, number] {
  const start = book.sections[i]?.start_para ?? 0;
  const end = book.sections[i + 1]?.start_para ?? book.paragraphs.length;
  return [start, end];
}

/**
 * Renders the chapter being read, with each TTS chunk as a clickable sentence span; the active one is lit.
 * Only one chapter is in the DOM: a whole novel is ~10,000 spans, too many to re-render on every spoken word.
 */
function BookText({ book, chunks, index, onPick, word }: Props) {
  const indexById = new Map(chunks.map((c, i) => [c.id, i]));
  const sectionAt = new Map(book.sections.map((s) => [s.start_para, s]));
  const current = sectionIndexAt(book, chunks[index]?.para ?? 0);
  const [from, to] = sectionRange(book, current);
  const goTo = (i: number) => {
    const [start] = sectionRange(book, i);
    const first = chunks.findIndex((c) => c.para >= start);
    if (first >= 0) onPick(first);
  };
  const chapterNav = book.sections.length > 1 && (
    <nav className="chapter-nav" aria-label="Chapters">
      <button disabled={current === 0} onClick={() => goTo(current - 1)}>Previous chapter</button>
      {isNested(book.sections)
        ? <ChapterList book={book} current={current} onGo={goTo} />
        : <select aria-label="Go to chapter" value={current} onChange={(e) => goTo(Number(e.target.value))}>
            {book.sections.map((s, i) => <option key={s.id} value={i}>{cleanTitle(s.title) || `Part ${i + 1}`}</option>)}
          </select>}
      <button disabled={current >= book.sections.length - 1} onClick={() => goTo(current + 1)}>Next chapter</button>
    </nav>
  );

  return (
    <article>
      <header className="book-title">
        <h1>{book.title}</h1>
        <p>by {book.author}</p>
      </header>
      {chapterNav}
      {book.paragraphs.slice(from, to).map((p) => {
        const section = sectionAt.get(p.idx);
        const heading = section && section.title && <h2 className="section-mark">{cleanTitle(section.title)}</h2>;
        if (p.kind === "math") {
          const c = p.tts_chunks[0];
          const i = c ? indexById.get(c.id) ?? 0 : -1;
          const state = i === index ? "active" : i < index ? "past" : "";
          return (
            <Fragment key={p.idx}>
              {heading}
              <div className="para para-math" data-para={p.idx}>
                <span className={`sentence ${state}`} data-active={i === index} role="button" tabIndex={-1}
                      onClick={() => i >= 0 && onPick(i)}>
                  <Equation p={p} />
                </span>
              </div>
            </Fragment>
          );
        }
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
              {p.inline_math?.length
                ? <WithMath p={p} start={c.char_start} end={c.char_end} word={i === index ? word : null} />
                : i === index && word ? <Spoken text={p.text.slice(c.char_start, c.char_end)} word={word} /> : p.text.slice(c.char_start, c.char_end)}
            </span>,
          );
          cursor = c.char_end;
        }
        if (cursor < p.text.length) parts.push(<span className="gap" key="tail">{p.text.slice(cursor)}</span>);
        return (
          <Fragment key={p.idx}>
            {heading}
            {p.kind === "figure" && p.figure
              ? <Figure bookId={book.id} name={p.figure} caption={p.text} para={p.idx}>{parts}</Figure>
              : <p className={p.kind === "code" ? "para para-code" : "para"} data-para={p.idx}>{parts}</p>}
          </Fragment>
        );
      })}
      {chapterNav}
    </article>
  );
}

/** A display equation: typeset from its LaTeX, else the picture cropped from the PDF, else the extracted text. */
function Equation({ p }: { p: Paragraph }) {
  const view = equationView(p.math);
  const spoken = p.tts_chunks[0]?.tts_text ?? p.text;
  if (view.kind === "latex") return <span className="math-tex" dangerouslySetInnerHTML={{ __html: view.html }} />;
  if (view.kind === "image") {
    return <img className="math-img" src={view.src} alt={spoken} loading="lazy" style={{ width: imageWidth(p.math) }} />;
  }
  return <span className="math-text">{p.text}</span>;
}

/** A sentence holding inline formulas, each typeset (else its picture, else its text); the spoken word is marked. */
function WithMath({ p, start, end, word }: {
  p: Paragraph; start: number; end: number; word?: { charStart: number; charEnd: number } | null;
}) {
  return <>{pieces(p.text, start, end, p.inline_math, word).map((x, k) => {
    if (x.kind === "text") return x.spoken ? <mark className="word" key={k}>{x.text}</mark> : x.text;
    const view = inlineView(x.math);
    const cls = `math-inline${x.spoken ? " word" : ""}`;
    if (view.kind === "latex") return <span className={cls} key={k} dangerouslySetInnerHTML={{ __html: view.html }} />;
    if (view.kind === "image") {
      return <img className={`${cls} math-inline-img`} key={k} src={view.src} alt={x.math.spoken || x.text}
                  loading="lazy" style={{ height: imageHeight(x.math) }} />;
    }
    return x.spoken ? <mark className="word" key={k}>{x.text}</mark> : x.text;
  })}</>;
}

/** The sentence being read, with the word currently spoken marked. */
function Spoken({ text, word }: { text: string; word: { charStart: number; charEnd: number } }) {
  const a = Math.min(word.charStart, text.length), b = Math.min(word.charEnd, text.length);
  return <>{text.slice(0, a)}<mark className="word">{text.slice(a, b)}</mark>{text.slice(b)}</>;
}

export default memo(BookText);
