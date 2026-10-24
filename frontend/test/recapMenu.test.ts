// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RecapMenu, { menuKey, recapChoices } from "../src/reader/RecapMenu";
import { addNarration, addQuestion, appendSentence, fillNarration, finishAnswer, loadChat, priorTurns, resetChatMemory, saveChat } from "../src/talk/chatLog";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

const key = (el: Element, k: string) => act(() => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });

describe("RecapMenu", () => {
  it("offers the series option only for a book in a series", () => {
    expect(recapChoices(true).map((c) => c.label)).toEqual(["Last chapter", "This book so far", "The series so far"]);
    expect(recapChoices(false).map((c) => c.scope)).toEqual(["chapter", "book"]);
  });

  it("menuKey wraps arrows and jumps with Home/End", () => {
    expect(menuKey(2, 3, "ArrowDown")).toBe(0);
    expect(menuKey(0, 3, "ArrowUp")).toBe(2);
    expect(menuKey(1, 3, "Home")).toBe(0);
    expect(menuKey(1, 3, "End")).toBe(2);
    expect(menuKey(1, 3, "a")).toBeNull();
  });

  it("is an accessible menu button: opens on the first item, arrows move, Esc closes and refocuses, a pick reports", () => {
    const onPick = vi.fn();
    act(() => root.render(createElement(RecapMenu, { inSeries: true, onPick })));
    const btn = host.querySelector<HTMLButtonElement>("button[aria-haspopup='menu']")!;
    expect(btn.textContent).toBe("Recap");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    act(() => btn.click());
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    const menu = host.querySelector("[role='menu']")!;
    expect(btn.getAttribute("aria-controls")).toBe(menu.id);
    let items = [...host.querySelectorAll<HTMLButtonElement>("[role='menuitem']")];
    expect(items.map((i) => i.textContent)).toEqual(["Last chapter", "This book so far", "The series so far"]);
    expect(document.activeElement).toBe(items[0]);
    key(menu, "ArrowDown");
    expect(document.activeElement).toBe(items[1]);
    key(menu, "ArrowUp"); key(menu, "ArrowUp");
    expect(document.activeElement).toBe(items[2]);
    key(menu, "Escape");
    expect(host.querySelector("[role='menu']")).toBeNull();
    expect(document.activeElement).toBe(btn);
    key(btn, "ArrowUp"); // opens on the last item
    items = [...host.querySelectorAll<HTMLButtonElement>("[role='menuitem']")];
    expect(document.activeElement).toBe(items[2]);
    act(() => items[1].click());
    expect(onPick).toHaveBeenCalledWith({ scope: "book", label: "This book so far" });
    expect(host.querySelector("[role='menu']")).toBeNull();
  });

  it("closes on a click outside", () => {
    act(() => root.render(createElement(RecapMenu, { inSeries: false, onPick: () => {} })));
    act(() => host.querySelector<HTMLButtonElement>("button")!.click());
    expect(host.querySelectorAll("[role='menuitem']")).toHaveLength(2);
    act(() => { document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })); });
    expect(host.querySelector("[role='menu']")).toBeNull();
  });
});

describe("narrator recaps in the chat log", () => {
  it("are shown at once, filled later or dropped on failure", () => {
    let m = addNarration([], "Last chapter");
    expect(m[0]).toMatchObject({ role: "narrator", loading: true, text: "", recap: "Last chapter" });
    m = fillNarration(m, m[0].id, "Alice fell.", "Last chapter: I");
    expect(m[0]).toMatchObject({ text: "Alice fell.", loading: false, recap: "Last chapter: I" });
    expect(fillNarration(addNarration([], "x"), 1, null)).toEqual([]);
  });

  it("are left out of what the character remembers, and a pending answer stays pending around them", () => {
    let m = finishAnswer(appendSentence(addQuestion([], "Q1", "narrator", "typed"), "A1"));
    m = fillNarration(addNarration(m, "This book so far"), 3, "Recap text.");
    expect(priorTurns(m, "narrator")).toEqual([{ role: "user", text: "Q1" }, { role: "character", text: "A1" }]);
    expect(finishAnswer(m)).toBe(m); // a recap is never treated as a pending answer
  });

  it("survive the session store, except one that was still loading", () => {
    resetChatMemory(); sessionStorage.clear();
    saveChat("b", [...fillNarration(addNarration([], "a"), 1, "Done."), ...addNarration([{ id: 1, role: "user", speakerId: "x", text: "" }], "b").slice(1)]);
    resetChatMemory();
    expect(loadChat("b").map((x) => [x.role, x.text])).toEqual([["narrator", "Done."]]);
  });
});
