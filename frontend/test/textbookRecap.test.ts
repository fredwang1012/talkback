// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { enrichDoneNote, fetchScopedRecap } from "../src/api";
import { recapChoices } from "../src/reader/RecapMenu";
import { recapHeading } from "../src/reader/RecapPanel";
import { parseIntent, runTool, TEXTBOOK_TOOL_DEFS, type ToolActions, type ToolContext } from "../src/talk/tools";
import { jobLine } from "../src/uploads";

afterEach(() => vi.unstubAllGlobals());

const sections = [
  { id: "c1", title: "1 Boolean retrieval", start_para: 0, level: 1 },
  { id: "c1-1", title: "1.1 An example", start_para: 3, level: 2 },
  { id: "c4", title: "4 Index construction", start_para: 10, level: 1 },
  { id: "c4-2", title: "4.2 Blocked sort-based indexing", start_para: 14, level: 2 },
];
const paragraphs = Array.from({ length: 20 }, (_, i) => ({ idx: i, text: `Paragraph ${i}.` }));

function ctx(over: Partial<ToolContext> = {}): ToolContext {
  return {
    bookId: "ir", sections, paragraphs, chunks: paragraphs.map((p) => ({ para: p.idx })), characters: [],
    index: 0, readerPara: 0, spoilers: false, playing: false, speed: 1, speeds: [1], voiceMode: "one", narrator: "male",
    inSeries: false, textbook: true, ...over,
  };
}
function acts() {
  const log: string[] = [];
  const a: ToolActions = {
    seek: () => {}, pause: () => {}, setVoiceMode: () => {}, setNarrator: () => {}, setSpeed: () => {}, showCast: () => {},
    openRecap: (s, section) => log.push(section ? `recap:${s}:${section.id}` : `recap:${s}`),
  };
  return { a, log };
}

describe("textbook recap", () => {
  it("names the chapter option 'This chapter' for a textbook (novels unchanged)", () => {
    expect(recapChoices(false, true).map((c) => c.label)).toEqual(["This chapter", "This book so far"]);
    expect(recapChoices(true).map((c) => c.label)).toEqual(["Last chapter", "This book so far", "The series so far"]);
  });

  it("parses 'summarize chapter 4' and friends into a recap of that chapter", () => {
    expect(parseIntent("Summarize chapter 4")).toEqual({ name: "open_recap", args: { scope: "chapter", chapter: "4" } });
    expect(parseIntent("can you summarise section 4.2, please?")).toEqual({ name: "open_recap", args: { scope: "chapter", chapter: "4.2" } });
    expect(parseIntent("recap chapter two")).toEqual({ name: "open_recap", args: { scope: "chapter", chapter: "two" } });
    expect(parseIntent("recap the last chapter")).toEqual({ name: "open_recap", args: { scope: "chapter" } });
    expect(parseIntent("summarize the book")).toEqual({ name: "open_recap", args: { scope: "book" } });
  });

  it("open_recap with a chapter recaps that section, ahead of the reader in a textbook", () => {
    const { a, log } = acts();
    const r = runTool("open_recap", { scope: "chapter", chapter: "4" }, ctx(), a);
    expect(r.ok).toBe(true);
    expect(r.chip).toBe("Recap: 4 Index construction");
    expect(runTool("open_recap", { chapter: "4.2" }, ctx(), a).chip).toBe("Recap: 4.2 Blocked sort-based indexing");
    expect(log).toEqual(["recap:chapter:c4", "recap:chapter:c4-2"]);
    expect(runTool("open_recap", { chapter: "99" }, ctx(), a).ok).toBe(false);
    expect(runTool("open_recap", { scope: "chapter" }, ctx(), a).chip).toBe("Recap: this chapter");
  });

  it("textbook open_recap resolves sections like jump_to (spoken numbers, any depth) and the Realtime def takes a chapter", () => {
    const def = TEXTBOOK_TOOL_DEFS.find((t) => t.name === "open_recap")!;
    expect(Object.keys(def.parameters.properties)).toContain("chapter");
    const { a, log } = acts();
    expect(runTool("open_recap", { chapter: "four point two" }, ctx({ kind: "textbook" }), a).chip).toBe("Recap: 4.2 Blocked sort-based indexing");
    expect(runTool("open_recap", { chapter: "section 4.2" }, ctx({ kind: "textbook" }), a).chip).toBe("Recap: 4.2 Blocked sort-based indexing");
    expect(log).toEqual(["recap:chapter:c4-2", "recap:chapter:c4-2"]);
    expect(runTool("open_recap", { chapter: "4.7" }, ctx({ kind: "textbook" }), a).ok).toBe(false);
  });

  it("a novel's chapter recap past the reader is refused while spoilers are off", () => {
    const { a, log } = acts();
    expect(runTool("open_recap", { chapter: "4" }, ctx({ textbook: false }), a).ok).toBe(false);
    expect(runTool("open_recap", { chapter: "1" }, ctx({ textbook: false }), a).ok).toBe(true);
    expect(log).toEqual(["recap:chapter:c1"]);
  });

  it("sends the section to the recap endpoint", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ scope: "chapter", upto_para: 3, chapter: "x", text: "t" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchScopedRecap("ir", 2, "chapter", "c4-2");
    await fetchScopedRecap("ir", 2, "book");
    const urls = fetchMock.mock.calls.map((c) => String((c as unknown[])[0]));
    expect(urls[0]).toContain("scope=chapter&section=c4-2");
    expect(urls[1]).not.toContain("section=");
  });

  it("headings and status lines use the textbook wording", () => {
    expect(recapHeading({ scope: "chapter", label: "Chapter recap", section: "c4" }, { chapter: "4 Index construction" })).toBe("Chapter recap: 4 Index construction");
    expect(enrichDoneNote({ status: "done", summaries_only: true, message: "Study notes for the recap are ready." })).toBe("study notes for the recap are ready");
    expect(jobLine({ key: "k", title: "T", phase: "confirm", status: { status: "ready", enrich: { status: "awaiting_confirmation", summaries_only: true } } }))
      .toBe("Readable now. Study notes for the recap need processing.");
    expect(jobLine({ key: "k", title: "T", phase: "confirm" })).toBe("Readable now. Characters, voices and recap need processing.");
  });
});
