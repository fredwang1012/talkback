/**
 * Reader tools the character can call (#72, "Agents That Act"): declared to Higgs Realtime as function tools, run here
 * in the browser against the reader, and answered with a short result the model then speaks. Pure logic: the reader is
 * reached through `ToolActions`, the state through a `ToolContext` snapshot, so every tool can be unit-tested.
 *
 * Spoiler invariant: with spoilers off no tool may move the reader past the paragraph they have reached
 * (`resolveJump` refuses, and the chunk it picks is re-checked), whatever the model asked for.
 *
 * Boson's server-side `{"type": "web_search"}` tool is never declared: a web search about a novel is a spoiler machine.
 */

import type { RecapScope } from "../api";
import type { FlatChunk } from "../reader/position";
import type { VoiceMode } from "../reader/castLogic";
import { adjacentSection, findSectionByNumber, findSectionByTitle, findTopic, sectionAt, sectionEnd, titleNumber, titleText } from "./sections";

export type Gender = "male" | "female";

/** "Say that again" / "go back a bit": rewind and read on. Declared for every kind of book. */
const GO_BACK = {
  type: "function", name: "go_back",
  description: "Rewind and read again: 'say that again', 'go back a bit', 'repeat the last paragraph'. No arguments repeats the current paragraph.",
  parameters: {
    type: "object",
    properties: {
      paragraphs: { type: "integer", description: "Paragraphs to go back: 0 repeats the current one, 1 starts at the one before" },
      sentences: { type: "integer", description: "Instead: sentences to go back (0 repeats the current sentence)" },
    },
  },
} as const;

/** The function tools of a novel, in the shape `session.update` takes (`tools[]`, type "function"). */
export const TOOL_DEFS = [
  {
    type: "function", name: "jump_to",
    description: "Move the reader to a chapter (by number or title) or to a quoted passage of the book. Spoilers off: only places the reader has already reached.",
    parameters: {
      type: "object",
      properties: {
        chapter: { type: "string", description: "Chapter number (e.g. '3') or part of its title" },
        passage: { type: "string", description: "Words quoted from the passage to find" },
      },
    },
  },
  {
    type: "function", name: "read_chapter",
    description: "Start reading a chapter aloud from its beginning (by number or title). Spoilers off: only chapters already reached.",
    parameters: { type: "object", properties: { chapter: { type: "string", description: "Chapter number or part of its title" } }, required: ["chapter"] },
  },
  { type: "function", name: "resume_reading", description: "Continue reading the book aloud from where the reader stopped.", parameters: { type: "object", properties: {} } },
  { type: "function", name: "pause_reading", description: "Stop reading the book aloud; keep the place.", parameters: { type: "object", properties: {} } },
  {
    type: "function", name: "set_voice_mode",
    description: "Read in one voice (the narrator reads every line) or two voices (a male and a female voice).",
    parameters: { type: "object", properties: { mode: { type: "string", enum: ["one", "two"] } }, required: ["mode"] },
  },
  {
    type: "function", name: "set_narrator",
    description: "Choose whether the male or the female voice narrates.",
    parameters: { type: "object", properties: { gender: { type: "string", enum: ["male", "female"] } }, required: ["gender"] },
  },
  {
    type: "function", name: "set_speed",
    description: "Change the reading speed: a multiplier such as 1.25, or 'faster' / 'slower' / 'normal'.",
    parameters: { type: "object", properties: { speed: { type: "string", description: "e.g. '1.5', 'faster', 'slower', 'normal'" } }, required: ["speed"] },
  },
  {
    type: "function", name: "open_recap",
    description: "Have the narrator recap the story so far: the last chapter, this book so far, or the series so far; or summarise one chapter or section the reader names (chapter, e.g. '4' or '4.2').",
    parameters: { type: "object", properties: { scope: { type: "string", enum: ["chapter", "book", "series"] }, chapter: { type: "string", description: "a chapter or section to summarise, e.g. '4', 'four', '4.2' or words from its title" } } },
  },
  { type: "function", name: "bookmark", description: "Bookmark the reader's current place in the book.", parameters: { type: "object", properties: {} } },
  { type: "function", name: "show_cast", description: "Show the cast: the characters the reader has met so far, and open the voices panel.", parameters: { type: "object", properties: {} } },
  GO_BACK,
] as const;

/**
 * The tools a textbook gets: sections at any depth ("2.3"), no spoiler limit (a textbook is read out of order), next /
 * previous section, go back, and find where a topic is covered. No cast, narrator gender or two voices: one tutor reads.
 */
export const TEXTBOOK_TOOL_DEFS = [
  {
    type: "function", name: "jump_to",
    description: "Move the reader to a chapter or section (by number like '3' or '2.3', or words from its title), or to a quoted passage. Any place in the book.",
    parameters: {
      type: "object",
      properties: {
        section: { type: "string", description: "Chapter or section number ('4', '2.3', '2.3.1') or words from its title" },
        passage: { type: "string", description: "Words quoted from the passage to find" },
      },
    },
  },
  {
    type: "function", name: "read_chapter",
    description: "Start reading a chapter or section aloud from its beginning: a number like '3' or '2.3', or words from its title ('the section on inverted indexes').",
    parameters: { type: "object", properties: { section: { type: "string", description: "Chapter or section number, or words from its title" } }, required: ["section"] },
  },
  {
    type: "function", name: "next_section",
    description: "Move to the start of the next section (or the next chapter).",
    parameters: { type: "object", properties: { unit: { type: "string", enum: ["section", "chapter"] } } },
  },
  {
    type: "function", name: "previous_section",
    description: "Move back to the start of the previous section (or the previous chapter).",
    parameters: { type: "object", properties: { unit: { type: "string", enum: ["section", "chapter"] } } },
  },
  GO_BACK,
  {
    type: "function", name: "find_topic",
    description: "Find where the book covers a topic (searches section titles and text) and move there. Use for 'where is X explained?'.",
    parameters: { type: "object", properties: { query: { type: "string", description: "The topic, in a few words" } }, required: ["query"] },
  },
  {
    type: "function", name: "quiz_me",
    description: "Quiz the reader on a section: returns its text and the quiz rules. Default: the section being read. Reading stays paused.",
    parameters: { type: "object", properties: { section: { type: "string", description: "Section or chapter number, title words, or 'this chapter'; omit for the current section" } } },
  },
  {
    type: "function", name: "explain_here",
    description: "Explain what was just read: the current paragraph, or the nearest equation or figure ('what does this equation mean?'). Returns the text to explain.",
    parameters: { type: "object", properties: { what: { type: "string", enum: ["paragraph", "equation", "figure"] } } },
  },
  { type: "function", name: "resume_reading", description: "Continue reading aloud from where the reader stopped.", parameters: { type: "object", properties: {} } },
  { type: "function", name: "pause_reading", description: "Stop reading aloud; keep the place.", parameters: { type: "object", properties: {} } },
  {
    type: "function", name: "set_speed",
    description: "Change the reading speed: a multiplier such as 1.25, or 'faster' / 'slower' / 'normal'.",
    parameters: { type: "object", properties: { speed: { type: "string", description: "e.g. '1.5', 'faster', 'slower', 'normal'" } }, required: ["speed"] },
  },
  {
    type: "function", name: "open_recap",
    description: "Have the tutor summarize this chapter or the book so far, or one chapter or section the reader names (chapter, e.g. '4' or '4.2').",
    parameters: { type: "object", properties: { scope: { type: "string", enum: ["chapter", "book"] }, chapter: { type: "string", description: "a chapter or section to summarize, e.g. '4', 'four', '4.2' or words from its title" } } },
  },
  { type: "function", name: "bookmark", description: "Bookmark the reader's current place.", parameters: { type: "object", properties: {} } },
] as const;

export type BookKind = "novel" | "textbook";

/** The function tools to declare in `session.update` for a book of this kind. */
export function toolDefsFor(kind: BookKind | undefined): typeof TOOL_DEFS | typeof TEXTBOOK_TOOL_DEFS {
  return kind === "textbook" ? TEXTBOOK_TOOL_DEFS : TOOL_DEFS;
}

export type ToolName = (typeof TOOL_DEFS)[number]["name"] | (typeof TEXTBOOK_TOOL_DEFS)[number]["name"];
export const TOOL_NAMES: readonly string[] = [...new Set([...TOOL_DEFS, ...TEXTBOOK_TOOL_DEFS].map((t) => t.name))];

/** What the tools know about the reader right now (a snapshot taken when the tool runs). */
export type ToolContext = {
  bookId: string;
  /** A textbook has no spoiler limit and section tools; unset means a novel. */
  kind?: BookKind;
  sections: { id: string; title: string; start_para: number; level?: number }[];
  paragraphs: { idx: number; text: string; kind?: string | null; math?: { latex?: string | null; spoken?: string | null } | null }[];
  characters: { id: string; name: string; first_para: number; main?: boolean | null }[];
  chunks: Pick<FlatChunk, "para">[];
  index: number; // current chunk
  readerPara: number; // the paragraph reached: the spoiler limit
  spoilers: boolean; // full-book mode: the reader allows spoilers
  playing: boolean;
  speed: number;
  speeds: number[];
  voiceMode: VoiceMode;
  narrator: Gender;
  inSeries: boolean;
  textbook?: boolean; // no spoilers: any chapter can be recapped
};

/** What the tools may do to the reader. */
export type ToolActions = {
  seek: (index: number) => void; // move the position without starting playback
  pause: () => void;
  setVoiceMode: (mode: VoiceMode) => void;
  setNarrator: (g: Gender) => void;
  setSpeed: (speed: number) => void;
  openRecap: (scope: RecapScope, section?: { id: string; title: string }) => void; // section: a chapter asked for by name
  showCast: () => void;
};

/** The result of a tool: `output` goes back to the model, `chip` is shown in the chat; the rest tells the talk loop what to do once the answer ends. */
export type ToolOutcome = {
  ok: boolean;
  output: string;
  chip: string | null;
  /** The chunk to continue reading from (the reader was moved). */
  moveTo?: number;
  /** Reading must go on once the character has finished speaking (read_chapter, resume_reading). Unset: as before the question. */
  play?: boolean;
  /** Reading should not go on afterwards (pause_reading, quiz_me, explain_here). */
  pause?: boolean;
  /** The Gemini path (no Realtime model to read `output`): the question to answer aloud instead of ending the turn silently (<= 500 chars). */
  ask?: string;
  /** quiz_me: the talk loop enters quiz mode with this section (see `quizTurnInstructions`). */
  quiz?: Quiz;
};

/** A quiz in progress (quiz_me): the section and its text, kept by the talk loop for the next turns. */
export type Quiz = { title: string; text: string };
export const QUIZ_QUESTIONS = 3;

/** What the reader says that ends a quiz early: stop / cancel / "keep reading", or any command (navigation and the rest). */
export function endsQuiz(text: string): boolean {
  const i = parseIntent(text);
  if (i) return i.name !== "quiz_me";
  const t = norm(text).replace(/[,;:!?.]+/g, " ").replace(/\s+/g, " ").trim();
  return /^(?:(?:ok|okay|no|please|let's|lets) )*(?:stop|cancel|quit|end|enough|skip|no more)(?: (?:the|this))?(?: (?:quiz|questions|it))?(?: please| now)?$/.test(t)
    || /^(?:i'?m done|that's enough|thats enough|no more questions)$/.test(t);
}

/** The quiz part of the instructions for the reader's answer to question `n` (Realtime: added to the grounding). */
export function quizTurnInstructions(q: Quiz, n: number): string {
  const next = n < QUIZ_QUESTIONS
    ? `Then ask question ${n + 1}: one short question on the text, and wait for the answer.`
    : `That was the last question: give the final score out of ${QUIZ_QUESTIONS} (count your right/wrong gradings above) and offer to keep reading.`;
  return `QUIZ MODE on "${q.title}": the reader just answered question ${n} of ${QUIZ_QUESTIONS}. Say right or wrong with a one-line explanation from the text. ${next} Keep it short; no markdown.\nQuiz text:\n${q.text}`;
}

/** The Gemini path: the reader's answer, wrapped with the quiz progress (the earlier turns are in the chat history). <= 500 chars. */
export function quizGeminiQuery(q: Quiz, n: number, answer: string): string {
  const next = n < QUIZ_QUESTIONS ? `then ask question ${n + 1}, one short question, and wait.` : `then give my final score out of ${QUIZ_QUESTIONS} and offer to keep reading.`;
  return `Quiz on "${clip(q.title, 80)}", question ${n} of ${QUIZ_QUESTIONS}. My answer: "${clip(answer, 200)}". Say right or wrong in one line, ${next}`;
}

export type ToolRunner = (name: string, args: Record<string, unknown>) => ToolOutcome;

const norm = (s: string) => s.toLowerCase().replace(/[“”"'‘’]/g, "").replace(/\s+/g, " ").trim();
const ROMAN: Record<string, number> = { i: 1, ii: 2, iii: 3, iv: 4, v: 5, vi: 6, vii: 7, viii: 8, ix: 9, x: 10, xi: 11, xii: 12, xiii: 13, xiv: 14, xv: 15, xvi: 16, xvii: 17, xviii: 18, xix: 19, xx: 20 };
const WORDS: Record<string, number> = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12, first: 1, second: 2, third: 3, last: -1 };

/** "3", "three", "iii", "chapter 3" -> 3; "last" -> -1; anything else -> null. */
export function chapterNumber(s: string): number | null {
  const t = norm(s).replace(/^(chapter|part|section|ch\.?)\s+/, "");
  if (/^\d+$/.test(t)) return Number(t);
  if (ROMAN[t]) return ROMAN[t];
  if (WORDS[t] !== undefined) return WORDS[t];
  return null;
}

/** The chapters a number counts through: top-level sections (textbooks nest subsections under them). */
export function topSections<S extends { level?: number }>(sections: S[]): S[] {
  return sections.filter((s) => (s.level ?? 1) <= 1);
}

/** The section meant by `chapter` (a number, a Roman numeral, "last", or words from the title), or null. */
export function findChapter<S extends { title: string; level?: number }>(sections: S[], chapter: string): S | null {
  const top = topSections(sections);
  const n = chapterNumber(chapter);
  if (n !== null) {
    if (n === -1) return top[top.length - 1] ?? null;
    // numbered titles ("Chapter 3", "III. The Letters") first, then the nth chapter
    const byTitle = top.find((s) => { const m = /^(?:chapter|part|section|ch\.?)?\s*([ivxlc]+|\d+)\b/i.exec(norm(s.title)); return !!m && chapterNumber(m[1]) === n; });
    return byTitle ?? top[n - 1] ?? null;
  }
  const q = norm(chapter);
  if (!q) return null;
  return top.find((s) => norm(s.title) === q) ?? top.find((s) => norm(s.title).includes(q)) ?? sections.find((s) => norm(s.title).includes(q)) ?? null;
}

/** First chunk of paragraph `para` or of the next paragraph that has one; null past the end. */
export function chunkAtPara(chunks: Pick<FlatChunk, "para">[], para: number): number | null {
  const i = chunks.findIndex((c) => c.para >= para);
  return i < 0 ? null : i;
}

type Jump = { ok: true; index: number; para: number; label: string } | { ok: false; output: string };

/** A textbook is read out of order: no spoiler limit. A novel has one unless the reader turned spoilers on. */
const unlimited = (ctx: ToolContext) => ctx.spoilers || ctx.kind === "textbook";

const NUM_WORDS: Record<string, string> = { zero: "0", one: "1", two: "2", three: "3", four: "4", five: "5", six: "6", seven: "7", eight: "8", nine: "9", ten: "10", eleven: "11", twelve: "12" };

/** "the section on inverted indexes" -> words "inverted indexes"; "section two point three" -> number "2.3". */
export function parseSectionQuery(s: string): { num: string | null; words: string } {
  const words = norm(s).replace(/[?!,;]+/g, "").replace(/\.$/, "")
    .replace(/^(?:the )?(?:sub)?(?:chapter|section|part|ch\.?)s? /, "").replace(/^(?:on|about|covering|for|called|named) /, "").trim();
  const spoken = words.split(" ").map((w) => NUM_WORDS[w] ?? w).join(" ").replace(/(\d) (?:point|dot) (?=\d)/g, "$1.");
  return { num: /^\d+(?:\.\d+)*$/.test(spoken) ? spoken : null, words };
}

/** A textbook place: a section by number (any depth), by chapter word ("three", "last"), by title, or by topic. */
function resolveTextbookPlace(ctx: ToolContext, query: string): { para: number; label: string } | null {
  const { num, words } = parseSectionQuery(query);
  if (num) {
    const s = findSectionByNumber(ctx.sections, num) ?? (num.includes(".") ? null : findChapter(ctx.sections, num));
    return s ? { para: s.start_para, label: s.title } : null;
  }
  if (chapterNumber(words) !== null) { const s = findChapter(ctx.sections, words); if (s) return { para: s.start_para, label: s.title }; }
  const t = findSectionByTitle(ctx.sections, words, ctx.readerPara);
  if (t && t.score === 1) return { para: t.section.start_para, label: t.section.title };
  const hit = findTopic(ctx.sections, ctx.paragraphs, words, { readerPara: ctx.readerPara });
  return hit ? { para: hit.para, label: hit.section?.title ?? `paragraph ${hit.para + 1}` } : null;
}

/** Where `jump_to` / `read_chapter` should go, or why not. Spoilers off (novels): never past `readerPara`. */
export function resolveJump(ctx: ToolContext, args: { chapter?: unknown; section?: unknown; passage?: unknown }): Jump {
  const where = args.section ?? args.chapter;
  const chapter = typeof where === "string" ? where : typeof where === "number" ? String(where) : "";
  const passage = typeof args.passage === "string" ? args.passage : "";
  const limit = unlimited(ctx) ? Number.POSITIVE_INFINITY : ctx.readerPara;
  const land = (para: number, label: string): Jump => {
    const index = chunkAtPara(ctx.chunks, para);
    if (index === null) return { ok: false, output: "That is past the end of the book." };
    if (ctx.chunks[index].para > limit) return { ok: false, output: `${label} is past where the reader has read, and spoilers are off. Say so briefly.` };
    return { ok: true, index, para: ctx.chunks[index].para, label };
  };

  if (chapter.trim()) {
    if (/^bookmark/i.test(chapter.trim())) return jumpToBookmark(ctx, land);
    if (ctx.kind === "textbook") {
      const p = resolveTextbookPlace(ctx, chapter);
      if (!p) return { ok: false, output: `No section matches "${chapter}". Say so briefly.` };
      return land(p.para, p.label);
    }
    const s = findChapter(ctx.sections, chapter);
    if (!s) return { ok: false, output: `No chapter matches "${chapter}". Chapters are numbered 1 to ${topSections(ctx.sections).length}.` };
    return land(s.start_para, s.title);
  }
  if (passage.trim()) {
    if (/^bookmark/i.test(passage.trim())) return jumpToBookmark(ctx, land);
    const q = norm(passage);
    const hits = ctx.paragraphs.filter((p) => norm(p.text).includes(q)).map((p) => p.idx);
    if (!hits.length) return { ok: false, output: `Could not find "${passage}" in the book.` };
    const before = hits.filter((i) => i <= ctx.readerPara);
    // the nearest match at or before the reader; with spoilers on, the first later one when there is none before
    const para = before.length ? Math.max(...before) : unlimited(ctx) ? Math.min(...hits) : null;
    if (para === null) return { ok: false, output: `"${passage}" only appears later in the book, and spoilers are off. Say so briefly.` };
    return land(para, `paragraph ${para + 1}`);
  }
  return { ok: false, output: "Say which chapter or quote a passage." };
}

function jumpToBookmark(ctx: ToolContext, land: (para: number, label: string) => Jump): Jump {
  const b = bookmarks(ctx.bookId).at(-1);
  if (!b) return { ok: false, output: "There is no bookmark yet." };
  return land(b.para, `the bookmark (${b.label})`);
}

/** The chapter title at `para` (the last section starting at or before it). */
export function chapterTitleAt(sections: ToolContext["sections"], para: number): string | null {
  let t: string | null = null;
  for (const s of sections) if (s.start_para <= para) t = s.title; else break;
  return t;
}

/** The next speed up or down, or a multiplier the reader asked for, snapped to the offered steps. */
export function pickSpeed(current: number, speeds: number[], want: string | number): number | null {
  const steps = [...speeds].sort((a, b) => a - b);
  const at = steps.indexOf(current);
  const w = typeof want === "number" ? String(want) : norm(want);
  if (/faster|quick|speed up|up/.test(w)) return steps[Math.min((at < 0 ? steps.indexOf(1) : at) + 1, steps.length - 1)];
  if (/slower|slow down|down/.test(w)) return steps[Math.max((at < 0 ? steps.indexOf(1) : at) - 1, 0)];
  if (/normal|default|regular|1x/.test(w)) return steps.includes(1) ? 1 : null;
  const n = Number.parseFloat(w.replace(/x$/, ""));
  if (!Number.isFinite(n)) return null;
  return steps.reduce((best, s) => (Math.abs(s - n) < Math.abs(best - n) ? s : best), steps[0]);
}

// ---- bookmarks (localStorage, per book) ----

export type Bookmark = { index: number; para: number; label: string; at: number };
const bmKey = (bookId: string) => `talkback:bookmarks:${bookId}`;

export function bookmarks(bookId: string): Bookmark[] {
  try {
    const v = JSON.parse(localStorage.getItem(bmKey(bookId)) ?? "[]");
    return Array.isArray(v) ? v.filter((b) => b && typeof b.index === "number" && typeof b.para === "number") : [];
  } catch { return []; }
}

export function addBookmark(bookId: string, b: Omit<Bookmark, "at">): Bookmark[] {
  const next = [...bookmarks(bookId).filter((x) => x.index !== b.index), { ...b, at: Date.now() }].slice(-20);
  try { localStorage.setItem(bmKey(bookId), JSON.stringify(next)); } catch { /* storage blocked: the chip still shows */ }
  return next;
}

// ---- quiz and explain: text handed to the model ----

type Para = ToolContext["paragraphs"][number];

/** A paragraph as the model should see it: an equation as LaTeX plus its spoken form, a figure as its caption; code is skipped. */
export function paraForModel(p: Para): string | null {
  if (p.kind === "code") return null;
  if (p.kind === "math") {
    const latex = p.math?.latex?.trim(), spoken = p.math?.spoken?.trim();
    if (latex || spoken) return `[Equation${latex ? `: ${latex}` : ""}${spoken ? ` (read as: ${spoken})` : ""}]`;
  }
  if (p.kind === "figure") return `[Figure caption: ${p.text.trim()}]`;
  return p.text.trim() || null;
}

const clip = (s: string, max: number) => (s.length <= max ? s : s.slice(0, max).replace(/\s+\S*$/, "") + " ...");

const QUIZ_MAX = 3000;

// ---- the dispatcher ----

const refuse = (output: string): ToolOutcome => ({ ok: false, output, chip: null });

/** Run one tool against the reader. Unknown names are refused (the model gets told), never thrown. */
export function runTool(name: string, args: Record<string, unknown>, ctx: ToolContext, act: ToolActions): ToolOutcome {
  const place = (para: number) => { const c = chapterTitleAt(ctx.sections, para); return c ? `${c}, paragraph ${para + 1}` : `paragraph ${para + 1}`; };
  switch (name) {
    case "jump_to":
    case "read_chapter": {
      const j = resolveJump(ctx, name === "read_chapter" ? { chapter: args.section ?? args.chapter ?? args.passage } : args);
      if (!j.ok) return refuse(j.output);
      act.seek(j.index);
      const play = name === "read_chapter" ? true : undefined; // a jump keeps reading only if the reader was reading
      const at = ctx.kind === "textbook" ? j.label : `${j.label} (${place(j.para)})`;
      return { ok: true, output: `Moved to ${at}.${play ? " Reading starts there after you answer." : ""} Confirm in one short sentence.`,
        chip: `Jumped to ${j.label}`, moveTo: j.index, ...(play ? { play } : {}) };
    }
    case "next_section":
    case "previous_section": {
      const unit = args.unit === "chapter" ? "chapter" : "section";
      const dir = name === "next_section" ? "next" : "previous";
      const s = adjacentSection(ctx.sections, ctx.readerPara, dir, unit);
      if (!s) return refuse(`This is the ${dir === "next" ? "last" : "first"} ${unit}. Say so briefly.`);
      const index = chunkAtPara(ctx.chunks, s.start_para);
      if (index === null) return refuse("That is past the end of the book.");
      if (!unlimited(ctx) && ctx.chunks[index].para > ctx.readerPara) return refuse(`The next ${unit} is past where the reader has read, and spoilers are off. Say so briefly.`);
      act.seek(index);
      return { ok: true, output: `Moved to ${s.title}. Confirm in one short sentence.`, chip: `${dir === "next" ? "Next" : "Back"}: ${s.title}`, moveTo: index };
    }
    case "go_back": {
      const count = (v: unknown) => { const n = Math.floor(Number(v)); return Number.isFinite(n) && n >= 0 ? Math.min(n, 50) : null; };
      const sentences = count(args.sentences);
      const paragraphs = count(args.paragraphs) ?? 0;
      let i = Math.max(0, Math.min(ctx.index, ctx.chunks.length - 1));
      let what: string;
      if (sentences !== null && args.paragraphs === undefined) {
        i = Math.max(0, i - sentences);
        what = sentences === 0 ? "the start of the sentence" : `${sentences} sentence${sentences === 1 ? "" : "s"} back`;
      } else {
        const startOf = (k: number) => { while (k > 0 && ctx.chunks[k - 1].para === ctx.chunks[k].para) k--; return k; };
        i = startOf(i);
        for (let n = 0; n < paragraphs && i > 0; n++) i = startOf(i - 1);
        what = paragraphs === 0 ? "the start of the paragraph" : `${paragraphs} paragraph${paragraphs === 1 ? "" : "s"} back`;
      }
      if (!ctx.chunks.length) return refuse("There is nothing to go back to.");
      act.seek(i);
      return { ok: true, output: `Rewound to ${what}; reading resumes there after you answer. Say only a few words, like "Sure, again."`, chip: `Back to ${what}`, moveTo: i, play: true };
    }
    case "quiz_me": {
      const want = typeof args.section === "string" ? args.section.trim() : "";
      let sec: ToolContext["sections"][number] | null;
      if (!want || /^(?:this|the|current)? ?(?:section|part|one|bit)?$/.test(norm(want)) || /^(?:this|that|it|what i (?:just )?read)$/.test(norm(want))) sec = sectionAt(ctx.sections, ctx.readerPara);
      else if (/^(?:this|the current) chapter$/.test(norm(want))) sec = sectionAt(topSections(ctx.sections), ctx.readerPara);
      else {
        const p = ctx.kind === "textbook" ? resolveTextbookPlace(ctx, want) : null;
        sec = p ? sectionAt(ctx.sections, p.para) : findChapter(ctx.sections, want);
        if (p && sec && sec.start_para !== p.para) sec = sectionAt(ctx.sections, p.para); // a topic hit: the section it is in
      }
      if (!sec) return refuse(`No section matches "${want}". Say so briefly.`);
      const end = Math.min(sectionEnd(ctx.sections, sec), unlimited(ctx) ? Number.POSITIVE_INFINITY : ctx.readerPara + 1);
      if (end <= sec.start_para) return refuse(`${sec.title} is past where the reader has read, and spoilers are off. Say so briefly.`);
      let text = "";
      for (const p of ctx.paragraphs) {
        if (p.idx < sec.start_para || p.idx >= end) continue;
        const t = paraForModel(p);
        if (!t) continue;
        if (text.length + t.length > QUIZ_MAX) { text += (text ? "\n" : "") + clip(t, Math.max(0, QUIZ_MAX - text.length)); break; }
        text += (text ? "\n" : "") + t;
      }
      if (!text.trim()) return refuse(`${sec.title} has no text to quiz on. Say so briefly.`);
      act.pause();
      return {
        ok: true, pause: true, chip: `Quiz: ${sec.title}`, quiz: { title: sec.title, text },
        output: `Quiz on "${sec.title}"; reading stays paused. Ask 3 short questions on the text below, one at a time. After each spoken answer say right or wrong with a one-line explanation. Then give the score and offer to keep reading.\nText:\n${text}`,
        ask: `Quiz me on "${clip(sec.title, 120)}": ask me one short question about it, then wait for my answer. After I answer, say right or wrong in one line and ask the next; after 3 questions give my score.`,
      };
    }
    case "explain_here": {
      const what = args.what === "equation" ? "equation" : args.what === "figure" ? "figure" : "paragraph";
      const cur = ctx.paragraphs.find((p) => p.idx === ctx.readerPara);
      let target: Para | undefined = cur;
      if (what !== "paragraph") {
        const kind = what === "equation" ? "math" : "figure";
        // the nearest one just read (or on screen just after): a few paragraphs back first, then one or two ahead; a novel
        // with spoilers off never looks past the reader
        const ahead = unlimited(ctx) ? 2 : 0;
        const near = ctx.paragraphs.filter((p) => p.kind === kind && p.idx >= ctx.readerPara - 6 && p.idx <= ctx.readerPara + ahead);
        target = near.filter((p) => p.idx <= ctx.readerPara).at(-1) ?? near[0];
        if (!target) return refuse(`There is no ${what} near here. Say so briefly.`);
      }
      const body = target ? paraForModel(target) : null;
      if (!target || !body) return refuse("There is nothing here to explain. Say so briefly.");
      const prevP = [...ctx.paragraphs].reverse().find((p) => p.idx < target!.idx && paraForModel(p));
      const before = prevP ? paraForModel(prevP) : null;
      act.pause();
      const label = what === "paragraph" ? (target.kind === "math" ? "the equation" : target.kind === "figure" ? "the figure" : "this paragraph") : `the ${what}`;
      return {
        ok: true, pause: true, chip: `Explaining ${label}`,
        output: `Explain ${label} simply, in 2-4 sentences, then offer to continue reading.\n${before ? `Context before: ${clip(before, 800)}\n` : ""}To explain: ${clip(body, 1500)}`,
        ask: `Explain ${label} simply, in 2-4 sentences: ${clip(body, 420 - label.length)}`,
      };
    }
    case "find_topic": {
      const query = typeof args.query === "string" ? args.query.trim() : "";
      if (!query) return refuse("Say which topic to find.");
      const hit = findTopic(ctx.sections, ctx.paragraphs, query, { maxPara: unlimited(ctx) ? undefined : ctx.readerPara, readerPara: ctx.readerPara });
      if (!hit) return refuse(`The book does not seem to cover "${query}"${unlimited(ctx) ? "" : " in the part read so far"}. Say so briefly.`);
      const index = chunkAtPara(ctx.chunks, hit.para);
      if (index === null || (!unlimited(ctx) && ctx.chunks[index].para > ctx.readerPara)) return refuse(`"${query}" only comes later, and spoilers are off. Say so briefly.`);
      act.seek(index);
      const title = hit.section?.title ?? `paragraph ${hit.para + 1}`;
      const num = hit.section ? titleNumber(hit.section.title) : null;
      const say = num ? `section ${num}, ${titleText(hit.section!.title)}` : `"${title}"`;
      return { ok: true, output: `Found in ${say}; moved there. Tell the reader in one sentence, e.g. "That's in ${say}; jumping there."`, chip: `Found in ${title}`, moveTo: index };
    }
    case "resume_reading":
      return { ok: true, output: `Reading continues from ${place(ctx.readerPara)} after you answer. Confirm in a few words.`, chip: "Reading resumes", play: true };
    case "pause_reading":
      act.pause();
      return { ok: true, output: `Paused at ${place(ctx.readerPara)}. Confirm in a few words.`, chip: "Reading paused", pause: true };
    case "set_voice_mode": {
      const mode = args.mode === "one" ? "one" : args.mode === "two" ? "two" : null;
      if (!mode) return refuse("mode must be 'one' or 'two'.");
      act.setVoiceMode(mode);
      return { ok: true, output: mode === "one" ? "Now reading in one voice: the narrator reads every line." : "Now reading in two voices: a male and a female voice.", chip: mode === "one" ? "One voice" : "Two voices" };
    }
    case "set_narrator": {
      const g = args.gender === "male" ? "male" : args.gender === "female" ? "female" : null;
      if (!g) return refuse("gender must be 'male' or 'female'.");
      act.setNarrator(g);
      return { ok: true, output: `The ${g} voice narrates now.`, chip: `${g === "male" ? "Male" : "Female"} narrator` };
    }
    case "set_speed": {
      const want = typeof args.speed === "number" || typeof args.speed === "string" ? args.speed : "";
      const s = pickSpeed(ctx.speed, ctx.speeds, want);
      if (s === null) return refuse(`Speeds are ${ctx.speeds.join(", ")}; say one of them, or faster / slower.`);
      act.setSpeed(s);
      return { ok: true, output: s === ctx.speed ? `The speed is already ${s}x.` : `Reading speed set to ${s}x.`, chip: `Speed ${s}x` };
    }
    case "open_recap": {
      const asked = typeof args.chapter === "string" ? args.chapter : typeof args.chapter === "number" ? String(args.chapter) : "";
      if (asked.trim()) {
        const book = ctx.kind === "textbook" || ctx.textbook;
        let s: ToolContext["sections"][number] | null;
        if (book) { // sections at any depth, spoken numbers, titles, topics: the same places jump_to reaches
          const p = resolveTextbookPlace(ctx, asked);
          s = p ? sectionAt(ctx.sections, p.para) : null;
          if (!s) return refuse(`No chapter or section matches "${asked}". Say so briefly.`);
        } else s = findChapter(ctx.sections, asked);
        if (!s) return refuse(`No chapter matches "${asked}". Chapters are numbered 1 to ${topSections(ctx.sections).length}.`);
        if (!ctx.textbook && !ctx.spoilers && s.start_para > ctx.readerPara) return refuse(`${s.title} is past where the reader has read, and spoilers are off. Say so briefly.`);
        act.openRecap("chapter", { id: s.id, title: s.title });
        return { ok: true, output: `The narrator is summarising ${s.title} in the chat; do not summarise it yourself. Say so in a few words.`, chip: `Recap: ${s.title}` };
      }
      const scope: RecapScope = args.scope === "book" ? "book" : args.scope === "series" && ctx.inSeries ? "series" : "chapter";
      act.openRecap(scope);
      const label = scope === "chapter" ? (ctx.textbook ? "this chapter" : "the last chapter") : scope === "book" ? "this book so far" : "the series so far";
      return { ok: true, output: `The narrator is recapping ${label} in the chat; do not recap it yourself. Say so in a few words.`, chip: `Recap: ${label}` };
    }
    case "bookmark": {
      const label = place(ctx.readerPara);
      addBookmark(ctx.bookId, { index: ctx.index, para: ctx.readerPara, label });
      return { ok: true, output: `Bookmarked ${label}. Confirm in a few words.`, chip: `Bookmarked ${label}` };
    }
    case "show_cast": {
      act.showCast();
      const met = ctx.characters.filter((c) => c.main !== false && (ctx.spoilers || c.first_para <= ctx.readerPara)).map((c) => c.name);
      return { ok: true, output: met.length ? `The voices panel is open. Characters met so far: ${met.join(", ")}. Name them briefly.` : "The voices panel is open. No named characters have appeared yet.", chip: "Showed the cast" };
    }
    default:
      return refuse(`Unknown tool ${name}.`);
  }
}

// ---- the Gemini path: a small intent parse instead of model tool calls ----

export type Intent = { name: ToolName; args: Record<string, unknown> };

/**
 * A spoken command the Gemini path can act on without the model (no Realtime, so no function calling): a few plain
 * phrasings only, so a real question is never swallowed. Anything else is null and goes to Gemini as a question.
 */
export function parseIntent(text: string): Intent | null {
  // the transcriber writes speech as prose (#102): "Okay, read chapter 2, please!" must parse like "read chapter 2"
  const t = norm(text)
    .replace(/[,;:!?]+/g, " ").replace(/\.+$/, "").replace(/\s+/g, " ").trim()
    .replace(/^(?:hey|hi|ok|okay|so|um|uh|now)\s+/, "")
    .replace(/^(?:can|could|would|will)\s+you\s+(?:please\s+)?/, "")
    .replace(/\s+please$/, "");
  let m: RegExpExecArray | null;
  // where a topic is covered (textbooks): only with a "covered / explained / which section" verb, so "where is Alice?" stays a question
  const topic = (q: string): Intent => ({ name: "find_topic", args: { query: q.replace(/ in (?:the|this) (?:book|text|textbook)$/, "") } });
  if ((m = /^where (?:is|are) (.+?) (?:covered|discussed|explained|introduced|defined|described|talked about|mentioned)(?: in (?:the|this) (?:book|text|textbook))?$/.exec(t))) return topic(m[1]);
  if ((m = /^where (?:does|do) (?:the|this|they|it) (?:book |text |textbook )?(?:talk about|cover|discuss|explain|introduce|describe|mention|define) (.+)$/.exec(t))) return topic(m[1]);
  if ((m = /^(?:which|what) (?:section|chapter|part) (?:covers|talks about|discusses|explains|introduces|describes|defines|is about) (.+)$/.exec(t))) return topic(m[1]);
  if ((m = /^(?:find|look up|search for) (?:the )?(?:section|part|chapter|bit) (?:on|about|covering|that covers|that explains) (.+)$/.exec(t))) return topic(m[1]);
  if ((m = /^where can i (?:read|learn|find out) about (.+)$/.exec(t))) return topic(m[1]);
  // quiz me / explain this
  if ((m = /^(?:please )?(?:quiz|test) me(?: (?:on|about) (.+))?$/.exec(t)) || (m = /^(?:give me|do|start) (?:a|the) (?:quick )?quiz(?: (?:on|about) (.+))?$/.exec(t))) {
    const on = (m[1] ?? "").trim();
    return { name: "quiz_me", args: !on || /^(?:this|the|this section|the section|that|it|what i (?:just )?read)$/.test(on) ? {} : { section: on } };
  }
  const explainWhat = (w: string | undefined): Record<string, unknown> =>
    !w ? {} : /equation|formula|math/.test(w) ? { what: "equation" } : /figure|diagram|picture|image|graph|chart|table/.test(w) ? { what: "figure" } : {};
  if ((m = /^(?:please )?(?:explain|break down)(?: (?:this|that|it|the))?(?: (equation|formula|math|figure|diagram|picture|image|graph|chart|table|paragraph|part|bit|passage))?(?: (?:to me|again|simply|more simply))?$/.exec(t))) return { name: "explain_here", args: explainWhat(m[1]) };
  if ((m = /^what (?:does|did) (?:this|that|the|it)(?: (equation|formula|figure|diagram|picture|graph|chart|table|paragraph|passage))? ?(?:mean|show|say)$/.exec(t))) return { name: "explain_here", args: explainWhat(m[1]) };
  // next / previous section or chapter
  if ((m = /^(?:please )?(?:(?:go|skip|jump|move)(?: on| ahead| forward)? to )?(?:the )?next (section|chapter)$/.exec(t)) || (m = /^skip (?:this|the) (section|chapter)$/.exec(t)))
    return { name: "next_section", args: m[1] === "chapter" ? { unit: "chapter" } : {} };
  if ((m = /^(?:please )?(?:(?:go|jump|move) (?:back )?to )?(?:the )?previous (section|chapter)$/.exec(t)) || (m = /^(?:go )?back (?:a|one) (section|chapter)$/.exec(t)))
    return { name: "previous_section", args: m[1] === "chapter" ? { unit: "chapter" } : {} };
  // say that again / go back a bit
  if (/^(?:say|read) (?:that|it|this) again$/.test(t) || /^repeat(?: that| this| it)?(?: again)?$/.test(t) || /^come again$/.test(t)
    || /^repeat (?:the|that|this) (?:last )?paragraph$/.test(t) || /^(?:read|say|start) (?:the|that|this) (?:last )?paragraph (?:again|over)$/.test(t)) return { name: "go_back", args: {} };
  if (/^repeat (?:the|that) (?:last )?sentence$/.test(t) || /^(?:read|say) (?:the|that) (?:last )?sentence again$/.test(t)) return { name: "go_back", args: { sentences: 0 } };
  if (/^(?:go back|back up|rewind)(?: a (?:bit|little|paragraph)| one paragraph)?$/.test(t)) return { name: "go_back", args: { paragraphs: 1 } };
  if ((m = /^(?:go back|back up|rewind) (\d+|[a-z]+) (paragraph|sentence)s?$/.exec(t))) {
    const n = /^\d+$/.test(m[1]) ? Number(m[1]) : m[1] === "a" ? 1 : WORDS[m[1]];
    if (n !== undefined && n > 0) return { name: "go_back", args: m[2] === "sentence" ? { sentences: n } : { paragraphs: n } };
  }
  // chapters and sections: by number ("2.3", "2 point 3") or by title ("the section on inverted indexes")
  if ((m = /^(?:please )?(?:read|start|begin)(?: me)? (?:the )?(?:chapter|part|section) (?:on|about|covering) (.+)$/.exec(t))) return { name: "read_chapter", args: { chapter: m[1] } };
  if ((m = /^(?:please )?(?:jump|go|skip|take me|move)(?: back| ahead| forward)? to (?:the )?(?:chapter|part|section) (?:on|about|covering) (.+)$/.exec(t))) return { name: "jump_to", args: { chapter: m[1] } };
  if ((m = /^(?:please )?(?:read|start|begin)(?: me)? (?:chapter|part|section) (\d+(?: point \d+)+|\S+)(?: (?:aloud|out loud|please))?$/.exec(t))) return { name: "read_chapter", args: { chapter: m[1] } };
  if ((m = /^(?:please )?(?:jump|go|skip|take me|move)(?: back| ahead| forward)? to (?:the )?(?:(?:chapter|part|section) (\d+(?: point \d+)+|\S+)|(?:my )?bookmark|(.+))$/.exec(t))) {
    if (m[1]) return { name: "jump_to", args: { chapter: m[1] } };
    if (!m[2]) return { name: "jump_to", args: { chapter: "bookmark" } };
    if (/^\d+(?:\.\d+)+$/.test(m[2])) return { name: "jump_to", args: { chapter: m[2] } }; // "go to 4.1"
    const where = m[2].replace(/^(?:the )?(?:part|bit|passage|line|place) (?:where|with|that says|about) /, "");
    return { name: "jump_to", args: { passage: where } };
  }
  if (/^(?:please )?(?:resume|continue|keep|carry on|go on)(?: reading| the story| the book)?(?: please)?$/.test(t) || /^(?:resume|continue) reading$/.test(t)) return { name: "resume_reading", args: {} };
  if (/^(?:please )?(?:pause|stop)(?: reading| the story| the book)?(?: please)?$/.test(t)) return { name: "pause_reading", args: {} };
  if ((m = /^(?:please )?(?:read|switch|use|go)(?: to)? (?:in )?(one|two|a single|both) voices?$/.exec(t))) return { name: "set_voice_mode", args: { mode: m[1] === "two" || m[1] === "both" ? "two" : "one" } };
  if ((m = /^(?:please )?(?:use|switch to|make it|i want)(?: the| a)? (male|female|man's|woman's) (?:voice|narrator)(?: for the narrator| to narrate)?$/.exec(t))) return { name: "set_narrator", args: { gender: m[1].startsWith("m") ? "male" : "female" } };
  if ((m = /^(?:please )?(?:read |go |speak |talk )?(?:a (?:bit|little) )?(faster|slower|quicker|speed up|slow down)(?: please)?$/.exec(t))) return { name: "set_speed", args: { speed: /fast|quick|up/.test(m[1]) ? "faster" : "slower" } };
  if ((m = /^(?:please )?(?:set |change )?(?:the )?(?:reading )?speed (?:to )?(\d+(?:\.\d+)?)x?$/.exec(t)) || (m = /^(?:read at |go at |)(\d+(?:\.\d+)?)x(?: speed)?$/.exec(t))) return { name: "set_speed", args: { speed: m[1] } };
  if ((m = /^(?:please )?(?:recap|summarize|summarise|sum up|catch me up on|remind me (?:of|about)) (?:chapter|section|part) (\S+)$/.exec(t))) {
    return { name: "open_recap", args: { scope: "chapter", chapter: m[1] } };
  }
  if ((m = /^(?:please )?(?:recap|summarize|summarise|catch me up(?: on)?|remind me(?: of| about)?)(?: the| this)? ?(last chapter|chapter|book|series|story|everything)?(?: so far)?(?: please)?$/.exec(t))) {
    const w = m[1] ?? "";
    return { name: "open_recap", args: { scope: /series|everything/.test(w) ? "series" : /book|story/.test(w) ? "book" : "chapter" } };
  }
  if (/^(?:please )?(?:bookmark|save)(?: this| my)?(?: (?:place|spot|page|position))?(?: please)?$/.test(t) || /^(?:add a |set a |drop a )?bookmark(?: here)?$/.test(t)) return { name: "bookmark", args: {} };
  if (/^(?:please )?(?:show|open|list)(?: me)? the (?:cast|characters|voices)(?: panel)?$/.test(t) || /^who(?:'s| is) in the cast$/.test(t)) return { name: "show_cast", args: {} };
  return null;
}
