import { describe, expect, it, vi } from "vitest";
import { createReplayPlayer, createReplayStore, pcmToWav, runPcm, runSentences, MAX_REPLAYS } from "../src/talk/replay";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";

const clip = { audio_url: "/a.mp3" };

describe("replay store", () => {
  it("keeps only the last MAX_REPLAYS replies", () => {
    const s = createReplayStore();
    for (let i = 1; i <= MAX_REPLAYS + 5; i++) s.addSentence(i, "x", clip);
    expect(s.ids().length).toBe(MAX_REPLAYS);
    expect(s.has(1)).toBe(false);
    expect(s.has(MAX_REPLAYS + 5)).toBe(true);
  });
  it("collects sentences in order and pcm chunks per reply", () => {
    const s = createReplayStore();
    s.addSentence(1, "a", clip); s.addSentence(1, "b", clip); s.addPcm(2, "AA=="); s.addPcm(2, "AA==");
    expect((s.get(1) as any).parts.map((p: any) => p.text)).toEqual(["a", "b"]);
    expect((s.get(2) as any).chunks.length).toBe(2);
    s.clear();
    expect(s.ids()).toEqual([]);
  });
});

describe("pcmToWav", () => {
  it("writes a 24 kHz mono 16-bit header around the samples", () => {
    const b64 = btoa(String.fromCharCode(1, 0, 2, 0));
    const w = pcmToWav([b64, b64]);
    const v = new DataView(w.buffer);
    expect(String.fromCharCode(...w.slice(0, 4))).toBe("RIFF");
    expect(v.getUint32(24, true)).toBe(24000);
    expect(v.getUint16(34, true)).toBe(16);
    expect(v.getUint32(40, true)).toBe(8);
    expect(w.length).toBe(52);
  });
});

function fakeSpeaker() {
  const resolvers: (() => void)[] = [];
  const played: string[] = [];
  const sp: SentenceSpeaker = {
    prefetch: vi.fn(),
    play: (t) => { played.push(t); return new Promise<void>((r) => resolvers.push(r)); },
    stop: vi.fn(() => resolvers.splice(0).forEach((r) => r())),
  };
  return { sp, played, finish: () => resolvers.splice(0).forEach((r) => r()) };
}

describe("runSentences", () => {
  it("plays parts in order and stops for good", async () => {
    const f = fakeSpeaker();
    const r = runSentences([{ text: "a" }, { text: "b" }, { text: "c" }], f.sp);
    await Promise.resolve();
    expect(f.played).toEqual(["a"]);
    f.finish(); await new Promise((x) => setTimeout(x, 0));
    expect(f.played).toEqual(["a", "b"]);
    r.stop(); await r.done;
    expect(f.played).toEqual(["a", "b"]); // never moved on to "c"
  });
});

describe("replay player", () => {
  const mk = () => {
    const store = createReplayStore();
    store.addPcm(1, "AA=="); store.addPcm(2, "AA==");
    const runs: { stop: ReturnType<typeof vi.fn>; end: () => void }[] = [];
    const changes: (number | null)[] = [];
    const player = createReplayPlayer(store, () => {
      let end = () => {};
      const done = new Promise<void>((r) => { end = r; });
      const stop = vi.fn(() => end());
      runs.push({ stop, end });
      return { done, stop };
    }, (p) => changes.push(p));
    return { player, runs, changes };
  };
  it("plays one reply at a time and toggles off", () => {
    const { player, runs, changes } = mk();
    player.toggle(1);
    player.toggle(2);
    expect(runs[0].stop).toHaveBeenCalled();
    expect(player.playing()).toBe(2);
    player.toggle(2);
    expect(player.playing()).toBeNull();
    expect(changes).toEqual([1, null, 2, null]);
  });
  it("clears the playing id when the reply ends", async () => {
    const { player, runs } = mk();
    player.toggle(1);
    runs[0].end();
    await new Promise((x) => setTimeout(x, 0));
    expect(player.playing()).toBeNull();
  });
  it("ignores an unknown reply", () => {
    const { player, runs } = mk();
    player.toggle(9);
    expect(runs.length).toBe(0);
  });
});

describe("replay pause / resume (#90)", () => {
  it("a run that can pause is paused and resumed by toggling; another reply stops it", () => {
    const store = createReplayStore();
    store.addPcm(1, "AA=="); store.addPcm(2, "AA==");
    const log: string[] = [];
    const changes: (number | null)[] = [];
    const player = createReplayPlayer(store, (item) => {
      const id = item === store.get(1) ? 1 : 2;
      return { done: new Promise<void>(() => {}), stop: () => log.push(`stop${id}`), pause: () => log.push(`pause${id}`), resume: () => { log.push(`resume${id}`); return true; } };
    }, (p) => changes.push(p));
    player.toggle(1);
    player.toggle(1);
    expect(player.playing()).toBeNull();
    expect(player.paused()).toBe(1);
    player.toggle(1);
    expect(player.playing()).toBe(1);
    expect(player.paused()).toBeNull();
    player.toggle(1); // pause again
    player.toggle(2); // a paused reply is dropped when another plays
    expect(log).toEqual(["pause1", "resume1", "pause1", "stop1"]);
    expect(player.playing()).toBe(2);
    expect(player.paused()).toBeNull();
    expect(changes.filter((c, i) => c !== changes[i - 1])).toEqual([1, null, 1, null, 2]);
  });

  it("runSentences pauses and resumes its speaker", () => {
    const f = fakeSpeaker();
    const sp = { ...f.sp, pause: vi.fn(), resume: vi.fn(() => true) };
    const r = runSentences([{ text: "a" }], sp);
    r.pause?.();
    expect(r.resume?.()).toBe(true);
    expect(sp.pause).toHaveBeenCalled();
    expect(sp.resume).toHaveBeenCalled();
  });

  it("runPcm pauses the audio element and plays it on from there", () => {
    const a = { onended: null, onerror: null, play: vi.fn(() => Promise.resolve()), pause: vi.fn() } as unknown as HTMLAudioElement;
    const url = globalThis.URL as unknown as { createObjectURL?: unknown; revokeObjectURL?: unknown };
    url.createObjectURL ??= () => "blob:x"; url.revokeObjectURL ??= () => {};
    const r = runPcm(["AA=="], () => a);
    r.pause?.();
    expect(a.pause).toHaveBeenCalled();
    r.resume?.();
    expect(a.play).toHaveBeenCalledTimes(2);
  });
});
