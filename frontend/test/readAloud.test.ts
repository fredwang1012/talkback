import { describe, expect, it, vi } from "vitest";
import { createReadAloud } from "../src/reader/readAloud";
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
  it("Stop really stops: later parts are never spoken", async () => {
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
});
