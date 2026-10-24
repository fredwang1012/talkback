// @vitest-environment jsdom
import { createElement, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RecapPanel, { nothingYet, recapHeading } from "../src/reader/RecapPanel";
import { speechParts } from "../src/reader/readAloud";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

const lastChapter = { scope: "chapter", label: "Last chapter" } as const;

async function show(body: object, onClose = () => {}, para = 12) {
  const f = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status: 200 }));
  vi.stubGlobal("fetch", f);
  await act(async () => { root.render(createElement(RecapPanel, { bookId: "hp1", para, choice: lastChapter, onClose })); });
  return f;
}

describe("RecapPanel (text-only fallback)", () => {
  it("asks for the chosen scope at the reader's paragraph and shows the chapter it covers", async () => {
    const f = await show({ scope: "chapter", upto_para: 10, chapter: "The Boy Who Lived", text: "Harry was left on a doorstep." });
    expect(f.mock.calls[0][0]).toBe("/api/books/hp1/recap?para=12&scope=chapter");
    expect(host.querySelector("#recap-title")!.textContent).toBe("Last chapter: The Boy Who Lived");
    expect(host.querySelector('[role="dialog"]')!.textContent).toContain("Harry was left on a doorstep.");
  });

  it("says there is nothing to recap yet, or that this is the start", async () => {
    await show({ scope: "chapter", upto_para: null, chapter: "I.", text: "" });
    expect(host.textContent).toContain("Nothing to recap yet — keep reading.");
    expect(nothingYet(0)).toBe("You're at the start of this book.");
  });

  it("focuses the close button and closes on it and on Escape", async () => {
    const onClose = vi.fn();
    await show({ scope: "chapter", upto_para: null, chapter: "", text: "" }, onClose);
    const btn = host.querySelector<HTMLButtonElement>('button[aria-label="Close recap"]')!;
    expect(document.activeElement).toBe(btn);
    act(() => btn.click());
    act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("headings name the chapter only for the last-chapter scope", () => {
    expect(recapHeading(lastChapter, { chapter: "II" })).toBe("Last chapter: II");
    expect(recapHeading({ scope: "book", label: "This book so far" }, { chapter: "II" })).toBe("This book so far");
  });
});

describe("speechParts", () => {
  it("keeps every part within the speak limit and loses no words", () => {
    const text = Array.from({ length: 60 }, (_, i) => `Sentence number ${i} goes here.`).join(" ");
    const parts = speechParts(text, 380);
    expect(parts.every((p) => p.length <= 380)).toBe(true);
    expect(parts.join(" ")).toBe(text);
  });
});
