// @vitest-environment jsdom
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";
import {
  BILINGUAL_LANGUAGES, loadBilingual, parasToTranslate, readingChunks, remapIndex, saveBilingual, sourceId,
} from "../src/reader/bilingual";
import BookText from "../src/reader/BookText";
import { flattenChunks } from "../src/reader/position";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";

const book = (): Book => ({
  id: "b1", title: "T", author: "A", kind: "novel", sections: [{ id: "s1", title: "I", start_para: 0 }],
  characters: [], narrator: null,
  paragraphs: [
    { idx: 0, section_id: "s1", text: "Alice sat. She was tired.", segments: [], tts_chunks: [
      { id: "p0c0", speaker: "narrator", voice: "v", tts_text: "Alice sat.", char_start: 0, char_end: 10 },
      { id: "p0c1", speaker: "rabbit", voice: "w", tts_text: "She was tired.", char_start: 11, char_end: 25 }] },
    { idx: 1, section_id: "s1", text: "x = 1", kind: "code", segments: [], tts_chunks: [
      { id: "p1c0", speaker: "narrator", voice: "v", tts_text: "Code listing skipped.", char_start: 0, char_end: 5 }] },
    { idx: 2, section_id: "s1", text: "The end.", segments: [], tts_chunks: [
      { id: "p2c0", speaker: "narrator", voice: "v", tts_text: "The end.", char_start: 0, char_end: 8 }] },
  ],
} as unknown as Book);

describe("reading chunks per mode", () => {
  it("off is today's list", () => {
    expect(readingChunks(book(), { mode: "off", lang: "spa" })).toEqual(flattenChunks(book()));
  });

  it("bilingual reads each paragraph, then its translation in the same speaker; code stays English", () => {
    const c = readingChunks(book(), { mode: "bilingual", lang: "spa" });
    expect(c.map((x) => x.id)).toEqual(["p0c0", "p0c1", "p0c0@spa", "p0c1@spa", "p1c0", "p2c0", "p2c0@spa"]);
    expect(c[3]).toMatchObject({ speaker: "rabbit", voice: "w", para: 0, translation: { lang: "spa", of: "p0c1" } });
  });

  it("translated only replaces the original chunks", () => {
    const c = readingChunks(book(), { mode: "translated", lang: "zho" });
    expect(c.map((x) => x.id)).toEqual(["p0c0@zho", "p0c1@zho", "p1c0", "p2c0@zho"]);
  });

  it("a mode change keeps the reader on the same sentence", () => {
    const off = readingChunks(book(), { mode: "off", lang: "spa" });
    const bi = readingChunks(book(), { mode: "bilingual", lang: "spa" });
    const tr = readingChunks(book(), { mode: "translated", lang: "spa" });
    expect(remapIndex(off, bi, 3)).toBe(5);              // p2c0
    expect(remapIndex(bi, tr, 3)).toBe(1);               // p0c1@spa -> p0c1@spa
    expect(remapIndex(tr, off, 1)).toBe(1);              // back to p0c1
    expect(remapIndex(bi, off, 6)).toBe(3);              // p2c0@spa -> p2c0
    expect(sourceId(bi[2])).toBe("p0c0");
  });
});

describe("lazy translation window", () => {
  it("asks only for the paragraphs about to be read, never the whole book", () => {
    const bi = readingChunks(book(), { mode: "bilingual", lang: "spa" });
    expect(parasToTranslate(bi, 0)).toEqual([0]);        // p0's translation is next
    expect(parasToTranslate(bi, 4)).toEqual([2]);        // code paragraph is never translated
    expect(parasToTranslate(readingChunks(book(), { mode: "off", lang: "spa" }), 0)).toEqual([]);
  });
});

describe("remembered per book", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());
  it("defaults to off and keeps each book's choice", () => {
    expect(loadBilingual("b1")).toEqual({ mode: "off", lang: "spa" });
    saveBilingual("b1", { mode: "translated", lang: "jpn" });
    expect(loadBilingual("b1")).toEqual({ mode: "translated", lang: "jpn" });
    expect(loadBilingual("b2").mode).toBe("off");
    localStorage.setItem("talkback:bilingual:b3", "{bad");
    expect(loadBilingual("b3").mode).toBe("off");
    localStorage.setItem("talkback:bilingual:b4", JSON.stringify({ mode: "bilingual", lang: "eng" }));
    expect(loadBilingual("b4")).toEqual({ mode: "off", lang: "spa" }); // English is not a second language
  });
  it("offers the languages of the brief", () => {
    expect(BILINGUAL_LANGUAGES.map((l) => l.code)).toEqual(["spa", "fra", "deu", "ita", "por", "zho", "jpn", "kor"]);
  });
});

describe("BookText", () => {
  const render = (mode: "bilingual" | "translated", index: number, pieces: Record<number, string[]>) => {
    const b = book();
    const chunks = readingChunks(b, { mode, lang: "spa" });
    return renderToStaticMarkup(createElement(BookText, {
      book: b, chunks, index, onPick: () => {}, translation: { mode, lang: "spa", pieces },
    }));
  };
  it("bilingual shows the translation under the paragraph and lights the piece being read", () => {
    const html = render("bilingual", 3, { 0: ["Alicia se sentó.", "Estaba cansada."] });
    expect(html).toContain("Alice sat.");
    expect(html).toMatch(/class="para para-translation"[^>]*lang="es"/);
    expect(html).toMatch(/class="sentence active"[^>]*>Estaba cansada\./);
    expect(html).not.toContain("Translating…");         // p2 is not next: nothing is promised for it yet
    expect(render("bilingual", 4, {})).toContain("Translating…"); // p2 is next and on its way
  });
  it("translated only hides the original text", () => {
    const html = render("translated", 0, { 0: ["Alicia se sentó.", "Estaba cansada."], 2: ["Fin."] });
    expect(html).not.toContain("Alice sat.");
    expect(html).toContain("Alicia se sentó.");
    expect(html).toContain("x = 1");                    // the code listing stays
  });
});

describe("engine sequencing", () => {
  class FakeAudio implements AudioLike {
    currentTime = 0; playbackRate = 1; preservesPitch = false; preload = ""; onended: (() => void) | null = null;
    constructor(public src: string) {}
    async play() {}
    pause() {}
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));

  it("plays the original sentences of a paragraph, then their translation, then goes on", async () => {
    const chunks = readingChunks(book(), { mode: "bilingual", lang: "spa" });
    const fetchTts = vi.fn(async (c: { id: string }): Promise<TtsResult> => ({ audio_url: `/m/${c.id}.mp3`, timestamps: null }));
    const audios: FakeAudio[] = [];
    const engine = new ReaderEngine(chunks, {
      fetchTts, createAudio: (u) => { const a = new FakeAudio(u); audios.push(a); return a; },
    });
    const played: string[] = [];
    engine.on((s) => { if (s.state === "playing" && played[played.length - 1] !== chunks[s.index].id) played.push(chunks[s.index].id); });
    engine.play();
    for (let n = 0; n < 8; n++) {
      await flush(); await flush();
      const cur = engine.currentAudio() as FakeAudio | null;
      cur?.onended?.();
    }
    expect(played).toEqual(["p0c0", "p0c1", "p0c0@spa", "p0c1@spa", "p1c0", "p2c0", "p2c0@spa"]);
    expect(engine.activeWord()).toBeNull();
  });
});

describe("translation API", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("asks for a chunk's translated audio with its language, voice and speed", async () => {
    const { fetchTranslatedTts, fetchTranslation } = await import("../src/api");
    const bodies: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      bodies.push({ url, ...JSON.parse(String(init.body)) });
      return new Response(JSON.stringify({ audio_url: "/media/tts/x.mp3", timestamps: null, cached: false, text: "Hola.", pieces: ["Hola."] }), { status: 200 });
    }));
    await fetchTranslatedTts("b1", "p0c0", "spa", "nora", false, "slow");
    await fetchTranslation("b1", 0, "zho");
    expect(bodies[0]).toMatchObject({ url: "/api/tts/translated", book_id: "b1", chunk_id: "p0c0", language: "spa", voice_override: "nora", speed: "slow" });
    expect(bodies[1]).toMatchObject({ url: "/api/translate", book_id: "b1", para: 0, language: "zho" });
  });
  it("surfaces the server's message", async () => {
    const { fetchTranslation } = await import("../src/api");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Translation needs Gemini" }), { status: 503 })));
    await expect(fetchTranslation("b1", 0, "spa")).rejects.toThrow("Translation needs Gemini");
  });
});

describe("player bar picker", () => {
  it("turns on in bilingual mode, keeps the mode on a language change, and turns off", async () => {
    const { pickLanguage } = await import("../src/reader/bilingual");
    expect(pickLanguage({ mode: "off", lang: "spa" }, "zho")).toEqual({ mode: "bilingual", lang: "zho" });
    expect(pickLanguage({ mode: "translated", lang: "spa" }, "fra")).toEqual({ mode: "translated", lang: "fra" });
    expect(pickLanguage({ mode: "translated", lang: "fra" }, "")).toEqual({ mode: "off", lang: "fra" });
  });
  it("shows the picker and the mode only when a language is on", async () => {
    const PlayerBar = (await import("../src/reader/PlayerBar")).default;
    const base = {
      playing: false, onToggle() {}, onPrev() {}, onNext() {}, speed: 1, speeds: [1], onSpeed() {}, onVoices() {},
      autoResume: true, onAutoResume() {}, para: 0, totalParas: 1, atStart: true, atEnd: false, hasCast: false,
      onRecap() {}, inSeries: false, audioAvailable: true, audioError: null,
    };
    const off = renderToStaticMarkup(createElement(PlayerBar, { ...base, bilingual: { value: { mode: "off", lang: "spa" }, onChange() {} } }));
    expect(off).toContain("Second language");
    expect(off).not.toContain("Translation only");
    const on = renderToStaticMarkup(createElement(PlayerBar, { ...base, bilingual: { value: { mode: "bilingual", lang: "jpn" }, onChange() {} } }));
    expect(on).toContain("Translation only");
    expect(on).toMatch(/<option value="jpn" selected="">Japanese<\/option>/);
  });
});
