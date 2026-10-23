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

export type Gender = "male" | "female";

/** The function tools, in the shape `session.update` takes (`tools[]`, type "function"). */
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
    description: "Have the narrator recap the story so far: the last chapter, this book so far, or the series so far.",
    parameters: { type: "object", properties: { scope: { type: "string", enum: ["chapter", "book", "series"] } } },
  },
  { type: "function", name: "bookmark", description: "Bookmark the reader's current place in the book.", parameters: { type: "object", properties: {} } },
  { type: "function", name: "show_cast", description: "Show the cast: the characters the reader has met so far, and open the voices panel.", parameters: { type: "object", properties: {} } },
] as const;

export type ToolName = (typeof TOOL_DEFS)[number]["name"];
export const TOOL_NAMES: readonly string[] = TOOL_DEFS.map((t) => t.name);

/** What the tools know about the reader right now (a snapshot taken when the tool runs). */
export type ToolContext = {
  bookId: string;
  sections: { id: string; title: string; start_para: number; level?: number }[];
  paragraphs: { idx: number; text: string }[];
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
};

/** What the tools may do to the reader. */
export type ToolActions = {
  seek: (index: number) => void; // move the position without starting playback
  pause: () => void;
  setVoiceMode: (mode: VoiceMode) => void;
  setNarrator: (g: Gender) => void;
  setSpeed: (speed: number) => void;
  openRecap: (scope: RecapScope) => void;
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
  /** Reading should not go on afterwards (pause_reading). */
  pause?: boolean;
};

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

/** Where `jump_to` / `read_chapter` should go, or why not. Spoilers off: never past `readerPara`. */
export function resolveJump(ctx: ToolContext, args: { chapter?: unknown; passage?: unknown }): Jump {
  const chapter = typeof args.chapter === "string" ? args.chapter : typeof args.chapter === "number" ? String(args.chapter) : "";
  const passage = typeof args.passage === "string" ? args.passage : "";
  const limit = ctx.spoilers ? Number.POSITIVE_INFINITY : ctx.readerPara;
  const land = (para: number, label: string): Jump => {
    const index = chunkAtPara(ctx.chunks, para);
    if (index === null) return { ok: false, output: "That is past the end of the book." };
    if (ctx.chunks[index].para > limit) return { ok: false, output: `${label} is past where the reader has read, and spoilers are off. Say so briefly.` };
    return { ok: true, index, para: ctx.chunks[index].para, label };
  };

  if (chapter.trim()) {
    if (/^bookmark/i.test(chapter.trim())) return jumpToBookmark(ctx, land);
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
    const para = before.length ? Math.max(...before) : ctx.spoilers ? Math.min(...hits) : null;
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

// ---- the dispatcher ----

const refuse = (output: string): ToolOutcome => ({ ok: false, output, chip: null });

/** Run one tool against the reader. Unknown names are refused (the model gets told), never thrown. */
export function runTool(name: string, args: Record<string, unknown>, ctx: ToolContext, act: ToolActions): ToolOutcome {
  const place = (para: number) => { const c = chapterTitleAt(ctx.sections, para); return c ? `${c}, paragraph ${para + 1}` : `paragraph ${para + 1}`; };
  switch (name) {
    case "jump_to":
    case "read_chapter": {
      const j = resolveJump(ctx, name === "read_chapter" ? { chapter: args.chapter ?? args.passage } : args);
      if (!j.ok) return refuse(j.output);
      act.seek(j.index);
      const play = name === "read_chapter" ? true : undefined; // a jump keeps reading only if the reader was reading
      return { ok: true, output: `Moved to ${j.label} (${place(j.para)}).${play ? " Reading starts there after you answer." : ""} Confirm in one short sentence.`,
        chip: `Jumped to ${j.label}`, moveTo: j.index, ...(play ? { play } : {}) };
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
      const scope: RecapScope = args.scope === "book" ? "book" : args.scope === "series" && ctx.inSeries ? "series" : "chapter";
      act.openRecap(scope);
      const label = scope === "chapter" ? "the last chapter" : scope === "book" ? "this book so far" : "the series so far";
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
  const t = norm(text).replace(/[.!?]+$/, "");
  let m: RegExpExecArray | null;
  if ((m = /^(?:please )?(?:read|start|begin)(?: me)? (?:chapter|part) (\S+)(?: (?:aloud|out loud|please))?$/.exec(t))) return { name: "read_chapter", args: { chapter: m[1] } };
  if ((m = /^(?:please )?(?:jump|go|skip|take me|move)(?: back| ahead| forward)? to (?:the )?(?:(?:chapter|part) (\S+)|(?:my )?bookmark|(.+))$/.exec(t))) {
    if (m[1]) return { name: "jump_to", args: { chapter: m[1] } };
    if (!m[2]) return { name: "jump_to", args: { chapter: "bookmark" } };
    const where = m[2].replace(/^(?:the )?(?:part|bit|passage|line|place) (?:where|with|that says|about) /, "");
    return { name: "jump_to", args: { passage: where } };
  }
  if (/^(?:please )?(?:resume|continue|keep|carry on|go on)(?: reading| the story| the book)?(?: please)?$/.test(t) || /^(?:resume|continue) reading$/.test(t)) return { name: "resume_reading", args: {} };
  if (/^(?:please )?(?:pause|stop)(?: reading| the story| the book)?(?: please)?$/.test(t)) return { name: "pause_reading", args: {} };
  if ((m = /^(?:please )?(?:read|switch|use|go)(?: to)? (?:in )?(one|two|a single|both) voices?$/.exec(t))) return { name: "set_voice_mode", args: { mode: m[1] === "two" || m[1] === "both" ? "two" : "one" } };
  if ((m = /^(?:please )?(?:use|switch to|make it|i want)(?: the| a)? (male|female|man's|woman's) (?:voice|narrator)(?: for the narrator| to narrate)?$/.exec(t))) return { name: "set_narrator", args: { gender: m[1].startsWith("m") ? "male" : "female" } };
  if ((m = /^(?:please )?(?:read |go |speak |talk )?(?:a (?:bit|little) )?(faster|slower|quicker|speed up|slow down)(?: please)?$/.exec(t))) return { name: "set_speed", args: { speed: /fast|quick|up/.test(m[1]) ? "faster" : "slower" } };
  if ((m = /^(?:please )?(?:set |change )?(?:the )?(?:reading )?speed (?:to )?(\d+(?:\.\d+)?)x?$/.exec(t)) || (m = /^(?:read at |go at |)(\d+(?:\.\d+)?)x(?: speed)?$/.exec(t))) return { name: "set_speed", args: { speed: m[1] } };
  if ((m = /^(?:please )?(?:recap|summarize|summarise|catch me up(?: on)?|remind me(?: of| about)?)(?: the| this)? ?(last chapter|chapter|book|series|story|everything)?(?: so far)?(?: please)?$/.exec(t))) {
    const w = m[1] ?? "";
    return { name: "open_recap", args: { scope: /series|everything/.test(w) ? "series" : /book|story/.test(w) ? "book" : "chapter" } };
  }
  if (/^(?:please )?(?:bookmark|save)(?: this| my)?(?: (?:place|spot|page|position))?(?: please)?$/.test(t) || /^(?:add a |set a |drop a )?bookmark(?: here)?$/.test(t)) return { name: "bookmark", args: {} };
  if (/^(?:please )?(?:show|open|list)(?: me)? the (?:cast|characters|voices)(?: panel)?$/.test(t) || /^who(?:'s| is) in the cast$/.test(t)) return { name: "show_cast", args: {} };
  return null;
}
