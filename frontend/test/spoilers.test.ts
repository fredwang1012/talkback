import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchRetrieve, streamAnswer } from "../src/api";
import { loadSpoilers, saveSpoilers } from "../src/reader/spoilers";

afterEach(() => { vi.unstubAllGlobals(); });

describe("spoiler toggle storage", () => {
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
  });

  it("defaults to off and persists per book", () => {
    expect(loadSpoilers("a")).toBe(false);
    saveSpoilers("a", true);
    expect(loadSpoilers("a")).toBe(true);
    expect(loadSpoilers("b")).toBe(false);
    saveSpoilers("a", false);
    expect(loadSpoilers("a")).toBe(false);
  });

  it("survives blocked storage", () => {
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); } });
    expect(loadSpoilers("a")).toBe(false);
    expect(() => saveSpoilers("a", true)).not.toThrow();
  });
});

const body = (fetchMock: ReturnType<typeof vi.fn>) =>
  JSON.parse((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);

describe("spoilers in request payloads", () => {
  it("fetchRetrieve sends spoilers:true only when on", async () => {
    const ok = () => new Response(JSON.stringify({ passages: [], story_so_far: "", instructions: "", persona: null, reader_para: 1 }));
    const off = vi.fn(async () => ok());
    vi.stubGlobal("fetch", off);
    await fetchRetrieve("b", "q", 1, "narrator");
    expect(body(off)).not.toHaveProperty("spoilers");
    const on = vi.fn(async () => ok());
    vi.stubGlobal("fetch", on);
    await fetchRetrieve("b", "q", 1, "narrator", true);
    expect(body(on).spoilers).toBe(true);
  });

  it("streamAnswer sends spoilers:true only when on", async () => {
    const mk = () => vi.fn(async () => new Response('{"type":"done"}\n'));
    const off = mk();
    vi.stubGlobal("fetch", off);
    await streamAnswer("b", "q", 1, "narrator", () => {});
    expect(body(off)).not.toHaveProperty("spoilers");
    const on = mk();
    vi.stubGlobal("fetch", on);
    await streamAnswer("b", "q", 1, "narrator", () => {}, undefined, true);
    expect(body(on).spoilers).toBe(true);
  });
});
