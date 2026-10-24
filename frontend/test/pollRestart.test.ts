import { afterEach, describe, expect, it, vi } from "vitest";
import { isUnreachable, pollBookStatus } from "../src/api";

describe("status polling rides out a backend restart", () => {
  afterEach(() => vi.restoreAllMocks());

  it("treats proxy 502/503/504 and network failures as unreachable, not 404 or 409", () => {
    expect(isUnreachable(Object.assign(new Error("x"), { status: 502 }))).toBe(true);
    expect(isUnreachable(Object.assign(new Error("x"), { status: 504 }))).toBe(true);
    expect(isUnreachable(new TypeError("Failed to fetch"))).toBe(true);
    expect(isUnreachable(Object.assign(new Error("x"), { status: 404 }))).toBe(false);
    expect(isUnreachable(Object.assign(new Error("x"), { status: 409 }))).toBe(false);
  });

  it("keeps polling through more than 3 failed checks while the backend restarts", async () => {
    let n = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      n += 1;
      if (n <= 6) return new Response("bad gateway", { status: 502 });
      return new Response(JSON.stringify({ status: "ready" }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const s = await pollBookStatus("u-x-00000000", undefined, 1, 60_000);
    expect(s?.status).toBe("ready");
    expect(n).toBe(7);
  });

  it("still gives up quickly on a real error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify({ detail: "Book not found" }), { status: 404, headers: { "content-type": "application/json" } }));
    await expect(pollBookStatus("u-x-00000000", undefined, 1, 60_000)).rejects.toThrow("Book not found");
  });
});
