import { describe, expect, it, vi } from "vitest";
import { RingPlayer } from "../src/audio/playback";
import { TalkClient, type MicLike, type PlayerLike } from "../src/talk/TalkClient";

function fakeCtx() {
  const ctx = {
    state: "running" as "running" | "suspended",
    suspend: vi.fn(async () => { ctx.state = "suspended"; }),
    resume: vi.fn(async () => { ctx.state = "running"; }),
    close: vi.fn(async () => {}),
  };
  return ctx;
}

describe("RingPlayer pause / resume (#90 Realtime)", () => {
  function player() {
    const p = new RingPlayer();
    const ctx = fakeCtx();
    const posted: unknown[] = [];
    const node = { port: { postMessage: (m: { type: string }) => { posted.push(m.type); if (m.type === "flush") queueMicrotask(() => (p as unknown as { pendingFlush: ((ms: number) => void)[] }).pendingFlush.shift()?.(100)); } } };
    Object.assign(p as object, { ctx, node, initPromise: Promise.resolve() });
    return { p, ctx, posted };
  }

  it("suspends the context, and new audio does not wake it while paused", async () => {
    const { p, ctx, posted } = player();
    await p.pausePlayback();
    expect(ctx.suspend).toHaveBeenCalled();
    p.enqueueBase64(btoa("\u0001\u0000"));
    expect(ctx.resume).not.toHaveBeenCalled(); // the rest of the reply waits in the ring buffer
    expect(posted).toEqual(["samples"]);
    await p.resumePlayback();
    expect(ctx.state).toBe("running");
  });

  it("a flush (barge-in / Stop) while paused wakes the worklet so it can answer", async () => {
    const { p, ctx } = player();
    await p.pausePlayback();
    const ms = await p.flush();
    expect(ctx.resume).toHaveBeenCalled();
    expect(ms).toBe(100);
  });
});

describe("TalkClient pause / resume", () => {
  it("delegates to the player", async () => {
    const calls: string[] = [];
    const player = {
      resume: async () => {}, enqueueBase64: () => {}, flush: async () => 0, reset: () => {}, close: async () => {},
      pausePlayback: async () => { calls.push("pause"); }, resumePlayback: async () => { calls.push("resume"); },
    } as PlayerLike;
    const mic = { start: async () => {}, stop: async () => {} } as unknown as MicLike;
    const c = new TalkClient({ tokenProvider: async () => ({ value: "k" }) as never, createPlayer: () => player, createMic: () => mic });
    await c.pausePlayback();
    await c.resumePlayback();
    expect(calls).toEqual(["pause", "resume"]);
  });
});
