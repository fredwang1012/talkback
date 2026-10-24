import { afterEach, describe, expect, it, vi } from "vitest";
import { speak, streamAnswer } from "../src/api";
import { createEmotionMemo } from "../src/talk/answerEmotion";

afterEach(() => { vi.unstubAllGlobals(); });

function ndjson(lines: string) {
  return new Response(new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(new TextEncoder().encode(lines)); c.close(); },
  }));
}

describe("answer emotion (#14)", () => {
  it("streamAnswer hands each sentence's emotion to onSentence, and nothing for neutral or junk", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ndjson(
      '{"type":"sentence","text":"I miss him.","emotion":"sadness"}\n{"type":"sentence","text":"Fine."}\n'
      + '{"type":"sentence","text":"Odd.","emotion":42}\n{"type":"done"}\n')));
    const got: [string, string | undefined][] = [];
    await streamAnswer("b", "q", 1, "harry", (t, e) => got.push([t, e]));
    expect(got).toEqual([["I miss him.", "sadness"], ["Fine.", undefined], ["Odd.", undefined]]);
  });

  it("speak sends the emotion only when there is one", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ voice: "v", audio_url: "/x", timestamps: null, cached: false })));
    vi.stubGlobal("fetch", fetchMock);
    await speak("I miss him.", "marcus", "sadness");
    await speak("Fine.", "marcus");
    const body = (i: number) => JSON.parse(((fetchMock.mock.calls[i] as unknown as [string, RequestInit])[1]).body as string);
    expect(body(0)).toEqual({ text: "I miss him.", voice: "marcus", emotion: "sadness" });
    expect(body(1)).toEqual({ text: "Fine.", voice: "marcus" });
  });

  it("the memo maps a sentence to its emotion until the next answer", () => {
    const m = createEmotionMemo();
    m.note("I miss him.", "sadness");
    m.note("Fine.", undefined);
    expect(m.get("I miss him.")).toBe("sadness");
    expect(m.get("I miss him.")).toBe("sadness"); // a prefetch and the play ask for the same sentence
    expect(m.get("Fine.")).toBeUndefined();
    m.clear();
    expect(m.get("I miss him.")).toBeUndefined();
  });
});
