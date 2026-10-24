import { describe, expect, it, vi } from "vitest";
import { createSentenceSpeaker, type AudioLike } from "../src/talk/sentencePlayer";

function fakeAudio() {
  const clips: (AudioLike & { url: string; paused: boolean })[] = [];
  const create = (url: string) => {
    const a = { url, paused: false, onended: null, onerror: null, play: () => Promise.resolve(), pause() { a.paused = true; } } as AudioLike & { url: string; paused: boolean };
    clips.push(a);
    return a;
  };
  return { clips, create };
}

describe("createSentenceSpeaker", () => {
  it("resolves on ended and reuses a prefetch", async () => {
    const speak = vi.fn(async (t: string) => ({ audio_url: `/m/${t}.wav` }));
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(speak, create);
    s.prefetch("a");
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onended?.();
    await done;
    expect(speak).toHaveBeenCalledTimes(1);
    expect(clips[0].url).toBe("/m/a.wav");
  });

  it("rejects on audio error", async () => {
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), create);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onerror?.();
    await expect(done).rejects.toThrow();
  });

  it("stop pauses the clip and resolves the pending play", async () => {
    const { clips, create } = fakeAudio();
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), create);
    const done = s.play("a");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    s.stop();
    await done;
    expect(clips[0].paused).toBe(true);
  });

  it("stop while still synthesising means the clip never starts", async () => {
    const { clips, create } = fakeAudio();
    let release!: () => void;
    const s = createSentenceSpeaker(() => new Promise((r) => { release = () => r({ audio_url: "/x" }); }), create);
    const done = s.play("a");
    s.stop();
    release();
    await done;
    expect(clips).toHaveLength(0);
  });
});

describe("speed and face signals", () => {
  const evAudio = () => {
    const l: Record<string, (() => void)[]> = {};
    const a = {
      onended: null as (() => void) | null, onerror: null, paused: false,
      play: () => Promise.resolve(), pause() { a.paused = true; },
      addEventListener(t: string, f: () => void) { (l[t] ||= []).push(f); },
      removeEventListener(t: string, f: () => void) { l[t] = (l[t] || []).filter((x) => x !== f); },
      fire(t: string) { (l[t] || []).forEach((f) => f()); },
    };
    return a;
  };

  it("preloads the audio of a prefetched sentence before it is played", async () => {
    const created: string[] = [];
    const s = createSentenceSpeaker(async (t) => ({ audio_url: `/m/${t}` }), (u) => { created.push(u); return evAudio(); });
    s.prefetch("a", 1);
    s.prefetch("b", 2);
    await vi.waitFor(() => expect(created).toEqual(["/m/a", "/m/b"]));
  });

  it("opens the lip-sync stream only for rank 0 prefetches", async () => {
    const open = vi.fn(async () => ({ url: "u", release() {} }));
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x", openVideo: open }), () => evAudio());
    s.prefetch("a", 0);
    s.prefetch("b", 1);
    await vi.waitFor(() => expect(open).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 5));
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("reports playing from media events and logs stage timings", async () => {
    const a = evAudio();
    const playing: boolean[] = [];
    const stages: string[] = [];
    const sync = { join() {}, leave() {} };
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), () => a, {
      sync, onPlaying: (v) => playing.push(v), onTiming: (e) => stages.push(e.stage),
    });
    const done = s.play("hi");
    await vi.waitFor(() => expect(stages).toContain("tts"));
    a.fire("playing");
    a.fire("pause");
    a.fire("playing");
    a.onended?.();
    await done;
    expect(playing).toEqual([true, false, true, false]);
    expect(stages).toEqual(["tts", "audio-start"]);
  });
});

describe("lip-sync start gate", () => {
  const gated = (opts: { startWaitMs?: number; canShow?: boolean } = {}) => {
    let plays = 0;
    const a = { onended: null as (() => void) | null, onerror: null, play: () => { plays++; return Promise.resolve(); }, pause() {} };
    let openGate!: () => void;
    const joins: boolean[] = [];
    const sync = {
      canShow: opts.canShow ?? true,
      join: (_a: unknown, _c: unknown, late?: boolean) => { joins.push(!!late); return new Promise<void>((r) => { openGate = r; }); },
      leave: vi.fn(),
    };
    let deliver!: (v: { url: string; release: () => void }) => void;
    const release = vi.fn();
    const openVideo = () => new Promise<{ url: string; release: () => void }>((r) => { deliver = r; });
    const stages: string[] = [];
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x", openVideo }), () => a,
      { sync, onTiming: (e) => stages.push(e.stage), startWaitMs: opts.startWaitMs ?? 5000 });
    return { s, a, sync, joins, stages, plays: () => plays, open: () => openGate(), deliver: () => deliver({ url: "u", release }), release };
  };

  it("the voice waits for its clip to be ready, then starts", async () => {
    const g = gated();
    const done = g.s.play("hi");
    await new Promise((r) => setTimeout(r, 5));
    expect(g.plays()).toBe(0); // no first fragment yet
    g.deliver();
    await vi.waitFor(() => expect(g.joins).toEqual([false]));
    expect(g.plays()).toBe(0); // joined, still buffering
    g.open();
    await vi.waitFor(() => expect(g.plays()).toBe(1));
    expect(g.stages).toContain("face-wait");
    g.a.onended?.();
    await done;
  });

  it("the wait is bounded: after startWaitMs the voice starts and a later clip joins late", async () => {
    const g = gated({ startWaitMs: 20 });
    const done = g.s.play("hi");
    await vi.waitFor(() => expect(g.plays()).toBe(1));
    g.deliver();
    await vi.waitFor(() => expect(g.joins).toEqual([true]));
    g.a.onended?.();
    await done;
  });

  it("no face to show: the voice does not wait", async () => {
    const g = gated({ canShow: false });
    const done = g.s.play("hi");
    await vi.waitFor(() => expect(g.plays()).toBe(1));
    g.a.onended?.();
    await done;
  });

  it("stop while waiting for the face: the voice never starts and the clip is released", async () => {
    const g = gated();
    const done = g.s.play("hi");
    await new Promise((r) => setTimeout(r, 5)); // synthesised, stream opening
    g.deliver();
    await vi.waitFor(() => expect(g.joins).toEqual([false]));
    g.s.stop();
    await done;
    g.open();
    await new Promise((r) => setTimeout(r, 5));
    expect(g.plays()).toBe(0);
    expect(g.sync.leave).toHaveBeenCalled();
    expect(g.release).toHaveBeenCalled();
  });
});

describe("pause and resume (#90, #88)", () => {
  const evAudio = (url: string) => {
    const l: Record<string, (() => void)[]> = {};
    const a = {
      url, onended: null as (() => void) | null, onerror: null, paused: true, currentTime: 0, plays: 0,
      play() { a.plays++; a.paused = false; (l.playing || []).forEach((f) => f()); return Promise.resolve(); },
      pause() { a.paused = true; (l.pause || []).forEach((f) => f()); },
      addEventListener(t: string, f: () => void) { (l[t] ||= []).push(f); },
      removeEventListener(t: string, f: () => void) { l[t] = (l[t] || []).filter((x) => x !== f); },
    };
    return a;
  };

  it("pause holds the clip at its place and resume continues the same clip", async () => {
    const clips: ReturnType<typeof evAudio>[] = [];
    const on: boolean[] = [];
    const s = createSentenceSpeaker(async (t) => ({ audio_url: `/m/${t}` }), (u) => { const a = evAudio(u); clips.push(a); return a; }, { onPlaying: (x) => on.push(x) });
    const done = s.play("a");
    await vi.waitFor(() => expect(clips[0]?.plays).toBe(1));
    clips[0].currentTime = 1.7;
    s.pause!();
    expect(s.paused).toBe(true);
    expect(clips[0].paused).toBe(true);
    expect(on.at(-1)).toBe(false); // the face goes quiet with the voice
    expect(s.resume!()).toBe(true);
    expect(clips).toHaveLength(1); // the same clip, not a new one
    expect(clips[0].plays).toBe(2);
    expect(clips[0].currentTime).toBe(1.7); // never rewound
    expect(on.at(-1)).toBe(true);
    clips[0].onended?.();
    await done;
  });

  it("a sentence that has not started yet waits while paused, then starts on resume", async () => {
    const clips: ReturnType<typeof evAudio>[] = [];
    let release!: () => void;
    const s = createSentenceSpeaker(() => new Promise((r) => { release = () => r({ audio_url: "/x" }); }), (u) => { const a = evAudio(u); clips.push(a); return a; });
    const done = s.play("a");
    s.pause!();
    release();
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(clips[0].plays).toBe(0);
    s.resume!();
    expect(clips[0].plays).toBe(1);
    clips[0].onended?.();
    await done;
  });

  it("stop ends a paused sentence; resume after stop has nothing to continue", async () => {
    const clips: ReturnType<typeof evAudio>[] = [];
    const s = createSentenceSpeaker(async () => ({ audio_url: "/x" }), (u) => { const a = evAudio(u); clips.push(a); return a; });
    const done = s.play("a");
    await vi.waitFor(() => expect(clips[0]?.plays).toBe(1));
    s.pause!();
    s.stop();
    await done;
    expect(s.paused).toBe(false);
    expect(s.resume!()).toBe(false);
    expect(clips[0].plays).toBe(1);
  });
});
