import { afterEach, describe, expect, it, vi } from "vitest";
import { speak, streamAnswer } from "../src/api";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

function hangingFetch() {
  return vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_res, rej) => {
    init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
  }));
}

function ndjsonResponse(chunks: string[], status = 200) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) { chunks.forEach((s) => c.enqueue(enc.encode(s))); c.close(); },
  });
  return new Response(body, { status });
}

describe("streamAnswer", () => {
  it("delivers sentences split across chunks and a trailing line without newline", async () => {
    const fetchMock = vi.fn(async () => ndjsonResponse([
      '{"type":"sentence","text":"Hel', 'lo."}\n{"type":"sentence","text":"Two."}\n{"type":"do', 'ne"}\n{"type":"sentence","text":"Last."}',
    ]));
    vi.stubGlobal("fetch", fetchMock);
    const got: string[] = [];
    await streamAnswer("b", "q", 3, "narrator", (t) => got.push(t));
    expect(got).toEqual(["Hello.", "Two.", "Last."]);
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(init.body as string)).toEqual({ book_id: "b", query: "q", reader_para: 3, speaker_id: "narrator" });
  });

  it("delivers earlier sentences, then throws on an error line", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ndjsonResponse(['{"type":"sentence","text":"A."}\n{"type":"error","message":"boom"}\n'])));
    const got: string[] = [];
    await expect(streamAnswer("b", "q", 1, "s", (t) => got.push(t))).rejects.toThrow("boom");
    expect(got).toEqual(["A."]);
  });

  it("reads the detail of a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Access code required" }), { status: 401 })));
    await expect(streamAnswer("b", "q", 1, "s", () => {})).rejects.toThrow("Access code required");
  });

  it("aborts through the signal without calling it a timeout", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const ctl = new AbortController();
    const p = streamAnswer("b", "q", 1, "s", () => {}, ctl.signal).catch((e: Error) => e);
    ctl.abort();
    expect(((await p) as Error).name).toBe("AbortError");
  });

  it("times out waiting for headers", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hangingFetch());
    const p = streamAnswer("b", "q", 1, "s", () => {}).catch((e: Error) => e.message);
    await vi.advanceTimersByTimeAsync(21000);
    expect(await p).toContain("took too long");
  });
});

describe("speak", () => {
  it("posts text and voice", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ voice: "v", audio_url: "/media/x.wav", timestamps: null, cached: false })));
    vi.stubGlobal("fetch", fetchMock);
    const res = await speak("Hi.", "marcus");
    expect(res.audio_url).toBe("/media/x.wav");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/speak");
    expect(JSON.parse(init.body as string)).toEqual({ text: "Hi.", voice: "marcus" });
  });
});
