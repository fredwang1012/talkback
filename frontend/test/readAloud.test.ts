import { describe, expect, it, vi } from "vitest";
import { createReadAloud, PREFETCH_AHEAD } from "../src/reader/readAloud";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";

function setup() {
  const resolvers: (() => void)[] = [];
  const played: string[] = [];
  const speakers: SentenceSpeaker[] = [];
  const changes: (string | null)[] = [];
  const onError = vi.fn();
  const ra = createReadAloud(() => {
    const sp: SentenceSpeaker = {
      prefetch: vi.fn(),
      play: (t) => { played.push(t); return new Promise<void>((r) => resolvers.push(r)); },
      stop: vi.fn(() => resolvers.splice(0).forEach((r) => r())),
    };
    speakers.push(sp);
    return sp;
  }, (p) => changes.push(p), onError);
  const tick = () => new Promise((r) => setTimeout(r, 0));
  return { ra, played, speakers, changes, onError, tick, finish: () => resolvers.splice(0).forEach((r) => r()) };
}

describe("createReadAloud", () => {
  it("Pause really stops: later parts are not spoken until resumed", async () => {
    const h = setup();
    h.ra.toggle("recap", ["one", "two", "three"]);
    await h.tick();
    expect(h.played).toEqual(["one"]);
    h.ra.toggle("recap", ["one", "two", "three"]); // the Stop button
    await h.tick(); await h.tick();
    expect(h.played).toEqual(["one"]);
    expect(h.ra.playing()).toBeNull();
    expect(h.changes).toEqual(["recap", null]);
  });
  it("plays parts in order and clears itself at the end", async () => {
    const h = setup();
    h.ra.toggle("recap", ["one", "two"]);
    await h.tick(); h.finish(); await h.tick(); h.finish(); await h.tick();
    expect(h.played).toEqual(["one", "two"]);
    expect(h.ra.playing()).toBeNull();
  });
  it("starting the earlier-books summary stops the recap", async () => {
    const h = setup();
    h.ra.toggle("recap", ["one", "two"]);
    await h.tick();
    h.ra.toggle("earlier", ["old"]);
    await h.tick();
    expect(h.speakers[0].stop).toHaveBeenCalled();
    expect(h.ra.playing()).toBe("earlier");
    expect(h.played).toEqual(["one", "old"]);
  });

  it("resumes a paused text from the part it stopped on", async () => {
    const h = setup();
    const parts = ["one", "two", "three"];
    h.ra.toggle("earlier", parts);
    await h.tick(); h.finish(); await h.tick(); // "one" done, "two" playing
    expect(h.played).toEqual(["one", "two"]);
    h.ra.toggle("earlier", parts); // Pause
    await h.tick(); await h.tick();
    expect(h.ra.playing()).toBeNull();
    expect(h.ra.paused("earlier", parts)).toBe(true);
    h.ra.toggle("earlier", parts); // Resume
    await h.tick();
    expect(h.played).toEqual(["one", "two", "two"]);
    expect(h.ra.paused("earlier", parts)).toBe(false);
  });
  it("Start over plays from the first part and different text starts fresh", async () => {
    const h = setup();
    h.ra.toggle("recap", ["a", "b"]);
    await h.tick(); h.finish(); await h.tick();
    h.ra.toggle("recap", ["a", "b"]); // pause on "b"
    await h.tick(); await h.tick();
    h.ra.restart("recap", ["a", "b"]);
    await h.tick();
    expect(h.played.at(-1)).toBe("a");
    h.ra.toggle("recap", ["a", "b"]); // pause on "a"
    await h.tick(); await h.tick();
    expect(h.ra.paused("recap", ["x", "y"])).toBe(false); // the More text is a different text
    h.ra.toggle("recap", ["x", "y"]);
    await h.tick();
    expect(h.played.at(-1)).toBe("x");
  });
  it("prefetches only a few parts ahead, not the whole text", async () => {
    const h = setup();
    const parts = Array.from({ length: 30 }, (_, i) => `p${i}`);
    h.ra.toggle("earlier", parts);
    await h.tick();
    const asked = (h.speakers[0].prefetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(asked).toEqual(parts.slice(1, 1 + PREFETCH_AHEAD));
  });
});

/** A speaker that can hold its place, like the real one (#88). */
function pausable() {
  const resolvers: (() => void)[] = [];
  const played: string[] = [];
  const log: string[] = [];
  const speakers: SentenceSpeaker[] = [];
  const changes: (string | null)[] = [];
  const ra = createReadAloud(() => {
    let paused = false;
    let stopped = false;
    const sp: SentenceSpeaker = {
      prefetch: vi.fn(),
      play: (t) => { played.push(t); return new Promise<void>((r) => resolvers.push(r)); },
      stop: vi.fn(() => { stopped = true; paused = false; log.push("stop"); resolvers.splice(0).forEach((r) => r()); }),
      pause: () => { paused = true; log.push("pause"); },
      resume: () => { log.push("resume"); if (!paused || stopped) return false; paused = false; return true; },
      get paused() { return paused; },
    };
    speakers.push(sp);
    return sp;
  }, (p) => changes.push(p), vi.fn());
  const tick = () => new Promise((r) => setTimeout(r, 0));
  return { ra, played, log, speakers, changes, tick, finish: () => resolvers.splice(0).forEach((r) => r()) };
}

describe("createReadAloud with a speaker that can pause (#88)", () => {
  it("Pause holds the clip and Resume continues it mid-sentence, without rereading the part", async () => {
    const h = pausable();
    const parts = ["one", "two", "three"];
    h.ra.toggle("r", parts);
    await h.tick(); h.finish(); await h.tick(); // "two" is playing
    h.ra.toggle("r", parts); // Pause
    expect(h.log).toEqual(["pause"]);
    expect(h.ra.playing()).toBeNull();
    expect(h.ra.paused("r", parts)).toBe(true);
    h.ra.toggle("r", parts); // Resume
    expect(h.log).toEqual(["pause", "resume"]);
    expect(h.ra.playing()).toBe("r");
    await h.tick();
    expect(h.played).toEqual(["one", "two"]); // "two" was not started again
    h.finish(); await h.tick();
    expect(h.played).toEqual(["one", "two", "three"]);
    expect(h.speakers).toHaveLength(1);
  });

  it("starting another text holds the first; coming back resumes it in place", async () => {
    const h = pausable();
    h.ra.toggle("a", ["a1", "a2"]);
    await h.tick();
    h.ra.toggle("b", ["b1"]);
    await h.tick();
    expect(h.ra.playing()).toBe("b");
    expect(h.ra.paused("a", ["a1", "a2"])).toBe(true);
    h.ra.toggle("a", ["a1", "a2"]);
    expect(h.ra.playing()).toBe("a");
    expect(h.played).toEqual(["a1", "b1"]); // a1 continues, not replayed
  });

  it("falls back to the start of the part when the held clip is gone", async () => {
    const h = pausable();
    h.ra.toggle("r", ["one", "two"]);
    await h.tick();
    h.ra.toggle("r", ["one", "two"]); // Pause
    (h.speakers[0].stop as () => void)(); // the clip was discarded
    h.ra.toggle("r", ["one", "two"]);
    await h.tick();
    expect(h.played).toEqual(["one", "one"]);
    expect(h.ra.playing()).toBe("r");
  });

  it("Start over drops the held clip; reset stops everything for good", async () => {
    const h = pausable();
    h.ra.toggle("r", ["one", "two"]);
    await h.tick(); h.finish(); await h.tick();
    h.ra.toggle("r", ["one", "two"]); // pause on "two"
    h.ra.restart("r", ["one", "two"]);
    await h.tick();
    expect(h.played.at(-1)).toBe("one");
    expect(h.speakers[0].stop).toHaveBeenCalled();
    h.ra.toggle("r", ["one", "two"]); // pause
    h.ra.reset();
    expect(h.speakers[1].stop).toHaveBeenCalled();
    expect(h.ra.paused("r", ["one", "two"])).toBe(false);
  });
});
