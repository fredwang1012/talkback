import { afterEach, describe, expect, it, vi } from "vitest";
import { enrichDoneNote, fetchBooks, pollBookStatus, pollEnrichment, startEnrichment, timedFetch, uploadBook } from "../src/api";
import { setAccessToken } from "../src/accessToken";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

function hangingFetch() {
  return vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_res, rej) => {
    init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
  }));
}

describe("timedFetch", () => {
  it("turns a stalled request into a readable error", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());
    const p = timedFetch("/api/x", {}, 500).catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(600);
    expect(await p).toContain("took too long");
  });

  it("passes responses through and does not abort a fast request", async () => {
    const fetchMock = vi.fn(async () => new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await fetchBooks()).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not rewrite ordinary network errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(timedFetch("/api/x")).rejects.toThrow("Failed to fetch");
  });
});

describe("uploadBook", () => {
  afterEach(() => setAccessToken(""));

  it("sends multipart fields with the auth header and no manual Content-Type", async () => {
    setAccessToken("sesame");
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => new Response(JSON.stringify({ book_id: "b1", status: "processing" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["hello"], "dune.txt", { type: "text/plain" });
    expect(await uploadBook(file, "Dune", "Herbert", "novel")).toMatchObject({ book_id: "b1" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/books/upload");
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Talkback-Token"]).toBe("sesame");
    expect(headers["Content-Type"]).toBeUndefined();
    const form = init?.body as FormData;
    expect((form.get("file") as File).name).toBe("dune.txt");
    expect(form.get("title")).toBe("Dune");
    expect(form.get("author")).toBe("Herbert");
    expect(form.get("kind")).toBe("novel");
  });

  it("surfaces the server's detail message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Another book is still being processed" }), { status: 429 })));
    await expect(uploadBook(new File(["x"], "a.txt"), "A", "", "novel")).rejects.toThrow("Another book is still being processed");
  });
});

describe("pollBookStatus", () => {
  it("polls until ready, then stops", async () => {
    vi.useFakeTimers();
    const replies = ["processing", "processing", "ready"];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: replies.shift() }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const p = pollBookStatus("b1");
    await vi.advanceTimersByTimeAsync(5000);
    expect(await p).toEqual({ status: "ready" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("returns the error message and stops on error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: "error", message: "bad pdf" }), { status: 200 })));
    expect(await pollBookStatus("b1")).toEqual({ status: "error", message: "bad pdf" });
  });

  it("stops when aborted", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: "processing" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const ctl = new AbortController();
    const p = pollBookStatus("b1", ctl.signal);
    await vi.advanceTimersByTimeAsync(1600);
    ctl.abort();
    expect(await p).toBeNull();
    const n = fetchMock.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(n);
  });

  it("tolerates 3 failed status calls in a row, then gives up on the 4th", async () => {
    vi.useFakeTimers();
    const seq = [false, false, true, false, false, false, false];
    const fetchMock = vi.fn(async () => {
      const ok = seq.shift();
      return ok ? new Response(JSON.stringify({ status: "processing" }), { status: 200 }) : new Response("{}", { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const p = pollBookStatus("b1").catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(20000);
    expect(await p).toContain("Status check failed (500)");
    expect(fetchMock).toHaveBeenCalledTimes(7); // success in the middle reset the counter
  });

  it("gives up after the time limit", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: "processing" }), { status: 200 })));
    const p = pollBookStatus("b1", undefined, 1500, 6000).catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(10000);
    expect(await p).toContain("taking too long");
  });
});

describe("novel enrichment polling", () => {
  const reply = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

  it("reports progress while running and stops when done", async () => {
    vi.useFakeTimers();
    const seq = [
      { status: "ready", enrich: { status: "running", stage: "cast", progress: 0.05, message: "Finding the main characters" } },
      { status: "ready", enrich: { status: "running", stage: "segment", progress: 0.4, message: "Splitting the dialogue by character" } },
      { status: "ready", enrich: { status: "done", stage: "done", progress: 1 } },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => reply(seq.shift())));
    const seen: number[] = [];
    const p = pollEnrichment("b1", (s) => seen.push(s.enrich?.progress ?? -1));
    await vi.advanceTimersByTimeAsync(10000);
    expect((await p)?.enrich?.status).toBe("done");
    expect(seen).toEqual([0.05, 0.4]);
  });

  it("stops at the confirmation step with the estimate, and for books without enrichment", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply({ status: "ready", enrich: { status: "awaiting_confirmation", estimate_usd: 1.23 } })));
    expect((await pollEnrichment("b1"))?.enrich?.estimate_usd).toBe(1.23);
    vi.stubGlobal("fetch", vi.fn(async () => reply({ status: "ready" })));
    expect(await pollEnrichment("b1")).toEqual({ status: "ready" });
  });

  it("startEnrichment posts to the enrich endpoint with the access token", async () => {
    setAccessToken("sesame");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: "ready", enrich: { status: "running" } }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await startEnrichment("b 1")).enrich?.status).toBe("running");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/books/b%201/enrich");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-Talkback-Token"]).toBe("sesame");
    setAccessToken("");
  });

  it("startEnrichment surfaces the server message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "This book is not waiting for confirmation." }), { status: 409 })));
    await expect(startEnrichment("b1")).rejects.toThrow("not waiting");
  });
});

describe("enrichDoneNote", () => {
  it("says so when the fallback was used", () => {
    const msg = "Characters detected with a simple fallback (no LLM key configured).";
    expect(enrichDoneNote({ status: "done", degraded: true, message: msg })).toBe(msg);
  });
  it("keeps the success wording otherwise", () => {
    expect(enrichDoneNote({ status: "done", message: "Characters, recap and search are ready." })).toBe("characters, voices and recap are ready");
  });
});
