import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchBooks, timedFetch } from "../src/api";

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
