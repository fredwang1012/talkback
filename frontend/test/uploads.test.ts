import { describe, expect, it } from "vitest";
import { indexingLabel, pollIndexing, uploadBook, UploadConflict, type BookStatus, describeBook } from "../src/api";
import { conflictPatch, fileProblem, isBusyError, isDeletingConflict, jobFinished, jobLine, jobProgress, titleFromFile, uploadSettled, type UploadJob, autoFill } from "../src/uploads";
import { vi, afterEach } from "vitest";

afterEach(() => vi.unstubAllGlobals());

const job = (over: Partial<UploadJob>): UploadJob => ({ key: "k", title: "T", phase: "processing", ...over });
const ready = (over: Partial<BookStatus> = {}): BookStatus => ({ status: "ready", ...over });

describe("multi-file uploads", () => {
  it("makes a title from each file name", () => {
    expect(titleFromFile("my_book-final.pdf")).toBe("my book final");
    expect(titleFromFile("x".repeat(200) + ".txt")).toHaveLength(120);
  });

  it("checks each file", () => {
    expect(fileProblem({ name: "a.pdf", size: 10 })).toBeNull();
    expect(fileProblem({ name: "a.TXT", size: 10 })).toBeNull();
    expect(fileProblem({ name: "a.docx", size: 10 })).toMatch(/PDF/);
    expect(fileProblem({ name: "a.pdf", size: 21 * 1024 * 1024 })).toMatch(/20 MB/);
  });

  it("is settled only when indexing (running or queued) and enrichment are done", () => {
    expect(uploadSettled({ status: "processing" })).toBe(false);
    expect(uploadSettled({ status: "error", message: "x" })).toBe(true);
    expect(uploadSettled(ready({ index: { state: "queued", done: 0, total: 9 } }))).toBe(false);
    expect(uploadSettled(ready({ index: { state: "running", done: 3, total: 9 } }))).toBe(false);
    expect(uploadSettled(ready({ enrich: { status: "running" } }))).toBe(false);
    expect(uploadSettled(ready({ index: { state: "done", done: 9, total: 9 }, enrich: { status: "awaiting_confirmation" } }))).toBe(true);
  });

  it("each card says its own stage", () => {
    expect(jobLine(job({ phase: "uploading" }))).toBe("Uploading…");
    expect(jobLine(job({ status: { status: "processing", message: "Queued" } }))).toBe("Queued");
    expect(jobLine(job({ status: { status: "processing", message: "Reading the PDF" } }))).toBe("Reading the PDF…");
    expect(jobLine(job({ status: ready({ index: { state: "queued", done: 0, total: 9 } }) }))).toBe("Readable now · Queued for indexing");
    expect(jobLine(job({ status: ready({ index: { state: "running", done: 300, total: 1257 } }) }))).toBe("Readable now · Indexing 300 / 1257");
    expect(jobLine(job({ status: ready({ enrich: { status: "running", message: "Finding the cast" } }) }))).toBe("Readable now · Finding the cast");
    expect(jobLine(job({ phase: "ready", status: ready() }))).toBe("Ready");
    expect(jobLine(job({ phase: "error", message: "Bad PDF" }))).toBe("Bad PDF");
    expect(jobLine(job({ phase: "waiting", message: "Waiting for a free slot" }))).toBe("Waiting for a free slot");
  });

  it("shows a bar only while there is something to measure", () => {
    expect(jobProgress(job({ status: ready({ index: { state: "running", done: 25, total: 100 } }) }))).toBe(0.25);
    expect(jobProgress(job({ status: ready({ enrich: { status: "running", progress: 0.4 } }) }))).toBe(0.4);
    expect(jobProgress(job({ status: ready({ index: { state: "queued", done: 0, total: 100 } }) }))).toBeUndefined();
    expect(jobProgress(job({ status: { status: "processing" } }))).toBeUndefined();
  });

  it("treats a full queue or the rate limit as wait-and-retry", () => {
    expect(isBusyError(new Error("6 books are already being processed; try again shortly."))).toBe(true);
    expect(isBusyError(new Error("Too many uploads; wait a moment."))).toBe(true);
    expect(isBusyError(new Error("Upload a .pdf or .txt file."))).toBe(false);
  });

  it("queued indexing has its own label and keeps the poll going", async () => {
    expect(indexingLabel({ state: "queued", done: 0, total: 9 })).toBe("Queued for indexing");
    const f = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(ready({ index: { state: "queued", done: 0, total: 9 } }))))
      .mockResolvedValueOnce(new Response(JSON.stringify(ready({ index: { state: "done", done: 9, total: 9 } }))));
    vi.stubGlobal("fetch", f);
    const st = await pollIndexing("b", undefined, undefined, 1);
    expect(st?.index?.state).toBe("done");
    expect(f).toHaveBeenCalledTimes(2);
  });
});

describe("duplicate uploads (#82)", () => {
  const conflict = (body: object) => vi.fn(async () => new Response(JSON.stringify(body), { status: 409 }));

  it("a 409 for a file already in the library carries the existing book", async () => {
    vi.stubGlobal("fetch", conflict({ detail: "This file is already in your library as “Dune”.", reason: "duplicate", book_id: "u-dune-1", title: "Dune" }));
    const err = await uploadBook(new File(["x"], "a.txt"), "A", "", "novel").catch((e) => e);
    expect(err).toBeInstanceOf(UploadConflict);
    expect(err).toMatchObject({ reason: "duplicate", bookId: "u-dune-1", message: "This file is already in your library as “Dune”." });
    expect(conflictPatch(err)).toEqual({ phase: "exists", message: err.message, existing: "u-dune-1" });
  });

  it("a book still being deleted means wait and retry, not a failure", async () => {
    vi.stubGlobal("fetch", conflict({ detail: "This book is still being deleted — try again in a moment.", reason: "deleting", book_id: "u-x-1" }));
    const err = await uploadBook(new File(["x"], "a.txt"), "A", "", "novel").catch((e) => e);
    expect(isDeletingConflict(err)).toBe(true);
    expect(isDeletingConflict(new Error("nope"))).toBe(false);
  });

  it("the twin of an upload in flight is shown without an open link", async () => {
    vi.stubGlobal("fetch", conflict({ detail: "This file is already being added to your library.", reason: "processing", book_id: "u-x-1" }));
    const err = await uploadBook(new File(["x"], "a.txt"), "A", "", "novel").catch((e) => e);
    expect(conflictPatch(err)).toEqual({ phase: "exists", message: "This file is already being added to your library." });
    expect(conflictPatch(new Error("other"))).toBeNull();
  });

  it("the card says so and can be dismissed", () => {
    const j = job({ phase: "exists", message: "This file is already in your library as “Dune”.", existing: "u-dune-1" });
    expect(jobLine(j)).toBe("This file is already in your library as “Dune”.");
    expect(jobFinished(j)).toBe(true);
    expect(jobFinished(job({ phase: "processing" }))).toBe(false);
  });
});

describe("auto title and author (#83)", () => {
  it("fills only what the reader has not typed", () => {
    const guess = { title: "The Time Machine", author: "H. G. Wells" };
    expect(autoFill({ title: "tm", author: "" }, guess, { title: false, author: false })).toEqual(guess);
    expect(autoFill({ title: "Mine", author: "" }, guess, { title: true, author: false })).toEqual({ title: "Mine", author: "H. G. Wells" });
    expect(autoFill({ title: "tm", author: "Me" }, { title: "", author: "" }, { title: false, author: true })).toEqual({ title: "tm", author: "Me" });
  });

  it("asks the server with the file and returns its guess", async () => {
    const f = vi.fn(async (_u: string, _i?: RequestInit) => new Response(JSON.stringify({ title: "Dune", author: "Frank Herbert", source: "llm" })));
    vi.stubGlobal("fetch", f);
    expect(await describeBook(new File(["x"], "dune.pdf"))).toEqual({ title: "Dune", author: "Frank Herbert", source: "llm" });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("/api/books/describe");
    expect(((init?.body as FormData).get("file") as File).name).toBe("dune.pdf");
  });

  it("sends only the start of a text file", async () => {
    const f = vi.fn(async (_u: string, _i?: RequestInit) => new Response(JSON.stringify({ title: "", author: "", source: "none" })));
    vi.stubGlobal("fetch", f);
    await describeBook(new File(["x".repeat(100_000)], "big.txt"));
    const sent = ((f.mock.calls[0][1]?.body as FormData).get("file") as File);
    expect(sent.name).toBe("big.txt");
    expect(sent.size).toBe(24_000);
  });

  it("a failed guess is just no guess", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 429 })));
    expect(await describeBook(new File(["x"], "a.pdf"))).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    expect(await describeBook(new File(["x"], "a.pdf"))).toBeNull();
  });
});
