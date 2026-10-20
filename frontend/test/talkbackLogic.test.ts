import { beforeEach, describe, expect, it } from "vitest";
import type { Book } from "../src/api";
import { MAX_MESSAGES, addQuestion, appendSentence, finishAnswer, loadChat, resetChatMemory, saveChat } from "../src/talk/chatLog";
import { chapterAt, showSpoilerToggle, speakerChoices, spaceStartsTalk, talkbackStatus, toggleMode } from "../src/reader/talkbackLogic";
import { TalkController, type ReaderControl, type TalkSession } from "../src/talk/TalkController";

const book = {
  kind: "novel", sections: [{ id: "a", title: "One", start_para: 0 }, { id: "b", title: "Two", start_para: 10 }],
  characters: [{ id: "h", name: "H", first_para: 3 }, { id: "s", name: "S", first_para: 30 }],
} as unknown as Book;

describe("talkback logic", () => {
  it("finds the chapter for a paragraph", () => {
    expect(chapterAt(book, 0)).toBe("One");
    expect(chapterAt(book, 9)).toBe("One");
    expect(chapterAt(book, 10)).toBe("Two");
    expect(chapterAt({ sections: [] }, 3)).toBeNull();
  });
  it("lists met characters, everyone with spoilers, nobody for textbooks", () => {
    expect(speakerChoices(book, 5, false).map((c) => c.id)).toEqual(["narrator", "h"]);
    expect(speakerChoices(book, 5, true).map((c) => c.id)).toEqual(["narrator", "h", "s"]);
    const withMinor = { ...book, characters: [...book.characters, { id: "m", name: "M", first_para: 0, main: false }] } as unknown as Book;
    expect(speakerChoices(withMinor, 5, true).map((c) => c.id)).toEqual(["narrator", "h", "s"]); // minor speakers are never offered
    expect(speakerChoices({ ...book, kind: "textbook" } as Book, 5, true)).toEqual([]);
    expect(showSpoilerToggle({ kind: "textbook" })).toBe(false);
    expect(showSpoilerToggle({ kind: "novel" })).toBe(true);
  });
  it("maps controller state to the four visible states", () => {
    expect(talkbackStatus("talk-idle", false)).toBe("idle");
    expect(talkbackStatus("listening", false)).toBe("listening");
    expect(talkbackStatus("retrieving", false)).toBe("thinking");
    expect(talkbackStatus("answering", true)).toBe("speaking");
  });
  it("toggles the mode and only starts hold-to-talk on Space outside fields", () => {
    expect(toggleMode("read")).toBe("talk");
    expect(toggleMode("talk")).toBe("read");
    expect(spaceStartsTalk({ code: "Space", repeat: false, target: { tagName: "DIV" } })).toBe(true);
    expect(spaceStartsTalk({ code: "Space", repeat: false, target: { tagName: "INPUT" } })).toBe(false);
    expect(spaceStartsTalk({ code: "Space", repeat: true, target: { tagName: "DIV" } })).toBe(false);
  });
});

describe("chat log", () => {
  beforeEach(() => resetChatMemory());
  it("builds a question and a streaming answer, then finishes it", () => {
    let m = addQuestion([], "Who?", "h", "typed");
    expect(m.map((x) => x.role)).toEqual(["user", "character"]);
    m = appendSentence(appendSentence(m, "A man."), "Big.");
    expect(m[1].text).toBe("A man. Big.");
    expect(finishAnswer(m)[1].pending).toBe(false);
    expect(finishAnswer(addQuestion([], "x", "h", "voice"))).toHaveLength(1); // an empty answer is dropped
  });
  it("keeps at most 50 messages and is per book", () => {
    let m = [] as ReturnType<typeof addQuestion>;
    for (let i = 0; i < 40; i++) m = addQuestion(m, `q${i}`, "h", "typed");
    expect(m).toHaveLength(MAX_MESSAGES);
    saveChat("a", m);
    expect(loadChat("a")).toHaveLength(MAX_MESSAGES);
    expect(loadChat("b")).toEqual([]);
    saveChat("a", []);
    expect(loadChat("a")).toEqual([]);
  });
});

describe("TalkController in Talkback", () => {
  function make(playing: boolean) {
    const log: string[] = [];
    const reader: ReaderControl = {
      isPlaying: () => playing, currentIndex: () => 5, currentPara: () => 2,
      pause: () => { playing = false; log.push("pause"); }, play: () => log.push("play"), resumeAt: (i) => log.push(`resumeAt:${i}`),
    };
    const session: TalkSession = {
      async ensureOpen() {}, async setVoice() {}, async startMic() {}, async stopMic() {}, commit() {}, respond() {},
      async interrupt() {}, on: () => () => {},
    };
    const sentences: string[] = [];
    const states: string[] = [];
    const c = new TalkController({
      session, reader, retrieve: async () => ({ instructions: "" }), voiceFor: () => "marcus",
      answer: async (_q, _p, _s, onSentence) => { onSentence("Hello there."); },
      speaker: { prefetch() {}, play: async (t) => { sentences.push(t); }, stop() {} },
      onState: (s) => states.push(s),
    });
    return { c, log, sentences, states };
  }
  it("a typed question with reading paused keeps the position and never starts the story", async () => {
    const { c, log, sentences, states } = make(false);
    await c.ask("Who?");
    await new Promise((r) => setTimeout(r, 0));
    expect(sentences).toEqual(["Hello there."]);
    expect(log).not.toContain("play");
    expect(log.some((l) => l.startsWith("resumeAt"))).toBe(false);
    expect(states.at(-1)).toBe("talk-idle");
  });
  it("a typed question while reading pauses it first", async () => {
    const { c, log } = make(true);
    await c.ask("Who?");
    expect(log[0]).toBe("pause");
  });
  it("a short mic tap in Talkback never starts the story", async () => {
    const { c, log, states } = make(false);
    c.setTalkback(true);
    await c.pttDown();
    await c.pttUp();
    expect(log).not.toContain("play");
    expect(states.at(-1)).toBe("talk-idle");
  });
});
