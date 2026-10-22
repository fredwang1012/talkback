import { afterEach, describe, expect, it, vi } from "vitest";
import { changeKind, type BookSummary } from "../src/api";
import { canMove, groupShelves, kindLabel, moveLabel, moveTarget, seriesTitle } from "../src/shelves";

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
