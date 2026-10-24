import katex from "katex";
import type { InlineMath, MathBlock } from "../api";

/** How a display equation is shown: typeset LaTeX, else the picture cropped from the PDF, else the extracted text. */
export type EquationView =
  | { kind: "latex"; html: string }
  | { kind: "image"; src: string }
  | { kind: "text" };

const CACHE_MAX = 500;
const cache = new Map<string, string | null>();
// KaTeX shows a refused command (\href with trust off) in errorColor instead of throwing; this odd shade marks it.
const REFUSED = "#cc0001";

/** KaTeX HTML for `latex`, or null when it does not parse (the picture stands in). Cached: a chapter re-renders on
 * every spoken word. trust stays off, so model-written LaTeX can never add links, classes or raw HTML. */
export function typeset(latex: string, display = true): string | null {
  const key = (display ? "D" : "T") + latex;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  let html: string | null;
  try {
    html = katex.renderToString(latex, {
      displayMode: display, throwOnError: true, strict: "ignore", trust: false, maxExpand: 500, errorColor: REFUSED,
    });
    if (html.includes(REFUSED)) html = null;
  } catch {
    html = null;
  }
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string);
  cache.set(key, html);
  return html;
}

/** The model's LaTeX leaves the equation number out; KaTeX sets it at the right margin like the book does. */
export function withNumber(latex: string, number?: string | null): string {
  return number && !latex.includes("\\tag") ? `${latex} \\tag{${number}}` : latex;
}

const BODY_PT = 10;  // textbook body text is ~10pt: the crop is sized so its glyphs match the reader's font
const PAD_PT = 2;    // ingest_upload.EQ_PAD on each side

/** CSS width of a crop: its width in the PDF, in ems of the reader's text (undefined: let the picture decide). */
export function imageWidth(math: MathBlock | null | undefined): string | undefined {
  const [x0, , x1] = math?.bbox ?? [];
  if (x0 === undefined || x1 === undefined || !(x1 > x0)) return undefined;
  return `${((x1 - x0 + 2 * PAD_PT) / BODY_PT).toFixed(2)}em`;
}

export function equationView(math: MathBlock | null | undefined): EquationView {
  const html = math?.latex ? typeset(withNumber(math.latex, math.number)) : null;
  if (html) return { kind: "latex", html };
  if (math?.image) return { kind: "image", src: math.image };
  return { kind: "text" };
}

/** An inline formula: typeset in text style, else its picture, else the extracted text. */
export function inlineView(m: InlineMath): EquationView {
  const html = m.latex ? typeset(m.latex, false) : null;
  if (html) return { kind: "latex", html };
  if (m.image) return { kind: "image", src: m.image };
  return { kind: "text" };
}

const INLINE_PAD_PT = 0.5;  // ingest_upload.INLINE_PAD, vertical

/** CSS height of an inline formula's crop, in ems of the reader's text. */
export function imageHeight(m: InlineMath): string | undefined {
  const [, y0, , y1] = m.bbox;
  if (y0 === undefined || y1 === undefined || !(y1 > y0)) return undefined;
  return `${((y1 - y0 + 2 * INLINE_PAD_PT) / BODY_PT).toFixed(2)}em`;
}

export type Piece =
  | { kind: "text"; text: string; spoken: boolean }
  | { kind: "math"; math: InlineMath; text: string; spoken: boolean };

/** p.text[start:end] cut into text and the inline formulas inside it; `spoken` marks the piece the current word
 * (offsets into the slice) falls on. A formula is spoken as a whole while any of its words is. */
export function pieces(text: string, start: number, end: number, inline: InlineMath[] | undefined,
                       word?: { charStart: number; charEnd: number } | null): Piece[] {
  const out: Piece[] = [];
  const a = word ? Math.min(word.charStart, end - start) : -1, b = word ? Math.min(word.charEnd, end - start) : -1;
  const plain = (s: number, e: number) => {
    if (e <= s) return;
    if (a < b && a < e - start && b > s - start) {
      const ws = Math.max(s, start + a), we = Math.min(e, start + b);
      if (ws > s) out.push({ kind: "text", text: text.slice(s, ws), spoken: false });
      out.push({ kind: "text", text: text.slice(ws, we), spoken: true });
      if (e > we) out.push({ kind: "text", text: text.slice(we, e), spoken: false });
    } else {
      out.push({ kind: "text", text: text.slice(s, e), spoken: false });
    }
  };
  let pos = start;
  for (const m of [...(inline ?? [])].sort((x, y) => x.start - y.start)) {
    if (m.start < pos || m.end > end) continue;
    plain(pos, m.start);
    const spoken = a < b && a < m.end - start && b > m.start - start;
    out.push({ kind: "math", math: m, text: text.slice(m.start, m.end), spoken });
    pos = m.end;
  }
  plain(pos, end);
  return out;
}
