// @vitest-environment jsdom
// Textbook voice-agent tools: sections at any depth, no spoiler gate, next/previous section, go back, find a topic.
import { beforeEach, describe, expect, it } from "vitest";
import { TOOL_DEFS, parseIntent, resolveJump, runTool, toolDefsFor, type ToolActions, type ToolContext } from "../src/talk/tools";
import { adjacentSection, findSectionByNumber, findSectionByTitle, findTopic, sectionAt, stem } from "../src/talk/sections";

const sections = [
  { id: "pre", title: "Preface", start_para: 0, level: 1 },
  { id: "c1", title: "1 Boolean retrieval", start_para: 2, level: 1 },
  { id: "c1.1", title: "1.1 An example problem", start_para: 3, level: 2 },
  { id: "c1.2", title: "1.2 Building an inverted index", start_para: 6, level: 2 },
  { id: "c1.3", title: "1.3 References and further reading", start_para: 9, level: 2 },
  { id: "c2", title: "2 The term vocabulary", start_para: 10, level: 1 },
  { id: "c2.1", title: "2.1 Tokenization", start_para: 11, level: 2 },
  { id: "c2.1.1", title: "2.1.1 Stop words", start_para: 11, level: 3 },
  { id: "c2.2", title: "2.2 Stemming", start_para: 14, level: 2 },
  { id: "x", title: "Faster postings list intersection via skip pointers", start_para: 16, level: 1 }, // a mis-parsed 2.3
  { id: "c2.4", title: "2.4 Positional indexes", start_para: 18, level: 2 },
  { id: "c2.5", title: "2.5 References and further reading", start_para: 20, level: 2 },
  { id: "c3", title: "3 Dictionaries and tolerant retrieval", start_para: 21, level: 1 },
  { id: "c3.1", title: "3.1 Search structures for dictionaries", start_para: 22, level: 2 },
  { id: "c3.2", title: "3.2 Wildcard queries", start_para: 25, level: 2 },
];
const paragraphs: { idx: number; text: string; kind?: string | null; math?: { latex?: string | null; spoken?: string | null } | null }[] = Array.from({ length: 30 }, (_, i) => ({ idx: i, text: `Some text number ${i}.` }));
paragraphs[7].text = "An inverted index maps each term to a postings list of the documents that contain it.";
paragraphs[12].text = "Tokenization chops a character sequence into pieces called tokens.";
paragraphs[15].text = "Porter's algorithm is the most common stemmer for English text.";
paragraphs[23].text = "A B-tree keeps the dictionary sorted so a term can be found quickly.";
paragraphs[26].text = "A permuterm index handles wildcard queries such as mon*.";
paragraphs[28] = { idx: 28, text: "def frobnicate(): return permuterm", kind: "code" };
paragraphs[24] = { idx: 24, text: "Figure 3.1: A binary tree over the dictionary.", kind: "figure" };
paragraphs[27] = { idx: 27, text: "J ( A , B ) garbled", kind: "math", math: { latex: "J(A,B)=\\frac{|A \\cap B|}{|A \\cup B|}", spoken: "J of A and B equals the size of A intersect B over the size of A union B" } };
// two chunks per paragraph; the code listing is not read aloud, so it has none
const chunks = paragraphs.filter((p) => p.kind !== "code").flatMap((p) => [{ para: p.idx }, { para: p.idx }]);

function ctx(over: Partial<ToolContext> = {}): ToolContext {
  return {
    bookId: "iir", kind: "textbook", sections, paragraphs, chunks, characters: [],
    index: 25, readerPara: 12, spoilers: false, playing: true, speed: 1, speeds: [0.75, 1, 1.25, 1.5], voiceMode: "one", narrator: "male", inSeries: false,
    ...over,
  };
}
function acts() {
  const log: string[] = [];
  const a: ToolActions = {
    seek: (i) => log.push(`seek:${i}`), pause: () => log.push("pause"), setVoiceMode: (m) => log.push(`mode:${m}`), setNarrator: (g) => log.push(`narrator:${g}`),
    setSpeed: (s) => log.push(`speed:${s}`), openRecap: (s) => log.push(`recap:${s}`), showCast: () => log.push("cast"),
  };
  return { a, log };
}
const names = (defs: readonly { name: string }[]) => defs.map((d) => d.name);

beforeEach(() => localStorage.clear());

describe("tools by book kind", () => {
  it("a textbook gets section tools and loses the cast / narrator / two-voice tools", () => {
    expect(names(toolDefsFor("textbook"))).toEqual([
      "jump_to", "read_chapter", "next_section", "previous_section", "go_back", "find_topic", "quiz_me", "explain_here", "resume_reading", "pause_reading", "set_speed", "open_recap", "bookmark"]);
    const json = JSON.stringify(toolDefsFor("textbook"));
    expect(json).not.toMatch(/spoiler/i);
    expect(json).toContain("2.3");
    expect(json).not.toContain("web_search");
    expect(toolDefsFor("textbook").every((t) => t.type === "function")).toBe(true);
  });
  it("a novel keeps its tools (plus go_back) and the spoiler wording", () => {
    expect(toolDefsFor("novel")).toBe(TOOL_DEFS);
    expect(names(toolDefsFor("novel"))).toEqual([
      "jump_to", "read_chapter", "resume_reading", "pause_reading", "set_voice_mode", "set_narrator", "set_speed", "open_recap", "bookmark", "show_cast", "go_back"]);
    expect(JSON.stringify(toolDefsFor("novel"))).toMatch(/Spoilers off/);
  });
});

describe("section resolution", () => {
  it("stems plurals so 'indexes' finds 'index'", () => {
    expect(stem("indexes")).toBe("index");
    expect(stem("queries")).toBe("query");
    expect(stem("dictionaries")).toBe("dictionary");
    expect(stem("words")).toBe("word");
    expect(stem("class")).toBe("class");
  });
  it("finds sections by number at any depth, and the unnumbered section in a numbering gap", () => {
    expect(findSectionByNumber(sections, "2.2")?.id).toBe("c2.2");
    expect(findSectionByNumber(sections, "2.1.1")?.id).toBe("c2.1.1");
    expect(findSectionByNumber(sections, "3")?.id).toBe("c3");
    expect(findSectionByNumber(sections, "2.3")?.id).toBe("x");
    expect(findSectionByNumber(sections, "9.9")).toBeNull();
    expect(findSectionByNumber(sections, "1.5")).toBeNull();
  });
  it("the gap rule only fills the one missing number between siblings, never with back matter", () => {
    const s = (id: string, title: string, start_para: number, level = 2) => ({ id, title, start_para, level });
    const book = [
      s("c1", "1 Intro", 0, 1), s("c1.1", "1.1 A", 1), s("c1.2", "1.2 B", 2), s("c1.3", "1.3 C", 3), s("c1.4", "1.4 D", 4),
      s("ex", "Exercises", 5), s("c2", "2 Next", 6, 1), s("c2.1", "2.1 E", 7), s("c2.2", "2.2 F", 8), s("ref", "References", 9),
      s("c2.4", "2.4 G", 10), s("c2.5", "2.5 H", 11), s("lost", "Lost heading", 12), s("c2.8", "2.8 I", 13),
    ];
    expect(findSectionByNumber(book, "1.5")).toBeNull(); // the chapter's exercises are not section 1.5
    expect(findSectionByNumber(book, "2.3")).toBeNull(); // a back-matter heading never stands in for a number
    expect(findSectionByNumber(book, "2.6")).toBeNull(); // two numbers missing: which one the heading lost is unknown
    expect(findSectionByNumber(book, "2.7")).toBeNull();
  });
  it("finds sections by title words; ties go to the next one from the reader, else the last before", () => {
    expect(findSectionByTitle(sections, "inverted indexes", 0)?.section.id).toBe("c1.2");
    expect(findSectionByTitle(sections, "wildcard queries", 0)?.section.id).toBe("c3.2");
    expect(findSectionByTitle(sections, "references and further reading", 3)?.section.id).toBe("c1.3");
    expect(findSectionByTitle(sections, "references and further reading", 12)?.section.id).toBe("c2.5");
    expect(findSectionByTitle(sections, "references and further reading", 27)?.section.id).toBe("c2.5");
    expect(findSectionByTitle(sections, "quantum chromodynamics", 0)).toBeNull();
  });
  it("sectionAt gives the deepest section containing a paragraph", () => {
    expect(sectionAt(sections, 12)?.id).toBe("c2.1.1");
    expect(sectionAt(sections, 26)?.id).toBe("c3.2");
  });
  it("jump_to / read_chapter take numbers, spoken numbers, chapters and title fragments", () => {
    expect(resolveJump(ctx(), { chapter: "2.2" })).toMatchObject({ ok: true, para: 14, index: 28 });
    expect(resolveJump(ctx(), { chapter: "section 3.2" })).toMatchObject({ ok: true, para: 25 });
    expect(resolveJump(ctx(), { section: "two point one" })).toMatchObject({ ok: true, para: 11 });
    expect(resolveJump(ctx(), { chapter: "chapter 3" })).toMatchObject({ ok: true, para: 21 });
    expect(resolveJump(ctx(), { chapter: "the section on inverted indexes" })).toMatchObject({ ok: true, para: 6 });
    expect(resolveJump(ctx(), { chapter: "2.3" })).toMatchObject({ ok: true, para: 16 });
    expect(resolveJump(ctx(), { chapter: "7.4" })).toMatchObject({ ok: false });
  });
});

describe("no spoiler gate in textbooks; novels keep it", () => {
  it("a textbook reader can jump or read ahead", () => {
    const { a, log } = acts();
    const r = runTool("read_chapter", { section: "3.2" }, ctx({ readerPara: 3, index: 6 }), a);
    expect(r).toMatchObject({ ok: true, moveTo: 50, play: true });
    expect(log).toEqual(["seek:50"]);
    expect(runTool("jump_to", { chapter: "3" }, ctx({ readerPara: 3 }), a)).toMatchObject({ ok: true, moveTo: 42 });
  });
  it("the same jump in a novel is refused with spoilers off", () => {
    const { a, log } = acts();
    const novel = ctx({ kind: "novel", readerPara: 3, index: 6 });
    expect(runTool("read_chapter", { chapter: "3" }, novel, a).ok).toBe(false);
    expect(runTool("next_section", {}, { ...novel, readerPara: 12 }, a).ok).toBe(false); // the next section is past the reader
    expect(runTool("find_topic", { query: "permuterm index" }, novel, a).ok).toBe(false); // only appears later
    expect(log).toEqual([]);
  });
});

describe("next / previous section", () => {
  it("moves through sections at any depth, keeping play as it was", () => {
    expect(adjacentSection(sections, 12, "next")?.id).toBe("c2.2");
    expect(adjacentSection(sections, 12, "previous")?.id).toBe("c2");
    expect(adjacentSection(sections, 12, "next", "chapter")?.id).toBe("x");
    expect(adjacentSection(sections, 12, "previous", "chapter")?.id).toBe("c1");
    const { a, log } = acts();
    const n = runTool("next_section", {}, ctx(), a);
    expect(n).toMatchObject({ ok: true, moveTo: 28, chip: "Next: 2.2 Stemming" });
    expect(n.play).toBeUndefined();
    expect(runTool("previous_section", {}, ctx(), a)).toMatchObject({ ok: true, moveTo: 20 });
    expect(runTool("next_section", { unit: "chapter" }, ctx(), a)).toMatchObject({ ok: true, moveTo: 32 });
    expect(log).toEqual(["seek:28", "seek:20", "seek:32"]);
  });
  it("says so at the first and last section", () => {
    const { a, log } = acts();
    expect(runTool("next_section", {}, ctx({ readerPara: 27, index: 54 }), a)).toMatchObject({ ok: false });
    expect(runTool("previous_section", {}, ctx({ readerPara: 0, index: 0 }), a)).toMatchObject({ ok: false });
    expect(adjacentSection(sections, 27, "next")).toBeNull();
    expect(adjacentSection(sections, 0, "previous")).toBeNull();
    expect(log).toEqual([]);
  });
});

describe("go_back", () => {
  it("repeats the current paragraph by default and always reads on", () => {
    const { a, log } = acts();
    expect(runTool("go_back", {}, ctx(), a)).toMatchObject({ ok: true, moveTo: 24, play: true });
    expect(runTool("go_back", { paragraphs: 1 }, ctx(), a)).toMatchObject({ moveTo: 22 });
    expect(runTool("go_back", { paragraphs: "2" }, ctx(), a)).toMatchObject({ moveTo: 20 });
    expect(runTool("go_back", { sentences: 0 }, ctx(), a)).toMatchObject({ moveTo: 25 });
    expect(runTool("go_back", { sentences: 1 }, ctx(), a)).toMatchObject({ moveTo: 24 });
    expect(runTool("go_back", { sentences: 3 }, ctx(), a)).toMatchObject({ moveTo: 22 });
    expect(log).toEqual(["seek:24", "seek:22", "seek:20", "seek:25", "seek:24", "seek:22"]);
  });
  it("stops at the start of the book and skips paragraphs that are not read aloud", () => {
    const { a } = acts();
    expect(runTool("go_back", { paragraphs: 5 }, ctx({ index: 1, readerPara: 0 }), a)).toMatchObject({ ok: true, moveTo: 0 });
    expect(runTool("go_back", { paragraphs: 1 }, ctx({ index: 57, readerPara: 29 }), a)).toMatchObject({ moveTo: 54 }); // para 28 is code
  });
  it("works in a novel too", () => {
    const { a } = acts();
    expect(runTool("go_back", {}, ctx({ kind: "novel" }), a)).toMatchObject({ ok: true, moveTo: 24, play: true });
  });
});

describe("find_topic", () => {
  it("prefers a section title, then the paragraph that covers the topic", () => {
    expect(findTopic(sections, paragraphs, "wildcard queries")).toMatchObject({ via: "title", para: 25, section: { id: "c3.2" } });
    expect(findTopic(sections, paragraphs, "permuterm index")).toMatchObject({ via: "text", para: 26, section: { id: "c3.2" } });
    expect(findTopic(sections, paragraphs, "the Porter stemmer")).toMatchObject({ via: "text", para: 15, section: { id: "c2.2" } });
    expect(findTopic(sections, paragraphs, "tokens")).toMatchObject({ via: "text", para: 12 }); // not the title "Tokenization": the word itself
    expect(findTopic(sections, paragraphs, "frobnicate")).toBeNull(); // only in a code listing
    expect(findTopic(sections, paragraphs, "zebra crossing")).toBeNull();
  });
  it("jumps there and tells the model the section to name in one sentence", () => {
    const { a, log } = acts();
    const r = runTool("find_topic", { query: "permuterm index" }, ctx({ readerPara: 3, index: 6 }), a);
    expect(r).toMatchObject({ ok: true, moveTo: 52, chip: "Found in 3.2 Wildcard queries" });
    expect(r.output).toContain("section 3.2, Wildcard queries");
    expect(log).toEqual(["seek:52"]);
    expect(runTool("find_topic", { query: "zebra crossing" }, ctx(), a)).toMatchObject({ ok: false });
  });
});

describe("tool results stay short", () => {
  it("every textbook tool answers in under 220 characters", () => {
    const { a } = acts();
    const calls: [string, Record<string, unknown>][] = [
      ["jump_to", { section: "2.2" }], ["read_chapter", { section: "the section on wildcard queries" }], ["next_section", {}], ["previous_section", {}],
      ["go_back", {}], ["go_back", { paragraphs: 2 }], ["find_topic", { query: "permuterm index" }], ["resume_reading", {}], ["pause_reading", {}], ["bookmark", {}],
      ["jump_to", { section: "8.8" }], ["find_topic", { query: "zebra" }],
    ];
    for (const [n, args] of calls) expect(runTool(n, args, ctx(), a).output.length, n).toBeLessThan(220);
  });
});

describe("parseIntent: textbook phrasings", () => {
  it("sections by number and by title", () => {
    expect(parseIntent("read section 2.3")).toEqual({ name: "read_chapter", args: { chapter: "2.3" } });
    expect(parseIntent("Read section 2.3.")).toEqual({ name: "read_chapter", args: { chapter: "2.3" } });
    expect(parseIntent("read section 2 point 3")).toEqual({ name: "read_chapter", args: { chapter: "2 point 3" } });
    expect(parseIntent("go to 4.1")).toEqual({ name: "jump_to", args: { chapter: "4.1" } });
    expect(parseIntent("Jump to section 4.1, please")).toEqual({ name: "jump_to", args: { chapter: "4.1" } });
    expect(parseIntent("read the section on inverted indexes")).toEqual({ name: "read_chapter", args: { chapter: "inverted indexes" } });
    expect(parseIntent("go to the section about stemming")).toEqual({ name: "jump_to", args: { chapter: "stemming" } });
  });
  it("next / previous", () => {
    for (const s of ["next section", "Go to the next section.", "skip to the next section", "skip this section"]) expect(parseIntent(s), s).toEqual({ name: "next_section", args: {} });
    expect(parseIntent("next chapter")).toEqual({ name: "next_section", args: { unit: "chapter" } });
    for (const s of ["previous section", "go back a section", "back one section"]) expect(parseIntent(s), s).toEqual({ name: "previous_section", args: {} });
    expect(parseIntent("go back a chapter")).toEqual({ name: "previous_section", args: { unit: "chapter" } });
  });
  it("repeat / go back", () => {
    for (const s of ["say that again", "Repeat that.", "Could you repeat the last paragraph?", "read that again"]) expect(parseIntent(s), s).toEqual({ name: "go_back", args: {} });
    for (const s of ["go back a bit", "rewind a bit", "go back a paragraph"]) expect(parseIntent(s), s).toEqual({ name: "go_back", args: { paragraphs: 1 } });
    expect(parseIntent("go back two paragraphs")).toEqual({ name: "go_back", args: { paragraphs: 2 } });
    expect(parseIntent("repeat the last sentence")).toEqual({ name: "go_back", args: { sentences: 0 } });
    expect(parseIntent("go back 3 sentences")).toEqual({ name: "go_back", args: { sentences: 3 } });
  });
  it("find a topic", () => {
    expect(parseIntent("Where is tokenization covered?")).toEqual({ name: "find_topic", args: { query: "tokenization" } });
    expect(parseIntent("where does the book talk about stemming")).toEqual({ name: "find_topic", args: { query: "stemming" } });
    expect(parseIntent("which section covers wildcard queries")).toEqual({ name: "find_topic", args: { query: "wildcard queries" } });
    expect(parseIntent("find the section on edit distance")).toEqual({ name: "find_topic", args: { query: "edit distance" } });
  });
  it("still leaves questions alone, and old phrasings parse as before", () => {
    for (const q of ["What is an inverted index?", "Where is Alice?", "What does section 2.3 say about stemming?", "Why do we need section 2.3?", "Say something about stemming"]) expect(parseIntent(q), q).toBeNull();
    expect(parseIntent("go back to the part where Alice falls down the hole")).toEqual({ name: "jump_to", args: { passage: "alice falls down the hole" } });
    expect(parseIntent("Read chapter 3")).toEqual({ name: "read_chapter", args: { chapter: "3" } });
  });
});

describe("session.update by book kind", () => {
  it("a textbook session declares the textbook tools", async () => {
    const { sessionConfig } = await import("../src/talk/TalkClient");
    const tools = (sessionConfig("v", toolDefsFor("textbook")) as { session: { tools: { name: string }[] } }).session.tools;
    expect(names(tools)).toContain("find_topic");
    expect(names(tools)).not.toContain("show_cast");
    const novel = (sessionConfig("v") as { session: { tools: { name: string }[] } }).session.tools;
    expect(names(novel)).toContain("show_cast");
  });
});

describe("quiz_me", () => {
  it("hands the model the text of the reader's section with the quiz rules, and keeps reading paused", () => {
    const { a, log } = acts();
    const r = runTool("quiz_me", {}, ctx(), a);
    expect(r).toMatchObject({ ok: true, pause: true, chip: "Quiz: 2.1.1 Stop words" });
    expect(r.output).toContain("Tokenization chops a character sequence");
    expect(r.output).toMatch(/3 short questions/);
    expect(r.output).toMatch(/one at a time/);
    expect(r.output).toMatch(/score/);
    expect(r.output).not.toContain("Porter"); // the next section
    expect(log).toEqual(["pause"]);
    expect(r.ask && r.ask.length).toBeLessThanOrEqual(500); // the Gemini path's question
  });
  it("any section or chapter, ahead of the reader too; code is left out, equations are spoken", () => {
    const { a } = acts();
    const r = runTool("quiz_me", { section: "chapter 3" }, ctx({ readerPara: 3, index: 6 }), a);
    expect(r).toMatchObject({ ok: true, chip: "Quiz: 3 Dictionaries and tolerant retrieval" });
    expect(r.output).toContain("permuterm index handles wildcard");
    expect(r.output).toContain("A intersect B");
    expect(r.output).not.toContain("frobnicate");
    expect(runTool("quiz_me", { section: "this chapter" }, ctx(), a).chip).toBe("Quiz: 2 The term vocabulary");
    expect(runTool("quiz_me", { section: "9.9" }, ctx(), a).ok).toBe(false);
  });
  it("caps the text near 3000 characters", () => {
    const { a } = acts();
    const long = paragraphs.map((p) => (p.idx === 12 ? { ...p, text: "word ".repeat(1500) } : p));
    const r = runTool("quiz_me", {}, ctx({ paragraphs: long }), a);
    expect(r.ok).toBe(true);
    expect(r.output.length).toBeLessThan(3500);
  });
  it("a novel cannot be quizzed past the reader", () => {
    const { a } = acts();
    expect(runTool("quiz_me", { section: "3" }, ctx({ kind: "novel", readerPara: 3 }), a).ok).toBe(false);
  });
});

describe("explain_here", () => {
  it("explains the current paragraph with the one before as context, reading paused", () => {
    const { a, log } = acts();
    const r = runTool("explain_here", {}, ctx(), a);
    expect(r).toMatchObject({ ok: true, pause: true, chip: "Explaining this paragraph" });
    expect(r.output).toContain("Tokenization chops");
    expect(r.output).toContain("Some text number 11");
    expect(r.output).toMatch(/2-4 sentences/);
    expect(r.output).toMatch(/offer to continue/i);
    expect(log).toEqual(["pause"]);
    expect(r.ask && r.ask.length).toBeLessThanOrEqual(500);
  });
  it("finds the nearest equation or figure just read, with its LaTeX and spoken form or its caption", () => {
    const { a } = acts();
    const eq = runTool("explain_here", { what: "equation" }, ctx({ readerPara: 29, index: 57 }), a);
    expect(eq.output).toContain("\\frac");
    expect(eq.output).toContain("A union B");
    expect(eq.chip).toBe("Explaining the equation");
    const fig = runTool("explain_here", { what: "figure" }, ctx({ readerPara: 26, index: 52 }), a);
    expect(fig.output).toContain("Figure 3.1: A binary tree");
    expect(runTool("explain_here", {}, ctx({ readerPara: 27, index: 54 }), a).output).toContain("A union B"); // on the equation itself
    expect(runTool("explain_here", { what: "equation" }, ctx(), a).ok).toBe(false); // none nearby
  });
  it("a novel with spoilers off never explains an equation or figure past the reader; a textbook may look just ahead", () => {
    const { a } = acts();
    // the figure is at 24, the equation at 27: from 25 only the figure is behind; the equation is one paragraph ahead
    expect(runTool("explain_here", { what: "equation" }, ctx({ readerPara: 25, index: 50 }), a).output).toContain("A union B");
    expect(runTool("explain_here", { what: "equation" }, ctx({ kind: "novel", readerPara: 25, index: 50 }), a).ok).toBe(false);
    expect(runTool("explain_here", { what: "figure" }, ctx({ kind: "novel", readerPara: 23, index: 46 }), a).ok).toBe(false);
    expect(runTool("explain_here", { what: "equation" }, ctx({ kind: "novel", spoilers: true, readerPara: 25, index: 50 }), a).ok).toBe(true);
  });
});

describe("parseIntent: quiz and explain", () => {
  it("quiz phrasings", () => {
    expect(parseIntent("quiz me")).toEqual({ name: "quiz_me", args: {} });
    expect(parseIntent("Quiz me on this section.")).toEqual({ name: "quiz_me", args: {} });
    expect(parseIntent("can you quiz me on chapter 3")).toEqual({ name: "quiz_me", args: { section: "chapter 3" } });
    expect(parseIntent("test me on this chapter")).toEqual({ name: "quiz_me", args: { section: "this chapter" } });
    expect(parseIntent("give me a quiz on wildcard queries")).toEqual({ name: "quiz_me", args: { section: "wildcard queries" } });
  });
  it("explain phrasings", () => {
    expect(parseIntent("What does this equation mean?")).toEqual({ name: "explain_here", args: { what: "equation" } });
    expect(parseIntent("explain this figure")).toEqual({ name: "explain_here", args: { what: "figure" } });
    expect(parseIntent("Explain that.")).toEqual({ name: "explain_here", args: {} });
    expect(parseIntent("what does that mean")).toEqual({ name: "explain_here", args: {} });
    expect(parseIntent("what does the diagram show")).toEqual({ name: "explain_here", args: { what: "figure" } });
    for (const q of ["Explain why the index is sorted", "What does this mean for big collections?", "Is this quiz hard?"]) expect(parseIntent(q), q).toBeNull();
  });
});
