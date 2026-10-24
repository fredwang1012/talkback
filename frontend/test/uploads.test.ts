import { describe, expect, it } from "vitest";
import { indexingLabel, pollIndexing, type BookStatus } from "../src/api";
import { fileProblem, isBusyError, jobLine, jobProgress, titleFromFile, uploadSettled, type UploadJob } from "../src/uploads";
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
