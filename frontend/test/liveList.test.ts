import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isProcessing, sameList, stageKey, startListRefresh } from "../src/liveList";
import { groupShelves } from "../src/shelves";
import type { BookStatus, BookSummary } from "../src/api";
import type { UploadJob } from "../src/uploads";

const book = (id: string, over: Partial<BookSummary> = {}): BookSummary =>
  ({ id, title: id, author: "Frank Herbert", kind: "novel", sections: 1, paragraphs: 2, characters: 0, ...over }) as BookSummary;
const job = (over: Partial<UploadJob>): UploadJob => ({ key: "k", title: "T", phase: "processing", ...over });

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("live list refresh", () => {
  it("refetches every interval while active and stops on stop()", async () => {
    const fetch = vi.fn().mockResolvedValue([book("a")]);
    const apply = vi.fn();
    const r = startListRefresh({ fetch, apply, ms: 10_000 });
    await vi.advanceTimersByTimeAsync(25_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    r.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("poke() refetches at once", async () => {
    const fetch = vi.fn().mockResolvedValue([]);
    const r = startListRefresh({ fetch, apply: vi.fn() });
    r.poke();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(1);
    r.stop();
  });

  it("a failed fetch is ignored and a stopped refresher never applies a late answer", async () => {
    const apply = vi.fn();
    const fetch = vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValue([book("a")]);
    const r = startListRefresh({ fetch, apply, ms: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(apply).not.toHaveBeenCalled();
    let release: (v: BookSummary[]) => void = () => undefined;
    fetch.mockImplementationOnce(() => new Promise((res) => { release = res; }));
    await vi.advanceTimersByTimeAsync(1000);
    r.stop();
    release([book("b")]);
    await vi.advanceTimersByTimeAsync(0);
    expect(apply).not.toHaveBeenCalled();
  });

  it("sameList keeps state when nothing changed (no flicker)", () => {
    expect(sameList([book("a")], [book("a")])).toBe(true);
    expect(sameList([book("a")], [book("a", { series: "dune" })])).toBe(false);
    expect(sameList(null, [])).toBe(false);
  });

  it("is processing while any upload job is unsettled, or the server still works on a listed book", () => {
    expect(isProcessing([job({ phase: "processing" })], 0)).toBe(true);
    expect(isProcessing([job({ phase: "uploading" })], 0)).toBe(true);
    expect(isProcessing([job({ phase: "ready" }), job({ phase: "error" }), job({ phase: "confirm" })], 0)).toBe(false);
    expect(isProcessing([], 1)).toBe(true);
    expect(isProcessing([], 0)).toBe(false);
  });

  it("stageKey changes with the stage, not with progress", () => {
    const s = (over: Partial<BookStatus>): BookStatus => ({ status: "ready", ...over });
    const a = stageKey(s({ enrich: { status: "running", stage: "queued", progress: 0 } }));
    expect(stageKey(s({ enrich: { status: "running", stage: "queued", progress: 0.5 } }))).toBe(a);
    expect(stageKey(s({ enrich: { status: "running", stage: "cast", progress: 0.1 } }))).not.toBe(a);
    expect(stageKey(s({ enrich: { status: "done", stage: "done" } }))).not.toBe(a);
  });

  it("grouping updates when a refetched list gives a book its series", () => {
    const before = groupShelves([book("u-1", { title: "Dune" }), book("u-2", { title: "Dune Messiah" })]);
    expect(before.series).toHaveLength(0);
    expect(before.standalone).toHaveLength(2);
    const after = groupShelves([book("u-1", { title: "Dune", series: "dune", series_index: 1 }),
                                book("u-2", { title: "Dune Messiah", series: "dune", series_index: 2 })]);
    expect(after.series).toHaveLength(1);
    expect(after.series[0].books.map((b) => b.id)).toEqual(["u-1", "u-2"]);
    expect(after.standalone).toHaveLength(0);
  });
});
