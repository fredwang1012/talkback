// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  TOOL_DEFS, addBookmark, bookmarks, chapterNumber, findChapter, parseIntent, pickSpeed, resolveJump, runTool,
  type ToolActions, type ToolContext,
} from "../src/talk/tools";

const sections = [
  { id: "s1", title: "Chapter I. Down the Rabbit-Hole", start_para: 0 },
  { id: "s2", title: "Chapter II. The Pool of Tears", start_para: 10 },
  { id: "s3", title: "Chapter III. A Caucus-Race", start_para: 20 },
];
const paragraphs = Array.from({ length: 30 }, (_, i) => ({ idx: i, text: `Paragraph ${i}.` }));
paragraphs[4].text = "Alice was beginning to get very tired of sitting by her sister on the bank.";
paragraphs[25].text = "“Curiouser and curiouser!” cried Alice.";
paragraphs[7].text = "Alice said “curiouser and curiouser” again, earlier."; // the same words, before the reader
const chunks = paragraphs.flatMap((p) => [{ para: p.idx }, { para: p.idx }]); // two chunks per paragraph

function ctx(over: Partial<ToolContext> = {}): ToolContext {
  return {
    bookId: "alice", sections, paragraphs, chunks,
    characters: [{ id: "alice", name: "Alice", first_para: 0 }, { id: "hatter", name: "The Hatter", first_para: 22 }, { id: "x", name: "Extra", first_para: 1, main: false }],
    index: 24, readerPara: 12, spoilers: false, playing: false, speed: 1, speeds: [0.75, 1, 1.25, 1.5], voiceMode: "two", narrator: "male", inSeries: false,
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

beforeEach(() => localStorage.clear());

describe("tool declarations", () => {
  it("declares only client function tools: never Boson's server-side web_search (spoilers)", () => {
    expect(TOOL_DEFS.every((t) => t.type === "function")).toBe(true);
    expect(TOOL_DEFS.map((t) => t.name)).toEqual([
      "jump_to", "read_chapter", "resume_reading", "pause_reading", "set_voice_mode", "set_narrator", "set_speed", "open_recap", "bookmark", "show_cast"]);
    expect(JSON.stringify(TOOL_DEFS)).not.toContain("web_search");
  });
});

describe("chapters", () => {
  it("reads numbers, words and Roman numerals", () => {
    expect(chapterNumber("3")).toBe(3);
    expect(chapterNumber("chapter three")).toBe(3);
    expect(chapterNumber("II")).toBe(2);
    expect(chapterNumber("last")).toBe(-1);
    expect(chapterNumber("the pool")).toBeNull();
  });
  it("finds a chapter by number, numeral in the title, words from the title, or 'last'", () => {
    expect(findChapter(sections, "2")?.id).toBe("s2");
    expect(findChapter(sections, "iii")?.id).toBe("s3");
    expect(findChapter(sections, "pool of tears")?.id).toBe("s2");
    expect(findChapter(sections, "last")?.id).toBe("s3");
    expect(findChapter(sections, "9")).toBeNull();
  });
});

describe("spoiler guard (resolveJump)", () => {
  it("jumps to a chapter already reached, landing on its first chunk", () => {
    const j = resolveJump(ctx(), { chapter: "2" });
    expect(j).toMatchObject({ ok: true, index: 20, para: 10 });
  });
  it("refuses a chapter past the reader when spoilers are off, allows it when they are on", () => {
    expect(resolveJump(ctx(), { chapter: "3" })).toMatchObject({ ok: false });
    expect((resolveJump(ctx(), { chapter: "3" }) as { output: string }).output).toMatch(/spoilers are off/);
    expect(resolveJump(ctx({ spoilers: true }), { chapter: "3" })).toMatchObject({ ok: true, index: 40 });
  });
  it("a quoted passage goes to the nearest match at or before the reader, never a later one", () => {
    expect(resolveJump(ctx(), { passage: "Curiouser and curiouser" })).toMatchObject({ ok: true, para: 7 });
    expect(resolveJump(ctx(), { passage: "tired of sitting by her sister" })).toMatchObject({ ok: true, para: 4 });
    expect(resolveJump(ctx({ readerPara: 5 }), { passage: "curiouser and curiouser" })).toMatchObject({ ok: false }); // both matches are later
    expect(resolveJump(ctx({ readerPara: 5, spoilers: true }), { passage: "curiouser and curiouser" })).toMatchObject({ ok: true, para: 7 });
  });
  it("a paragraph without chunks never lets the jump land past the limit", () => {
    const c = ctx({ readerPara: 10, chunks: chunks.filter((k) => k.para !== 10) }); // the chapter's first paragraph has no chunk
    expect(resolveJump(c, { chapter: "2" })).toMatchObject({ ok: false });
  });
  it("explains a missing chapter or passage instead of moving", () => {
    expect(resolveJump(ctx(), { chapter: "42" })).toMatchObject({ ok: false });
    expect(resolveJump(ctx(), { passage: "no such words" })).toMatchObject({ ok: false });
    expect(resolveJump(ctx(), {})).toMatchObject({ ok: false });
  });
});

describe("runTool", () => {
  it("jump_to seeks and leaves play/pause as it was; read_chapter always starts reading", () => {
    const { a, log } = acts();
    const jump = runTool("jump_to", { chapter: "2" }, ctx(), a);
    expect(jump).toMatchObject({ ok: true, moveTo: 20, chip: "Jumped to Chapter II. The Pool of Tears" });
    expect(jump.play).toBeUndefined();
    const read = runTool("read_chapter", { chapter: "two" }, ctx(), a);
    expect(read).toMatchObject({ ok: true, moveTo: 20, play: true });
    expect(log).toEqual(["seek:20", "seek:20"]);
  });
  it("refuses a spoiler jump without touching the reader", () => {
    const { a, log } = acts();
    const r = runTool("read_chapter", { chapter: "3" }, ctx(), a);
    expect(r.ok).toBe(false);
    expect(r.chip).toBeNull();
    expect(r.moveTo).toBeUndefined();
    expect(log).toEqual([]);
  });
  it("resume / pause", () => {
    const { a, log } = acts();
    expect(runTool("resume_reading", {}, ctx(), a)).toMatchObject({ ok: true, play: true, chip: "Reading resumes" });
    expect(runTool("pause_reading", {}, ctx(), a)).toMatchObject({ ok: true, pause: true, chip: "Reading paused" });
    expect(log).toEqual(["pause"]);
  });
  it("voice mode, narrator and speed", () => {
    const { a, log } = acts();
    expect(runTool("set_voice_mode", { mode: "one" }, ctx(), a).chip).toBe("One voice");
    expect(runTool("set_voice_mode", { mode: "three" }, ctx(), a).ok).toBe(false);
    expect(runTool("set_narrator", { gender: "female" }, ctx(), a).chip).toBe("Female narrator");
    expect(runTool("set_speed", { speed: "faster" }, ctx(), a).chip).toBe("Speed 1.25x");
    expect(runTool("set_speed", { speed: "1.4" }, ctx(), a).chip).toBe("Speed 1.5x");
    expect(runTool("set_speed", { speed: "warp" }, ctx(), a).ok).toBe(false);
    expect(log).toEqual(["mode:one", "narrator:female", "speed:1.25", "speed:1.5"]);
    expect(pickSpeed(1.5, [0.75, 1, 1.25, 1.5], "faster")).toBe(1.5);
    expect(pickSpeed(0.75, [0.75, 1, 1.25, 1.5], "slower")).toBe(0.75);
    expect(pickSpeed(1.25, [0.75, 1, 1.25, 1.5], "normal")).toBe(1);
  });
  it("recap scopes (series only in a series), bookmark, cast", () => {
    const { a, log } = acts();
    expect(runTool("open_recap", { scope: "series" }, ctx(), a).chip).toBe("Recap: the last chapter");
    expect(runTool("open_recap", { scope: "series" }, ctx({ inSeries: true }), a).chip).toBe("Recap: the series so far");
    expect(runTool("open_recap", { scope: "book" }, ctx(), a).output).toMatch(/do not recap it yourself/);
    const b = runTool("bookmark", {}, ctx(), a);
    expect(b.chip).toBe("Bookmarked Chapter II. The Pool of Tears, paragraph 13");
    expect(bookmarks("alice")).toMatchObject([{ index: 24, para: 12 }]);
    const cast = runTool("show_cast", {}, ctx(), a);
    expect(cast.output).toContain("Alice");
    expect(cast.output).not.toContain("Hatter"); // not met yet
    expect(cast.output).not.toContain("Extra"); // minor speaker
    expect(runTool("show_cast", {}, ctx({ spoilers: true }), a).output).toContain("Hatter");
    expect(log).toEqual(["recap:chapter", "recap:series", "recap:book", "cast", "cast"]);
  });
  it("jumps back to the bookmark, which is spoiler-safe too", () => {
    const { a } = acts();
    addBookmark("alice", { index: 8, para: 4, label: "p4" });
    expect(runTool("jump_to", { chapter: "bookmark" }, ctx(), a)).toMatchObject({ ok: true, moveTo: 8 });
    addBookmark("alice", { index: 50, para: 25, label: "p25" }); // set with spoilers on, then switched off
    expect(runTool("jump_to", { passage: "my bookmark" }, ctx(), a).ok).toBe(false);
  });
  it("an unknown tool is refused, not thrown", () => {
    const { a } = acts();
    expect(runTool("web_search", { q: "ending" }, ctx(), a)).toMatchObject({ ok: false, chip: null });
  });
});

describe("parseIntent (Gemini path)", () => {
  it("recognises plain commands", () => {
    expect(parseIntent("Read chapter 3")).toEqual({ name: "read_chapter", args: { chapter: "3" } });
    expect(parseIntent("jump to chapter two")).toEqual({ name: "jump_to", args: { chapter: "two" } });
    expect(parseIntent("go back to the part where Alice falls down the hole")).toEqual({ name: "jump_to", args: { passage: "alice falls down the hole" } });
    expect(parseIntent("go to my bookmark")).toEqual({ name: "jump_to", args: { chapter: "bookmark" } });
    expect(parseIntent("keep reading")).toEqual({ name: "resume_reading", args: {} });
    expect(parseIntent("pause")).toEqual({ name: "pause_reading", args: {} });
    expect(parseIntent("read in one voice")).toEqual({ name: "set_voice_mode", args: { mode: "one" } });
    expect(parseIntent("use the female voice")).toEqual({ name: "set_narrator", args: { gender: "female" } });
    expect(parseIntent("a bit faster")).toEqual({ name: "set_speed", args: { speed: "faster" } });
    expect(parseIntent("set the speed to 1.5")).toEqual({ name: "set_speed", args: { speed: "1.5" } });
    expect(parseIntent("recap the last chapter")).toEqual({ name: "open_recap", args: { scope: "chapter" } });
    expect(parseIntent("catch me up on the series")).toEqual({ name: "open_recap", args: { scope: "series" } });
    expect(parseIntent("bookmark this")).toEqual({ name: "bookmark", args: {} });
    expect(parseIntent("show me the cast")).toEqual({ name: "show_cast", args: {} });
  });
  it("tolerates how speech is transcribed: punctuation, commas, 'please', a polite prefix", () => {
    expect(parseIntent("Read chapter 2, please.")).toEqual({ name: "read_chapter", args: { chapter: "2" } });
    expect(parseIntent("Jump to chapter two!")).toEqual({ name: "jump_to", args: { chapter: "two" } });
    expect(parseIntent("Could you read chapter two?")).toEqual({ name: "read_chapter", args: { chapter: "two" } });
    expect(parseIntent("Okay, please pause.")).toEqual({ name: "pause_reading", args: {} });
    expect(parseIntent("Can you go a bit faster, please?")).toEqual({ name: "set_speed", args: { speed: "faster" } });
    expect(parseIntent("Set the speed to 1.5, please")).toEqual({ name: "set_speed", args: { speed: "1.5" } });
  });
  it("leaves real questions alone", () => {
    for (const q of ["Why did Alice follow the rabbit?", "What happens in chapter 3?", "Who is the Hatter?", "Is the story over?", "Can you read faster than a human?"]) {
      expect(parseIntent(q)).toBeNull();
    }
  });
});

describe("tool chips in the chat log", () => {
  it("sit before the answer being spoken, survive a reload, and are never sent back as history", async () => {
    const { addQuestion, addToolChip, finishAnswer, loadChat, priorTurns, resetChatMemory, saveChat } = await import("../src/talk/chatLog");
    resetChatMemory(); sessionStorage.clear();
    let m = addQuestion([], "Read chapter two", "alice", "voice");
    m = addToolChip(m, "Jumped to Chapter II");
    expect(m.map((x) => x.role)).toEqual(["user", "tool", "character"]);
    m = finishAnswer(m); // the Gemini path: no spoken answer, the empty bubble goes, the chip stays
    expect(m.map((x) => x.role)).toEqual(["user", "tool"]);
    saveChat("b", m); resetChatMemory();
    expect(loadChat("b").map((x) => x.role)).toEqual(["user", "tool"]);
    expect(priorTurns(m, "alice")).toEqual([{ role: "user", text: "Read chapter two" }]);
  });
});
