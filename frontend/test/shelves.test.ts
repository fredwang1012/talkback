import { afterEach, describe, expect, it, vi } from "vitest";
import { changeKind, deleteBook, fetchHiddenBooks, hideBook, restoreBook, waitForDeletion, type BookSummary } from "../src/api";
import { canDelete, canMove, groupShelves, hiddenLabel, kindLabel, moveLabel, moveTarget, removeKind, removeLabel, seriesTitle } from "../src/shelves";

afterEach(() => vi.unstubAllGlobals());

const book = (id: string, over: Partial<BookSummary> = {}): BookSummary => ({
  id, title: id, author: "", kind: "novel", license: "", attribution: null, paragraphs: 1, sections: 1,
  characters: 0, cover: null, ...over,
});

describe("groupShelves", () => {
  const books = [
    book("hp3", { title: "Prisoner", series: "harry-potter", series_index: 3 }),
    book("u-zed-1", { title: "Zed" }),
    book("hp1", { title: "Stone", series: "harry-potter", series_index: 1 }),
    book("u-bio-2", { title: "Biology", kind: "textbook" }),
    book("hp2", { title: "Chamber", series: "harry-potter", series_index: 2 }),
    book("u-alpha-3", { title: "Alpha" }),
    book("u-algebra-4", { title: "Algebra", kind: "textbook" }),
  ];
  const s = groupShelves(books);

  it("keeps a series together in series_index order", () => {
    expect(s.series).toHaveLength(1);
    expect(s.series[0].books.map((b) => b.id)).toEqual(["hp1", "hp2", "hp3"]);
  });
  it("lists standalone novels after, by title, and textbooks on their own shelf", () => {
    expect(s.standalone.map((b) => b.title)).toEqual(["Alpha", "Zed"]);
    expect(s.textbooks.map((b) => b.title)).toEqual(["Algebra", "Biology"]);
  });
  it("handles empty shelves", () => {
    expect(groupShelves([])).toEqual({ series: [], standalone: [], textbooks: [] });
    expect(groupShelves([book("a")]).textbooks).toEqual([]);
  });
  it("a book moved to textbooks leaves the series group", () => {
    const moved = groupShelves([book("hp1", { series: "s", series_index: 1, kind: "textbook" })]);
    expect(moved.series).toEqual([]);
    expect(moved.textbooks).toHaveLength(1);
  });
});

describe("move helpers", () => {
  it("only uploaded books can move", () => {
    expect(canMove({ id: "u-x-12345678" })).toBe(true);
    expect(canMove({ id: "hp1-sorcerers-stone" })).toBe(false);
  });
  it("labels and targets", () => {
    expect(moveLabel({ kind: "novel" })).toBe("Move to Textbooks");
    expect(moveTarget({ kind: "novel" })).toBe("textbook");
    expect(moveLabel({ kind: "textbook" })).toBe("Move to Novels");
    expect(moveTarget({ kind: "textbook" })).toBe("novel");
    expect(kindLabel("textbook")).toBe("Textbook");
    expect(seriesTitle("harry-potter")).toBe("Harry Potter");
  });
});

describe("changeKind", () => {
  it("posts the kind and returns the status", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready", kind: "novel" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await changeKind("u-x-1", "novel")).toMatchObject({ kind: "novel" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/books/u-x-1/kind");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ kind: "novel" });
  });
  it("surfaces the server message (e.g. 409 while processing)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "still being processed" }), { status: 409 })));
    await expect(changeKind("u-x-1", "novel")).rejects.toThrow("still being processed");
  });
});

describe("delete", () => {
  it("is a real delete only for uploaded books", () => {
    expect(canDelete(book("u-mine-1a2b3c4d"))).toBe(true);
    expect(canDelete(book("hp1"))).toBe(false);
    expect(canDelete(book("alice-in-wonderland"))).toBe(false);
  });

  it("built-in books are hidden instead (every book can be removed)", () => {
    expect(removeKind(book("u-mine-1a2b3c4d"))).toBe("delete");
    expect(removeKind(book("hp1"))).toBe("hide");
    expect(removeKind(book("alice-in-wonderland"))).toBe("hide");
    expect(removeLabel(book("u-x-1"))).toBe("Delete");
    expect(removeLabel(book("hp1"))).toBe("Hide");
  });

  it("labels the hidden-books control", () => {
    expect(hiddenLabel(0)).toBe("");
    expect(hiddenLabel(1)).toBe("Hidden books (1)");
    expect(hiddenLabel(7)).toBe("Hidden books (7)");
  });

  it("hides, restores and lists hidden books", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ hidden: "hp1" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ restored: "hp1" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{ id: "hp1", title: "Stone", author: "J" }]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await hideBook("hp1");
    await restoreBook("hp1");
    expect(await fetchHiddenBooks()).toEqual([{ id: "hp1", title: "Stone", author: "J" }]);
    expect(fetchMock.mock.calls.map((c) => [c[0], c[1]?.method])).toEqual([
      ["/api/books/hp1/hide", "POST"], ["/api/books/hp1/restore", "POST"], ["/api/hidden-books", undefined],
    ]);
  });

  it("surfaces a refused hide (anonymous visitor)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Sign in to delete things." }), { status: 403 })));
    await expect(hideBook("hp1")).rejects.toThrow(/Sign in/);
  });

  it("sends DELETE to the book and resolves on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ deleted: "u-x" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await deleteBook("u-x");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/books/u-x");
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
  });

  it("treats 404 as already deleted (a retry after the first request timed out but finished)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Book not found" }), { status: 404 })));
    await expect(deleteBook("u-x")).resolves.toBeUndefined();
  });

  it("surfaces the server's reason (409 while processing)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "still being processed" }), { status: 409 })));
    await expect(deleteBook("u-x")).rejects.toThrow(/still being processed/);
  });

  const status = (body: unknown, code = 200) => new Response(JSON.stringify(body), { status: code });

  it("waits in the background until the book is gone (404 = done)", async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(status({ status: "deleting" }))
      .mockResolvedValueOnce(status({ status: "deleting" }))
      .mockResolvedValueOnce(status({ detail: "Book not found" }, 404));
    vi.stubGlobal("fetch", f);
    expect(await waitForDeletion("u-x", undefined, 1)).toEqual({ ok: true });
    expect(f).toHaveBeenCalledTimes(3);
    expect(f.mock.calls[0][0]).toBe("/api/books/u-x/status");
  });

  it("reports a delete that failed part-way, with the server's message", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(status({ status: "delete_error", message: "Could not remove the book from search" })));
    expect(await waitForDeletion("u-x", undefined, 1)).toEqual({ ok: false, message: "Could not remove the book from search" });
  });

  it("a status that is no longer deleting counts as done; abort stops quietly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(status({ status: "ready" })));
    expect(await waitForDeletion("u-x", undefined, 1)).toEqual({ ok: true });
    const ctl = new AbortController(); ctl.abort();
    expect(await waitForDeletion("u-x", ctl.signal, 1)).toBeNull();
  });
});

import { dropReferences } from "../src/reader/castLogic";
import { deleteVoice } from "../src/api";

describe("deleting voices and faces", () => {
  it("sends DELETE and shows the server's refusal for built-in voices", async () => {
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "built-in" }), { status: 403 }));
    vi.stubGlobal("fetch", f);
    await expect(deleteVoice("voice_x")).rejects.toThrow(/built-in/);
    expect(f.mock.calls[0][0]).toBe("/api/voices/voice_x");
    expect(f.mock.calls[0][1].method).toBe("DELETE");
  });

  it("drops browser-side choices that pointed at the deleted voice or face", () => {
    const r = dropReferences({ mode: "one", voice: "v1", face: "f1" }, { voice: "v1", face: "none" }, { voice: "v1" });
    expect(r.style).toEqual({ mode: "one", voice: null, face: "f1" });
    expect(r.talk).toEqual({ voice: null, face: "none" });
    const g = dropReferences({ mode: "cast", voice: "v2", face: "f1" }, { voice: "v2", face: "f1" }, { face: "f1" });
    expect(g.style.face).toBeNull(); expect(g.talk.face).toBeNull(); expect(g.style.voice).toBe("v2");
  });
});
