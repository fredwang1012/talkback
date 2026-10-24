import { describe, expect, it } from "vitest";
import type { InlineMath } from "../src/api";
import { imageHeight, inlineView, pieces, typeset } from "../src/reader/equation";

const text = "The slope ˆβ1 is unbiased, and ˆβ0 too.";
const m = (start: number, end: number, extra: Partial<InlineMath> = {}): InlineMath =>
  ({ id: `m${start}`, start, end, page: 0, bbox: [0, 100, 10, 112], ...extra });
const b1 = m(10, 13), b0 = m(31, 34);

describe("inline formulas", () => {
  it("typesets in text style, else the crop, else the text", () => {
    const v = inlineView({ ...b1, latex: "\\hat\\beta_1", image: "/x.png" });
    expect(v.kind).toBe("latex");
    if (v.kind === "latex") expect(v.html).not.toContain("katex-display");
    expect(inlineView({ ...b1, latex: "\\frac{a", image: "/x.png" })).toEqual({ kind: "image", src: "/x.png" });
    expect(inlineView(b1)).toEqual({ kind: "text" });
    expect(typeset("x", false)).not.toBe(typeset("x", true));
  });

  it("sizes a crop by its height in the PDF", () => {
    expect(imageHeight(b1)).toBe("1.30em");   // 12pt + 0.5pt padding above and below
    expect(imageHeight({ ...b1, bbox: [] })).toBeUndefined();
  });

  it("cuts a sentence into text and formulas", () => {
    expect(pieces(text, 0, text.length, [b0, b1]).map((x) => [x.kind, x.text])).toEqual([
      ["text", "The slope "], ["math", "ˆβ1"], ["text", " is unbiased, and "], ["math", "ˆβ0"], ["text", " too."],
    ]);
  });

  it("works on a chunk slice and skips formulas outside it", () => {
    expect(pieces(text, 14, text.length, [b0, b1]).map((x) => x.text)).toEqual(["is unbiased, and ", "ˆβ0", " too."]);
  });

  it("marks the spoken word, and the whole formula while it is read", () => {
    const word = pieces(text, 0, text.length, [b1], { charStart: 4, charEnd: 9 });
    expect(word.filter((x) => x.spoken).map((x) => x.text)).toEqual(["slope"]);
    const formula = pieces(text, 0, text.length, [b1], { charStart: 10, charEnd: 13 });
    expect(formula.filter((x) => x.spoken).map((x) => [x.kind, x.text])).toEqual([["math", "ˆβ1"]]);
  });
});
