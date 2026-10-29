import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// #33: the expanded player scrolls, so without touch-action the browser takes a finger drag on the face for a scroll
// and sends pointercancel: the swipe down to collapse only ever worked with a mouse.
const css = readFileSync(new URL("../src/index.css", import.meta.url), "utf8");
const rule = (sel: string) => {
  const at = css.indexOf(`\n${sel} {`);
  return at < 0 ? "" : css.slice(at, css.indexOf("}", at));
};

describe("expanded player: swipe down on a touch screen", () => {
  it("the face keeps a finger drag for the swipe", () => {
    expect(rule(".avatar-player.expanded .ap-face")).toMatch(/touch-action:\s*none/);
  });
});
