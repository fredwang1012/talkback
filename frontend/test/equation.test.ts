import { describe, expect, it } from "vitest";
import { equationView, imageWidth, typeset, withNumber } from "../src/reader/equation";

const base = { id: "e1", page: 3, bbox: [0, 0, 10, 10] };

describe("equationView", () => {
  it("typesets LaTeX when it parses", () => {
    const v = equationView({ ...base, latex: "\\hat\\beta_1 = \\frac{a}{b}", image: "/api/books/b/equations/e1.png" });
    expect(v.kind).toBe("latex");
    if (v.kind === "latex") expect(v.html).toContain("katex-display");
  });

  it("falls back to the crop when the LaTeX does not parse", () => {
    expect(equationView({ ...base, latex: "\\frac{a", image: "/x.png" })).toEqual({ kind: "image", src: "/x.png" });
  });

  it("falls back to the text with neither", () => {
    expect(equationView({ ...base })).toEqual({ kind: "text" });
    expect(equationView(null)).toEqual({ kind: "text" });
  });

  it("sets the equation number as a tag, once", () => {
    expect(withNumber("y = x", "3.4")).toBe("y = x \\tag{3.4}");
    expect(withNumber("y = x \\tag{1}", "3.4")).toBe("y = x \\tag{1}");
    expect(withNumber("y = x", null)).toBe("y = x");
    const v = equationView({ ...base, latex: "y = x", number: "3.4" });
    expect(v.kind === "latex" && v.html).toContain("3.4");
  });

  it("sizes a crop by its width in the PDF, in ems of the reader's text", () => {
    expect(imageWidth({ ...base, bbox: [100, 0, 296, 20] })).toBe("20.00em");   // 196pt + 2pt padding each side
    expect(imageWidth({ ...base, bbox: [] })).toBeUndefined();
    expect(imageWidth(null)).toBeUndefined();
  });

  it("refuses LaTeX that would add links or raw HTML, so the crop shows instead", () => {
    expect(typeset("\\href{javascript:alert(1)}{x}")).toBeNull();
    expect(typeset("\\htmlClass{evil}{x}")).toBeNull();
    expect(equationView({ ...base, latex: "\\url{http://x}", image: "/x.png" }).kind).toBe("image");
  });
});
