import { afterEach, describe, expect, it, vi } from "vitest";
import { animateFace, saveCast, type AnimatedFace, type Book, type Cast, type Face } from "../src/api";
import {
  castFace, castVoice, createCastSaver, genderRows, migrateOldOverrides, narratorGender, radioKeyTarget, readingFaceId,
  runAnimate, slotVoice, speakerGender, talkFaceId, talkVoice, voiceMode, withAssignment, withGender, withSlot, withVoiceMode,
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
/** A cast saved by an older version in the removed Full cast mode (#93): it now reads in two voices. */
const blank = (): Cast => ({
  main: ["harry", "hermione"],
  narrator: { voice: null, face: null },
  characters: { harry: { voice: null, face: null }, hermione: { voice: null, face: null }, minor: { voice: null, face: null } },
  mode: "cast",
});
/** What the server sends for a novel in the default two-voice mode. */
const two = (): Cast => ({
  ...blank(), mode: "two", male: { voice: null, face: null }, female: { voice: null, face: null }, narrator_gender: null,
  genders: {}, auto_genders: { harry: "male", hermione: "female", minor: null },
  defaults: { male: "marcus", female: "chloe", narrator_gender: "male" },
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

describe("two voices (novels, the default)", () => {
  it("every speaker reads in the male or female voice by gender; the narrator in the server default", () => {
    const c = two();
    expect(voiceMode(c)).toBe("two");
    expect(["narrator", "harry", "hermione", "minor"].map((id) => castVoice(novel, c, id))).toEqual(["marcus", "marcus", "chloe", "marcus"]);
    expect(speakerGender(c, "minor_f")).toBe("female"); // the generic stand-ins keep their gender
  });

  it("the reader picks both voices, who narrates and a character's gender", () => {
    let c = withSlot(withSlot(two(), "male", { voice: "oliver" }), "female", { voice: "nora", face: "f-f" });
    c = withGender({ ...c, narrator_gender: "female" }, "harry", "female");
    expect(narratorGender(c)).toBe("female");
    expect(["narrator", "harry", "hermione", "minor"].map((id) => castVoice(novel, c, id))).toEqual(["nora", "nora", "nora", "nora"]);
    expect(readingFaceId(novel, c, "harry")).toBe("f-f");
    expect(slotVoice(c, "male")).toBe("oliver");
    expect(withGender(c, "harry", null).genders).toEqual({});
  });

  it("Talkback answers in the character's gendered voice and that voice's face", () => {
    const c = withSlot(two(), "female", { face: "f-f" });
    expect(talkVoice(novel, c, { voice: null, face: null }, "hermione")).toBe("chloe");
    expect(talkFaceId(novel, c, { voice: null, face: null }, "hermione", "chloe", [])).toBe("f-f");
    expect(talkFaceId(novel, c, { voice: null, face: null }, "harry", "marcus", [])).toBeNull();
  });

  it("lists the characters (not the stand-ins) with their inferred and overridden gender", () => {
    const b = { ...novel, characters: [...novel.characters, { id: "minor_m", name: "Other", voice: "oliver" }] } as unknown as Book;
    const rows = genderRows(b, withGender(two(), "minor", "female"));
    expect(rows.map((r) => [r.id, r.auto, r.override])).toEqual([["harry", "male", null], ["hermione", "female", null], ["minor", null, "female"]]);
  });

  it("an old Full cast (or unknown) setting reads in two voices; per-character voices and faces are ignored (#93)", () => {
    let cast = withAssignment({ ...two(), mode: "cast" }, "harry", { face: "f-harry", voice: "oliver" });
    cast = withSlot(cast, "female", { face: "f-f" });
    expect(voiceMode(cast)).toBe("two");
    expect(voiceMode({ ...cast, mode: undefined })).toBe("two");
    expect(voiceMode({ ...cast, mode: "one_voice" as never })).toBe("two");
    expect(castVoice(novel, cast, "harry")).toBe("marcus");
    expect(readingFaceId(novel, cast, "harry")).toBeNull();
    expect(readingFaceId(novel, cast, "hermione")).toBe("f-f");
  });

  it("one voice: every line, dialogue included, reads in the narrator's voice and face (#93)", () => {
    let c = withSlot(withSlot(two(), "male", { voice: "oliver", face: "f-m" }), "female", { voice: "nora", face: "f-f" });
    c = withVoiceMode(c, "one");
    expect(voiceMode(c)).toBe("one");
    expect(["narrator", "harry", "hermione", "minor", "minor_f"].map((id) => castVoice(novel, c, id))).toEqual(["oliver", "oliver", "oliver", "oliver", "oliver"]);
    expect(castFace(novel, c, "hermione")).toBe("f-m");
    expect(talkVoice(novel, c, { voice: null, face: null }, "hermione")).toBe("oliver");
    c = { ...c, narrator_gender: "female" };
    expect(["narrator", "harry"].map((id) => castVoice(novel, c, id))).toEqual(["nora", "nora"]);
    expect(withVoiceMode(c, "two").mode).toBe("two");
    expect(castVoice(novel, withVoiceMode(c, "two"), "harry")).toBe("oliver");
  });

  it("the reader engine sends the resolved voice for every chunk", async () => {
    const c = two();
    const sent: (string | undefined)[] = [];
    const engine = new ReaderEngine(chunks, {
      fetchTts: async (_c, voice): Promise<TtsResult> => { sent.push(voice); return { audio_url: "/a.mp3", timestamps: null }; },
      createAudio: (u) => new FakeAudio(u),
    });
    engine.setVoiceResolver((chunk) => castVoice(novel, c, chunk.speaker));
    engine.play();
    await new Promise((r) => setTimeout(r, 0));
    engine.dispose();
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.every((v) => v === "marcus")).toBe(true); // narrator and Harry: both the male voice
  });

  it("arrow keys move round a radio group; Home / End jump", () => {
    expect(radioKeyTarget("ArrowRight", 2, 3)).toBe(0);
    expect(radioKeyTarget("ArrowLeft", 0, 3)).toBe(2);
    expect(radioKeyTarget("ArrowDown", 0, 3)).toBe(1);
    expect(radioKeyTarget("End", 0, 3)).toBe(2);
    expect(radioKeyTarget("Home", 2, 3)).toBe(0);
    expect(radioKeyTarget("a", 0, 3)).toBeNull();
  });
});

describe("textbooks", () => {
  it("have one voice: the narrator's, whatever the mode", () => {
    const cast = { ...two(), main: ["harry"], narrator: { voice: "eleanor", face: "f-n" } };
    expect(castVoice(textbook, withVoiceMode(cast, "one"), "narrator")).toBe("eleanor");
    expect(castVoice(textbook, cast, "narrator")).toBe("eleanor");
    expect(readingFaceId(textbook, cast, "harry")).toBe("f-n");
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
  const cast = withSlot(two(), "male", { voice: "oliver", face: "f-h" });

  it("Auto resolves to the voice of the character's gender", () => {
    const auto = { voice: null, face: null };
    expect(talkVoice(novel, cast, auto, "harry")).toBe("oliver");
    expect(talkVoice(novel, cast, auto, "hermione")).toBe("chloe");
    expect(talkVoice(novel, cast, auto, "narrator")).toBe("oliver");
  });

  it("Auto avatar is the cast face; an explicit voice or face overrides", () => {
    expect(talkFaceId(novel, cast, { voice: null, face: null }, "harry", "oliver", faces)).toBe("f-h");
    expect(talkVoice(novel, cast, { voice: "chloe", face: null }, "harry")).toBe("chloe");
    expect(talkFaceId(novel, cast, { voice: null, face: "f-clone" }, "harry", "oliver", faces)).toBe("f-clone");
    expect(talkFaceId(novel, cast, { voice: null, face: "none" }, "harry", "oliver", faces)).toBeNull();
  });

  it("Auto falls back to the face bound to a cloned voice, and to no face", () => {
    expect(talkFaceId(novel, two(), { voice: "vc_me", face: null }, "hermione", "vc_me", faces)).toBe("f-clone");
    expect(talkFaceId(novel, two(), { voice: null, face: null }, "hermione", "chloe", faces)).toBeNull();
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
