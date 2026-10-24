import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { VolumeNote, noteFromEnrich } from "../src/pages/VolumeNote";

const MSG = "Character processing was interrupted; you can try again. The book is still readable with the narrator.";

describe("noteFromEnrich", () => {
  it("offers a retry for an interrupted or failed job", () => {
    expect(noteFromEnrich("b1", { status: "error", message: MSG })).toEqual({ id: "b1", text: MSG, retry: true });
    expect(noteFromEnrich("b1", { status: "error" })?.text).toBe("Character processing did not run.");
  });
  it("keeps refusals and confirmations as before, and shows nothing otherwise", () => {
    expect(noteFromEnrich("b1", { status: "refused", message: "Too long" })).toEqual({ id: "b1", text: "Too long", retry: true });
    expect(noteFromEnrich("b1", { status: "awaiting_confirmation", estimate_usd: 0.5 })?.confirm).toBe(0.5);
    expect(noteFromEnrich("b1", { status: "done" })).toBeNull();
    expect(noteFromEnrich("b1", undefined)).toBeNull();
  });
});

function find(node: ReactNode, pred: (e: ReactElement<Record<string, unknown>>) => boolean): ReactElement<Record<string, unknown>> | null {
  if (!isValidElement(node)) return null;
  const el = node as ReactElement<Record<string, unknown>>;
  if (pred(el)) return el;
  const kids = el.props.children as ReactNode;
  for (const k of Array.isArray(kids) ? kids : [kids]) { const hit = find(k, pred); if (hit) return hit; }
  return null;
}

describe("VolumeNote", () => {
  const book = { id: "b1", title: "Alice" };
  it("shows an accessible Try again button that calls onRetry", () => {
    const onRetry = vi.fn();
    const tree = VolumeNote({ title: book.title, note: { id: "b1", text: MSG, retry: true }, onConfirm: vi.fn(), onRetry });
    const html = renderToStaticMarkup(tree);
    expect(html).toContain("Try again");
    expect(html).toContain('aria-label="Try processing Alice again"');
    expect(html).toContain('role="status"');
    (find(tree, (e) => e.type === "button")!.props.onClick as () => void)();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
  it("has no retry button for a plain note", () => {
    const html = renderToStaticMarkup(createElement(VolumeNote, { title: "Alice", note: { id: "b1", text: "Working" }, onConfirm: vi.fn(), onRetry: vi.fn() }));
    expect(html).not.toContain("Try again");
  });
});
