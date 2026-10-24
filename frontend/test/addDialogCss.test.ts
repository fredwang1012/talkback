import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Tailwind's preflight sets `margin: 0` on every element, which removes the browser's `margin: auto` that centres a
// modal <dialog>: the Add book form then opened in the top-left corner (#91). The dialog must set its own margin.
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const rule = (sel: string) => {
  const at = css.indexOf(`\n${sel} {`);
  return at < 0 ? "" : css.slice(at, css.indexOf("}", at));
};

describe("Add book dialog", () => {
  it("is centred in the viewport despite the CSS reset", () => {
    const r = rule(".add-dialog");
    expect(r).toMatch(/margin:\s*auto/);
    expect(r).toMatch(/inset:\s*0/);
    expect(r).toMatch(/position:\s*fixed/);
  });

  it("fits a 390px phone with a 16px gutter", () => {
    expect(rule(".add-dialog")).toMatch(/width:\s*min\(560px,\s*calc\(100vw - 32px\)\)/);
    expect(rule(".add-dialog")).toMatch(/max-height:\s*calc\(100dvh - 32px\)/);
  });
});
