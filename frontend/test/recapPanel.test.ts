// @vitest-environment jsdom
import { createElement, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RecapPanel, { speechParts } from "../src/reader/RecapPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

async function show(body: object, onClose = () => {}) {
  const f = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify(body), { status: 200 }));
  vi.stubGlobal("fetch", f);
  await act(async () => { root.render(createElement(RecapPanel, { bookId: "hp1", para: 12, voice: null, onClose })); });
  return f;
}

describe("RecapPanel", () => {
  it("shows the chapter and the recap text, and asks for the reader's paragraph", async () => {
    const f = await show({ upto_para: 10, chapter: "The Boy Who Lived", text: "Harry was left on a doorstep.", earlier: "" });
    expect(f.mock.calls[0][0]).toBe("/api/books/hp1/recap?para=12");
    const dlg = host.querySelector('[role="dialog"]')!;
    expect(dlg.getAttribute("aria-labelledby")).toBe("recap-title");
    expect(host.querySelector("#recap-title")!.textContent).toBe("Previously: The Boy Who Lived");
    expect(dlg.textContent).toContain("Harry was left on a doorstep.");
    expect(host.querySelector("details")).toBeNull();
  });

  it("says there is nothing to recap yet when the text is empty", async () => {
    await show({ upto_para: null, chapter: "I.", text: "", earlier: "" });
    expect(host.textContent).toContain("Nothing to recap yet — keep reading.");
    expect(host.textContent).not.toContain("Read aloud");
  });

  it("offers earlier books in a collapsible section", async () => {
    await show({ upto_para: 10, chapter: "II", text: "Now.", earlier: "Book One: Harry went to school." });
    const d = host.querySelector("details")!;
    expect(d.querySelector("summary")!.textContent).toBe("Earlier books");
    expect(d.textContent).toContain("Book One: Harry went to school.");
    expect(d.open).toBe(false);
  });

  it("focuses the close button and closes on it and on Escape", async () => {
    const onClose = vi.fn();
    await show({ upto_para: null, chapter: "", text: "", earlier: "" }, onClose);
    const btn = host.querySelector<HTMLButtonElement>('button[aria-label="Close recap"]')!;
    expect(document.activeElement).toBe(btn);
    act(() => btn.click());
    act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(onClose).toHaveBeenCalledTimes(2);
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
