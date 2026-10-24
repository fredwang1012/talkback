import { describe, expect, it } from "vitest";
import { createStatusGate, talkbackStatus, type TalkbackStatus } from "../src/reader/talkbackLogic";

/** Manual timers: `tick(ms)` runs what is due. */
function clock() {
  let now = 0;
  let jobs: { at: number; fn: () => void; id: number }[] = [];
  let seq = 0;
  return {
    timers: {
      set: (fn: () => void, ms: number) => { const id = ++seq; jobs.push({ at: now + ms, fn, id }); return id; },
      clear: (h: unknown) => { jobs = jobs.filter((j) => j.id !== h); },
    },
    tick(ms: number) { now += ms; const due = jobs.filter((j) => j.at <= now); jobs = jobs.filter((j) => j.at > now); due.forEach((j) => j.fn()); },
  };
}

function gate(holdMs = 600) {
  const c = clock();
  const shown: TalkbackStatus[] = [];
  const g = createStatusGate((s) => shown.push(s), holdMs, c.timers);
  const feed = (state: Parameters<typeof talkbackStatus>[0], speaking: boolean) => g.update(talkbackStatus(state, speaking), state === "answering");
  return { g, c, shown, feed };
}

describe("Talkback status line (#81)", () => {
  it("stays on Speaking between the sentences of an answer", () => {
    const { c, shown, feed } = gate();
    feed("retrieving", false);
    feed("answering", false);
    feed("answering", true);   // sentence 1 plays
    feed("answering", false);  // gap: the next sentence is still being synthesised
    c.tick(2000);
    feed("answering", true);   // sentence 2
    feed("answering", false);
    expect(shown).toEqual(["thinking", "speaking"]);
  });

  it("does not drop to Ready for a short gap (a replayed answer, state stays idle)", () => {
    const { c, shown, feed } = gate();
    feed("talk-idle", false);
    feed("talk-idle", true);
    feed("talk-idle", false);
    c.tick(300);
    feed("talk-idle", true);
    feed("talk-idle", false);
    c.tick(599);
    expect(shown).toEqual(["idle", "speaking"]);
    c.tick(1);   // a real end: Ready once
    expect(shown).toEqual(["idle", "speaking", "idle"]);
  });

  it("the end of an answer shows Ready after the hold, and listening is immediate", () => {
    const { c, shown, feed } = gate();
    feed("answering", true);
    feed("talk-idle", false);
    expect(shown).toEqual(["speaking"]);
    c.tick(600);
    expect(shown.at(-1)).toBe("idle");
    feed("listening", false);
    expect(shown.at(-1)).toBe("listening");
  });

  it("a barge-in while speaking shows Listening at once", () => {
    const { shown, feed } = gate();
    feed("answering", true);
    feed("listening", false);
    expect(shown).toEqual(["speaking", "listening"]);
  });

  it("reset shows the new value at once and drops a pending change", () => {
    const { g, c, shown, feed } = gate();
    feed("answering", true);
    feed("talk-idle", false);
    g.reset("idle");
    c.tick(1000);
    expect(shown).toEqual(["speaking", "idle"]);
  });
});
