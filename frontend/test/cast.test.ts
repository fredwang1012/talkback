import { afterEach, describe, expect, it, vi } from "vitest";
import { animateFace, saveCast, type AnimatedFace, type Book, type Cast, type Face } from "../src/api";
import {
  castRows, createCastSaver, effectiveMode, migrateOldOverrides, oneVoiceOverride, readingFaceId, runAnimate,
  talkFaceId, talkVoice, voiceSummary, withAssignment, type ReadingStyle,
} from "../src/reader/castLogic";
import { ReaderEngine, type AudioLike, type TtsResult } from "../src/reader/ReaderEngine";
import type { FlatChunk } from "../src/reader/position";

afterEach(() => vi.unstubAllGlobals());

const novel = {
  id: "hp1", kind: "novel", series: "hp",
  narrator: { id: "narrator", voice: "marcus", is_character: null, portrait: null },
  characters: [
    { id: "harry", name: "Harry", voice: "jake" },
    { id: "hermione", name: "Hermione", voice: "nora" },
    { id: "minor", name: "Minor", voice: "chloe" },
  ],
} as unknown as Book;
const textbook = { ...novel, id: "tb", kind: "textbook", series: null, characters: [] } as unknown as Book;
const blank = (): Cast => ({
  main: ["harry", "hermione"],
  narrator: { voice: null, face: null },
  characters: { harry: { voice: null, face: null }, hermione: { voice: null, face: null }, minor: { voice: null, face: null } },
});

// ---------- one voice vs full cast ----------

class FakeAudio implements AudioLike {
  currentTime = 0; playbackRate = 1; preservesPitch = false; preload = "";
  onended: (() => void) | null = null;
  constructor(public src: string) {}
  async play() { /* nothing */ }
  pause() { /* nothing */ }
}
const chunks: FlatChunk[] = Array.from({ length: 4 }, (_, i) => ({
  id: `c${i}`, speaker: i % 2 ? "harry" : "narrator", voice: i % 2 ? "jake" : "marcus", tts_text: `t${i}`, char_start: 0, char_end: 5, para: i,
}));
async function sentVoices(override: string | null) {
  const sent: (string | undefined)[] = [];
  const engine = new ReaderEngine(chunks, {
    fetchTts: async (_c, voice): Promise<TtsResult> => { sent.push(voice); return { audio_url: "/a.mp3", timestamps: null }; },
    createAudio: (u) => new FakeAudio(u),
  });
  engine.setNarratorOnly(override);
  engine.play();
  await new Promise((r) => setTimeout(r, 0));
  engine.dispose();
  return sent;
}

describe("reading style", () => {
  const one: ReadingStyle = { mode: "one", voice: "oliver", face: null };
  const full: ReadingStyle = { mode: "cast", voice: "oliver", face: null };

  it("One voice sends voice_override for every chunk", async () => {
    const override = oneVoiceOverride(novel, blank(), one);
    expect(override).toBe("oliver");
    const sent = await sentVoices(override);
    expect(sent).toHaveLength(4);
    expect(sent.every((v) => v === "oliver")).toBe(true);
  });

  it("One voice without a pick uses the narrator's cast/book voice", () => {
    expect(oneVoiceOverride(novel, blank(), { ...one, voice: null })).toBe("marcus");
    const c = withAssignment(blank(), "narrator", { voice: "eleanor" });
    expect(oneVoiceOverride(novel, c, { ...one, voice: null })).toBe("eleanor");
  });

  it("Full cast sends no voice_override, so the server cast decides", async () => {
    const override = oneVoiceOverride(novel, blank(), full);
    expect(override).toBeNull();
    const sent = await sentVoices(override);
    expect(sent).toHaveLength(4);
    expect(sent.every((v) => v === undefined)).toBe(true);
  });

  it("shows the one-voice face for everything, and each speaker's cast face in Full cast", () => {
    let cast = withAssignment(blank(), "harry", { face: "f-harry" });
    cast = withAssignment(cast, "narrator", { face: "f-nar" });
    expect(readingFaceId(novel, cast, { ...one, face: "f-one" }, "harry")).toBe("f-one");
    expect(readingFaceId(novel, cast, full, "harry")).toBe("f-harry");
    expect(readingFaceId(novel, cast, full, "narrator")).toBe("f-nar");
    expect(readingFaceId(novel, cast, full, "hermione")).toBeNull();
  });
});

describe("textbooks", () => {
  it("show only the narrator row and ignore a stored One voice mode", () => {
    const cast = { ...blank(), main: ["harry"] };
    expect(castRows(textbook, cast)).toEqual([{ id: "narrator", name: "Narrator" }]);
    expect(effectiveMode(textbook, { mode: "one", voice: "oliver", face: null })).toBe("cast");
    expect(oneVoiceOverride(textbook, cast, { mode: "one", voice: "oliver", face: null })).toBeNull();
  });

  it("novels list the narrator and all main characters, no spoiler gating", () => {
    expect(castRows(novel, blank()).map((r) => r.id)).toEqual(["narrator", "harry", "hermione"]);
  });
});

// ---------- saving the cast ----------

describe("saving the cast", () => {
  it("changes one speaker and keeps the rest of the full cast in the payload", () => {
    const next = withAssignment(withAssignment(blank(), "hermione", { voice: "eleanor" }), "hermione", { face: "f1" });
    expect(next.characters.hermione).toEqual({ voice: "eleanor", face: "f1" });
    expect(next.characters.harry).toEqual({ voice: null, face: null });
    expect(next.main).toEqual(["harry", "hermione"]);
  });

  it("PUTs the whole cast as JSON with the auth header", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => new Response(JSON.stringify(blank()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const cast = withAssignment(blank(), "narrator", { voice: "nora" });
    await saveCast("hp1", cast);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/books/hp1/cast");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual(cast);
  });

  it("sends one request at a time and only the newest state after a burst", async () => {
    const calls: Cast[] = [];
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const save = vi.fn(async (_id: string, c: Cast) => { calls.push(c); if (calls.length === 1) await gate; return c; });
    const saved = vi.fn();
    const push = createCastSaver("hp1", saved, vi.fn(), save);
    const a = withAssignment(blank(), "narrator", { voice: "nora" });
    const b = withAssignment(a, "narrator", { voice: "jake" });
    const c = withAssignment(b, "narrator", { voice: "chloe" });
    const first = push(a);
    void push(b);
    const last = push(c);
    release();
    await Promise.all([first, last]);
    expect(calls).toEqual([a, c]);
    expect(saved).toHaveBeenLastCalledWith(c);
  });

  it("flush waits for the queued save", async () => {
    const save = vi.fn(async (_id: string, c: Cast) => { await new Promise((r) => setTimeout(r, 5)); return c; });
    const push = createCastSaver("hp1", vi.fn(), vi.fn(), save);
    void push(blank());
    void push(withAssignment(blank(), "narrator", { voice: "nora" }));
    await push.flush();
    expect(save).toHaveBeenCalledTimes(2);
  });

  it("reports a failed save", async () => {
    const onError = vi.fn();
    await createCastSaver("hp1", vi.fn(), onError, async () => { throw new Error("nope"); })(blank());
    expect(onError).toHaveBeenCalledWith("nope");
  });
});

// ---------- Talk mode ----------

describe("talk voice and avatar", () => {
  const faces: Face[] = [
    { id: "f-h", name: "H", image: "/h.png", voice: null, talk: null, idle: null },
    { id: "f-clone", name: "Me", image: "/me.png", voice: "vc_me", talk: null, idle: null },
  ];
  const cast = withAssignment(withAssignment(blank(), "harry", { voice: "oliver" }), "harry", { face: "f-h" });

  it("Auto resolves to the character's cast voice, else the book default", () => {
    const auto = { voice: null, face: null };
    expect(talkVoice(novel, cast, auto, "harry")).toBe("oliver");
    expect(talkVoice(novel, cast, auto, "hermione")).toBe("nora");
    expect(talkVoice(novel, cast, auto, "narrator")).toBe("marcus");
  });

  it("Auto avatar is the cast face; an explicit voice or face overrides", () => {
    expect(talkFaceId(cast, { voice: null, face: null }, "harry", "oliver", faces)).toBe("f-h");
    expect(talkVoice(novel, cast, { voice: "chloe", face: null }, "harry")).toBe("chloe");
    expect(talkFaceId(cast, { voice: null, face: "f-clone" }, "harry", "oliver", faces)).toBe("f-clone");
    expect(talkFaceId(cast, { voice: null, face: "none" }, "harry", "oliver", faces)).toBeNull();
  });

  it("Auto falls back to the face bound to a cloned voice, and to no face", () => {
    expect(talkFaceId(blank(), { voice: "vc_me", face: null }, "hermione", "vc_me", faces)).toBe("f-clone");
    expect(talkFaceId(blank(), { voice: null, face: null }, "hermione", "nora", faces)).toBeNull();
  });
});

// ---------- animate ----------

describe("animate button", () => {
  const done = { id: "f1", name: "A", image: "/a.png", voice: null, talk: "/t.mp4", idle: "/i.mp4", animated: true, approx_cost_usd: 0.05 } as AnimatedFace;
  const cb = () => ({ onStart: vi.fn(), onEnd: vi.fn(), onError: vi.fn() });

  it("marks the face running, then clears it and returns the result", async () => {
    const c = cb();
    const out = await runAnimate("f1", new Set(), c, async () => done);
    expect(out).toBe(done);
    expect(c.onStart).toHaveBeenCalledWith("f1");
    expect(c.onEnd).toHaveBeenCalledWith("f1");
    expect(c.onError).not.toHaveBeenCalled();
  });

  it("does nothing while that face is already animating (button disabled)", async () => {
    const c = cb();
    const animate = vi.fn(async () => done);
    expect(await runAnimate("f1", new Set(["f1"]), c, animate)).toBeNull();
    expect(animate).not.toHaveBeenCalled();
    expect(c.onStart).not.toHaveBeenCalled();
  });

  it("reports an error and re-enables the button", async () => {
    const c = cb();
    expect(await runAnimate("f1", new Set(), c, async () => { throw new Error("boom"); })).toBeNull();
    expect(c.onError).toHaveBeenCalledWith("f1", "boom");
    expect(c.onEnd).toHaveBeenCalledWith("f1");
  });

  it("turns 402 and 429 into readable messages", async () => {
    for (const [status, text] of [[402, "out of credit"], [429, "Too many"]] as const) {
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "raw" }), { status })));
      await expect(animateFace("f1")).rejects.toThrow(text);
    }
  });

  it("waits longer than the default 15 s", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_u: string, init?: RequestInit) => new Promise<Response>((res, rej) => {
      init?.signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
      setTimeout(() => res(new Response(JSON.stringify(done), { status: 200 })), 90_000);
    })));
    const p = animateFace("f1");
    await vi.advanceTimersByTimeAsync(91_000);
    expect((await p).animated).toBe(true);
    vi.useRealTimers();
  });
});

// ---------- migration ----------

describe("migration of the old series-wide overrides", () => {
  const mkStore = (initial: Record<string, string>) => {
    const data = { ...initial };
    return { data, getItem: (k: string) => data[k] ?? null, removeItem: (k: string) => { delete data[k]; } };
  };
  const KEY = "talkback:voices:hp";

  it("writes the old overrides into a null cast once, then deletes the key", async () => {
    const store = mkStore({ [KEY]: JSON.stringify({ narrator: "eleanor", harry: "oliver", stranger: "jake" }) });
    const save = vi.fn(async (_id: string, c: Cast) => c);
    const out = await migrateOldOverrides(novel, blank(), save, store);
    expect(save).toHaveBeenCalledTimes(1);
    const sent = save.mock.calls[0][1];
    expect(sent.narrator.voice).toBe("eleanor");
    expect(sent.characters.harry.voice).toBe("oliver");
    expect(sent.characters).not.toHaveProperty("stranger");
    expect(out).toEqual(sent);
    expect(store.data[KEY]).toBeUndefined();
    // second load: key gone, nothing is written
    expect(await migrateOldOverrides(novel, out as Cast, save, store)).toBeNull();
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("never overwrites a voice already set on the server", async () => {
    const store = mkStore({ [KEY]: JSON.stringify({ harry: "oliver" }) });
    const save = vi.fn(async (_id: string, c: Cast) => c);
    const out = await migrateOldOverrides(novel, withAssignment(blank(), "harry", { voice: "chloe" }), save, store);
    expect(save).not.toHaveBeenCalled();
    expect(out).toBeNull();
    expect(store.data[KEY]).toBeUndefined();
  });

  it("keeps the old key when the save fails, so it retries next load", async () => {
    const store = mkStore({ [KEY]: JSON.stringify({ harry: "oliver" }) });
    expect(await migrateOldOverrides(novel, blank(), async () => { throw new Error("down"); }, store)).toBeNull();
    expect(store.data[KEY]).toBeDefined();
  });

  it("copes with garbage and with no storage", async () => {
    const save = vi.fn(async (_id: string, c: Cast) => c);
    expect(await migrateOldOverrides(novel, blank(), save, mkStore({ [KEY]: "{not json" }))).toBeNull();
    expect(await migrateOldOverrides(novel, blank(), save, null)).toBeNull();
    expect(save).not.toHaveBeenCalled();
  });
});

describe("voice summary (#69)", () => {
  const b = { ...novel, characters: [
    { id: "dobby", name: "Dobby", voice: "jake", voice_tags: "<|prosody:pitch_high|>" },
    { id: "baked", name: "Baked", voice: "voice_x", voice_tags: "", base_voice: "jake", base_voice_tags: "<|prosody:pitch_high|>" },
  ] } as unknown as Book;
  const c = (): Cast => ({ main: [], narrator: { voice: null, face: null }, characters: {} });

  it("names the preset and its pitch/speed, the cast override first", () => {
    expect(voiceSummary(b, c(), "dobby")).toBe("jake, higher pitch");
    const o = withAssignment(c(), "dobby", { tags: "<|prosody:speed_slow|>", voice: "nora" });
    expect(voiceSummary(b, o, "dobby")).toBe("nora, slower");
    expect(voiceSummary(b, c(), "narrator")).toBe("marcus");
  });
  it("says when the voice is a baked custom voice", () => {
    expect(voiceSummary(b, c(), "baked")).toBe("custom voice (baked from jake, higher pitch)");
  });
});
