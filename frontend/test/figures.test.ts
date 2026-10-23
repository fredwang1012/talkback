// @vitest-environment jsdom
// Textbook figures in the reader (#40): shown between paragraphs with their caption, caption reading can be turned off.
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book } from "../src/api";

const fetched = vi.hoisted(() => [] as string[]);
vi.mock("../src/api", async (orig) => ({
  ...(await orig<typeof import("../src/api")>()),
  fetchFigure: vi.fn(async (bookId: string, name: string) => { fetched.push(`${bookId}/${name}`); return `blob:${name}`; }),
}));

import BookText from "../src/reader/BookText";
import PlayerBar from "../src/reader/PlayerBar";
import { ReaderEngine, type AudioLike } from "../src/reader/ReaderEngine";
import { figureUrl } from "../src/api";
import { flattenChunks, loadReadCaptions, nextChunk, saveReadCaptions, type FlatChunk } from "../src/reader/position";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const chunk = (id: string, text: string) => ({ id, speaker: "narrator", voice: "marcus", tts_text: text, char_start: 0, char_end: text.length });
const CAPTION = "Figure 1.3: The two parts of an inverted index.";
const book = {
  id: "u-ir-1234abcd", title: "IR", author: "M", kind: "textbook", license: "", attribution: null, characters: [], narrator: null,
  sections: [{ id: "s1", title: "1 Boolean retrieval", start_para: 0 }],
  paragraphs: [
    { idx: 0, section_id: "s1", text: "Before the figure.", segments: [], tts_chunks: [chunk("p0c0", "Before the figure.")] },
    { idx: 1, section_id: "s1", text: CAPTION, segments: [], tts_chunks: [chunk("p1c0", CAPTION)], kind: "figure", figure: "fig-3.png" },
    { idx: 2, section_id: "s1", text: "After the figure.", segments: [], tts_chunks: [chunk("p2c0", "After the figure.")] },
  ],
} as unknown as Book;

describe("figure paragraphs in the chunk list", () => {
  it("marks caption chunks and skips them only when asked, without changing the list", () => {
    const chunks = flattenChunks(book);
    expect(chunks.map((c) => [c.id, !!c.caption])).toEqual([["p0c0", false], ["p1c0", true], ["p2c0", false]]);
    expect(nextChunk(chunks, 0, false)).toBe(1);
    expect(nextChunk(chunks, 0, true)).toBe(2);
    expect(nextChunk(chunks, 2, true)).toBe(-1);
  });

  it("reads captions by default and remembers the reader's choice", () => {
    localStorage.clear();
    expect(loadReadCaptions()).toBe(true);
    saveReadCaptions(false);
    expect(loadReadCaptions()).toBe(false);
    saveReadCaptions(true);
    expect(loadReadCaptions()).toBe(true);
  });

  it("figure URLs are per book and escaped", () => {
    expect(figureUrl("u-ir-1234abcd", "fig-3.png")).toBe("/api/books/u-ir-1234abcd/figures/fig-3.png");
  });
});

describe("ReaderEngine with captions off", () => {
  class FakeAudio implements AudioLike {
    currentTime = 0; playbackRate = 1; preservesPitch = false; preload = ""; onended: (() => void) | null = null;
    constructor(public src: string) {}
    async play() {}
    pause() {}
  }
  const flush = () => new Promise((r) => setTimeout(r, 0));

  function run(skip: boolean) {
    const fetchedTts: string[] = [];
    const audios: FakeAudio[] = [];
    const chunks: FlatChunk[] = flattenChunks(book);
    const engine = new ReaderEngine(chunks, {
      fetchTts: async (c) => { fetchedTts.push(c.id); return { audio_url: `/media/${c.id}.mp3`, timestamps: null }; },
      createAudio: (url) => { const a = new FakeAudio(url); audios.push(a); return a; },
    });
    engine.setSkipCaptions(skip);
    return { engine, fetchedTts, end: (id: string) => audios.find((a) => a.src.endsWith(`${id}.mp3`))!.onended?.() };
  }

  it("goes from the paragraph before the figure straight to the one after it, never fetching the caption", async () => {
    const { engine, fetchedTts, end } = run(true);
    engine.play();
    await flush(); await flush();
    expect(fetchedTts).not.toContain("p1c0");
    end("p0c0");
    await flush();
    expect(engine.snapshot().index).toBe(2);
  });

  it("reads the caption by default", async () => {
    const { engine, end } = run(false);
    engine.play();
    await flush(); await flush();
    end("p0c0");
    await flush();
    expect(engine.snapshot().index).toBe(1);
  });
});

describe("BookText shows figures", () => {
  let root: Root;
  let host: HTMLElement;
  beforeEach(() => { fetched.length = 0; host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
  afterEach(() => { act(() => root.unmount()); host.remove(); });

  it("renders the image (fetched with the book's gate) between paragraphs with its caption as a readable sentence", async () => {
    const picked: number[] = [];
    const chunks = flattenChunks(book);
    await act(async () => { root.render(createElement(BookText, { book, chunks, index: 1, onPick: (i: number) => picked.push(i) })); });
    const fig = host.querySelector("figure.para-figure")!;
    expect(fig.getAttribute("data-para")).toBe("1");
    expect(fetched).toEqual(["u-ir-1234abcd/fig-3.png"]);
    const img = fig.querySelector("img")!;
    expect(img.getAttribute("src")).toBe("blob:fig-3.png");
    expect(img.getAttribute("alt")).toBe(CAPTION);
    const sentence = fig.querySelector("figcaption .sentence")!;
    expect(sentence.textContent).toBe(CAPTION);
    expect(sentence.className).toContain("active"); // the caption lights up while it is read
    act(() => (sentence as HTMLElement).click());
    expect(picked).toEqual([1]);
    const order = [...host.querySelectorAll("[data-para]")].map((e) => e.getAttribute("data-para"));
    expect(order).toEqual(["0", "1", "2"]);
  });

  it("taps to enlarge, and Escape or a click closes the larger view", async () => {
    await act(async () => { root.render(createElement(BookText, { book, chunks: flattenChunks(book), index: 0, onPick: () => {} })); });
    act(() => (host.querySelector(".figure-img") as HTMLElement).click());
    expect(host.querySelector(".figure-zoom img")?.getAttribute("src")).toBe("blob:fig-3.png");
    act(() => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); });
    expect(host.querySelector(".figure-zoom")).toBeNull();
    act(() => (host.querySelector(".figure-img") as HTMLElement).click());
    act(() => (host.querySelector(".figure-zoom") as HTMLElement).click());
    expect(host.querySelector(".figure-zoom")).toBeNull();
  });

  it("the player offers 'Read captions' only for a book with figures", () => {
    const props = {
      playing: false, onToggle: () => {}, onPrev: () => {}, onNext: () => {}, speed: 1, speeds: [1], onSpeed: () => {},
      onVoices: () => {}, autoResume: true, onAutoResume: () => {}, para: 0, totalParas: 3, atStart: true, atEnd: false,
      hasCast: false, onRecap: () => {}, inSeries: false, audioAvailable: true, audioError: null,
    };
    act(() => root.render(createElement(PlayerBar, props)));
    expect(host.textContent).not.toContain("Read captions");
    const onReadCaptions = vi.fn();
    act(() => root.render(createElement(PlayerBar, { ...props, hasFigures: true, readCaptions: true, onReadCaptions })));
    const box = [...host.querySelectorAll("label")].find((l) => l.textContent?.includes("Read captions"))!.querySelector("input")!;
    expect(box.checked).toBe(true);
    act(() => box.click());
    expect(onReadCaptions).toHaveBeenCalledWith(false);
  });
});
