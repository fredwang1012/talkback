// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanSources, cleanSuggestions, MAX_SUGGESTIONS, streamAnswer } from "../src/api";
import { addQuestion, appendSentence, finishAnswer, loadChat, priorTurns, resetChatMemory, saveChat, setSources } from "../src/talk/chatLog";

afterEach(() => { vi.unstubAllGlobals(); });
beforeEach(() => { resetChatMemory(); sessionStorage.clear(); });

// shaped like Google's search_entry_point.rendered_content (a style block and a chip carousel; no script)
const SUGGESTIONS = '<style>.chip { color: #333; } @media (prefers-color-scheme: dark) { .chip { color: #fff; } }</style>'
  + '<div class="container"><div class="carousel"><a class="chip" href="https://www.google.com/search?q=nitrogen&amp;client=app-vertex-grounding">nitrogen</a></div></div>';

function ndjsonResponse(lines: unknown[]) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) { c.enqueue(enc.encode(lines.map((l) => JSON.stringify(l)).join("\n") + "\n")); c.close(); },
  });
  return new Response(body, { status: 200 });
}

describe("web sources of a grounded answer", () => {
  it("streamAnswer reports the sources line (http links only, Google's suggestions as given) and never as a sentence", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ndjsonResponse([
      { type: "sentence", text: "A gas." },
      { type: "sources", sources: [{ uri: "https://e.org/a", title: "e.org" }, { uri: "javascript:alert(1)", title: "x" }, { uri: 3 }],
        search_suggestions: SUGGESTIONS },
      { type: "done" },
    ])));
    const said: string[] = [];
    const got: unknown[] = [];
    await streamAnswer("b", "q", 1, "narrator", (t) => said.push(t), undefined, false, [], (s) => got.push(s));
    expect(said).toEqual(["A gas."]);
    expect(got).toEqual([{ sources: [{ uri: "https://e.org/a", title: "e.org" }], suggestions: SUGGESTIONS }]);
  });

  it("suggestions alone still call back; an answer without a sources line never does", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ndjsonResponse([{ type: "sources", sources: [], search_suggestions: SUGGESTIONS }])));
    const got: unknown[] = [];
    await streamAnswer("b", "q", 1, "narrator", () => {}, undefined, false, [], (s) => got.push(s));
    expect(got).toEqual([{ sources: [], suggestions: SUGGESTIONS }]);
    vi.stubGlobal("fetch", vi.fn(async () => ndjsonResponse([{ type: "sentence", text: "A." }, { type: "done" }])));
    const onSources = vi.fn();
    await streamAnswer("b", "q", 1, "narrator", () => {}, undefined, false, [], onSources);
    expect(onSources).not.toHaveBeenCalled();
  });

  it("cleanSources drops non-http links, duplicates and odd shapes, and caps the count", () => {
    expect(cleanSources("x")).toEqual([]);
    expect(cleanSources([{ uri: "HTTPS://a.example/x" }, { uri: "data:text/html,hi", title: "d" }, { uri: "HTTPS://a.example/x", title: "dup" }]))
      .toEqual([{ uri: "HTTPS://a.example/x", title: "a.example" }]);
    expect(cleanSources(Array.from({ length: 20 }, (_, i) => ({ uri: `https://x${i}.example/`, title: "t" })))).toHaveLength(5);
  });

  it("cleanSuggestions keeps the markup unmodified and drops odd or oversized values", () => {
    expect(cleanSuggestions(SUGGESTIONS)).toBe(SUGGESTIONS);
    expect(cleanSuggestions(5)).toBeUndefined();
    expect(cleanSuggestions("  ")).toBeUndefined();
    expect(cleanSuggestions("x".repeat(MAX_SUGGESTIONS + 1))).toBeUndefined();
  });

  it("sources attach to the pending answer, stay out of the history, and survive a reload cleaned", () => {
    let m = addQuestion([], "What is it?", "narrator", "typed");
    m = appendSentence(m, "A gas.");
    m = setSources(m, { sources: [{ uri: "https://e.org/a", title: "e.org" }], suggestions: SUGGESTIONS });
    m = finishAnswer(m);
    expect(m[1].sources).toEqual([{ uri: "https://e.org/a", title: "e.org" }]);
    expect(m[1].searchSuggestions).toBe(SUGGESTIONS);
    expect(priorTurns(m, "narrator")).toEqual([{ role: "user", text: "What is it?" }, { role: "character", text: "A gas." }]);
    expect(setSources([], { sources: [{ uri: "https://e.org/a", title: "e" }] })).toEqual([]);   // nothing pending: ignored

    sessionStorage.setItem("talkback:chat:b", JSON.stringify([
      { id: 1, role: "character", speakerId: "narrator", text: "A.", searchSuggestions: SUGGESTIONS,
        sources: [{ uri: "javascript:alert(1)", title: "x" }, { uri: "https://ok.example/", title: "ok" }] },
      { id: 2, role: "character", speakerId: "narrator", text: "B.", searchSuggestions: 42 },
    ]));
    const loaded = loadChat("b");
    expect(loaded[0].sources).toEqual([{ uri: "https://ok.example/", title: "ok" }]);
    expect(loaded[0].searchSuggestions).toBe(SUGGESTIONS);
    expect("searchSuggestions" in loaded[1]).toBe(false);
    saveChat("b", []);
  });
});
