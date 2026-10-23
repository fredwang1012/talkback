import katex from "katex";
import type { MathBlock } from "../api";

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
export function typeset(latex: string): string | null {
  const hit = cache.get(latex);
  if (hit !== undefined) return hit;
  let html: string | null;
  try {
    html = katex.renderToString(latex, {
      displayMode: true, throwOnError: true, strict: "ignore", trust: false, maxExpand: 500, errorColor: REFUSED,
    });
    if (html.includes(REFUSED)) html = null;
  } catch {
    html = null;
  }
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string);
  cache.set(latex, html);
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
