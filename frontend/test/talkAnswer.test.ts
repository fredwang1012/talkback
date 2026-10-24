import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  TalkController, type ReaderControl, type SessionEvent, type TalkSession, type TalkState,
} from "../src/talk/TalkController";

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); }
  async startMic() { this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(instructions: string) { this.calls.push(`respond:${instructions}`); }
  async interrupt() { this.calls.push("interrupt"); }
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}

class FakeReader implements ReaderControl {
  playing: boolean;
  index = 5;
  log: string[] = [];
  constructor(playing = true) { this.playing = playing; }
  isPlaying() { return this.playing; }
  currentIndex() { return this.index; }
  currentPara() { return Math.floor(this.index / 2); }
  pause() { this.playing = false; this.log.push("pause"); }
  play() { this.playing = true; this.log.push("play"); }
  resumeAt(i: number) { this.playing = true; this.index = i; this.log.push(`resumeAt:${i}`); }
}

class FakeSpeaker {
  log: string[] = [];
  gates: Record<string, () => void> = {};
  play = (text: string) => new Promise<void>((resolve) => { this.log.push(`play:${text}`); this.gates[text] = resolve; });
  prefetch = (text: string) => { this.log.push(`prefetch:${text}`); };
  stop = () => { this.log.push("stop"); this.paused = false; Object.values(this.gates).forEach((g) => g()); };
  paused = false;
  pause = () => { this.log.push("pause"); this.paused = true; };
  resume = () => { this.log.push("resume"); const was = this.paused; this.paused = false; return was; };
}

const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);

function setup(opts: { playing?: boolean } = {}) {
  const session = new FakeSession();
  const reader = new FakeReader(opts.playing ?? true);
  const toasts: string[] = [];
  const states: TalkState[] = [];
  const retrieved: unknown[][] = [];
  const speaker = new FakeSpeaker();
  const stream: { emit: (t: string) => void; finish: () => void; fail: (m: string) => void; signal?: AbortSignal; args?: unknown[] } = {
    emit: () => {}, finish: () => {}, fail: () => {},
  };
  const talk = new TalkController({
    session, reader,
    retrieve: async (...args) => { retrieved.push(args); return { instructions: "INSTR" }; },
    voiceFor: () => "marcus",
    onState: (s) => states.push(s), onToast: (m) => toasts.push(m),
    speaker,
    answer: (query, para, speakerId, onSentence, signal) => new Promise<void>((resolve, reject) => {
      stream.args = [query, para, speakerId];
      stream.signal = signal;
      stream.emit = onSentence;
      stream.finish = resolve;
      stream.fail = (m) => reject(new Error(m));
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }),
  });
  return { session, reader, talk, speaker, stream, toasts, states, retrieved };
}
type H = ReturnType<typeof setup>;

async function ask(h: H, text = "what do you think?") {
  await h.talk.pttDown();
  await tick(600);
  const up = h.talk.pttUp();
  await tick(0);
  h.session.emit({ type: "transcript", text });
  await tick(0);
  await up;
}
async function end(h: H, text: string) { h.speaker.gates[text]?.(); await tick(0); }

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("TalkController via /api/answer", () => {
  it("speaks sentences in order, the first before the stream ends, without retrieve/respond", async () => {
    const h = setup();
    await ask(h);
    expect(h.talk.state).toBe("retrieving");
    expect(h.stream.args).toEqual(["what do you think?", 2, "narrator"]);
    h.stream.emit("One.");
    await tick(0);
    expect(h.talk.state).toBe("answering");
    expect(h.speaker.log).toEqual(["play:One."]); // started while the stream is still open
    h.stream.emit("Two.");
    h.stream.emit("Three.");
    await tick(0);
    expect(h.speaker.log.filter((l) => l !== "prefetch:Two.")).toEqual(["play:One.", "prefetch:Three."]); // up to PREFETCH_DEPTH ahead
    expect(h.speaker.log).toContain("prefetch:Two.");
    await end(h, "One.");
    await end(h, "Two.");
    expect(h.speaker.log.filter((l) => l.startsWith("play:"))).toEqual(["play:One.", "play:Two.", "play:Three."]);
    h.stream.finish();
    await end(h, "Three.");
    expect(h.talk.state).toBe("resuming");
    await tick(1500);
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
    expect(h.retrieved).toHaveLength(0);
    expect(h.session.calls.filter((c) => c.startsWith("respond"))).toHaveLength(0);
  });

  it("waits for the stream to finish before resuming", async () => {
    const h = setup();
    await ask(h);
    h.stream.emit("One.");
    await tick(0);
    await end(h, "One.");
    expect(h.talk.state).toBe("answering");
    h.stream.finish();
    await tick(0);
    expect(h.talk.state).toBe("resuming");
  });

  it("barge-in stops the clip, drops the queue and aborts the stream", async () => {
    const h = setup();
    await ask(h);
    h.stream.emit("One.");
    h.stream.emit("Two.");
    await tick(0);
    await h.talk.pttDown();
    expect(h.speaker.log).toContain("stop");
    expect(h.stream.signal?.aborted).toBe(true);
    expect(h.talk.state).toBe("listening");
    await tick(2000);
    expect(h.speaker.log.filter((l) => l.startsWith("play:"))).toEqual(["play:One."]); // Two. never plays
    expect(h.reader.log).toEqual(["pause"]); // still in the question, not resumed
  });

  it("with the reader paused it ends in talk-idle and never starts playback", async () => {
    const h = setup({ playing: false });
    await ask(h);
    h.stream.emit("One.");
    h.stream.finish();
    await tick(0);
    await end(h, "One.");
    await tick(5000);
    expect(h.talk.state).toBe("talk-idle");
    expect(h.reader.log).toEqual([]);
  });

  it("auto-resume off stays idle", async () => {
    const h = setup();
    h.talk.setAutoResume(false);
    await ask(h);
    h.stream.emit("One.");
    h.stream.finish();
    await tick(0);
    await end(h, "One.");
    expect(h.talk.state).toBe("talk-idle");
  });

  it("an error before any sentence toasts and resumes the story", async () => {
    const h = setup();
    await ask(h);
    h.stream.fail("model unavailable");
    await tick(0);
    expect(h.toasts[0]).toContain("model unavailable");
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("an error after some sentences still speaks them, then finishes", async () => {
    const h = setup();
    await ask(h);
    h.stream.emit("One.");
    await tick(0);
    h.stream.fail("cut off");
    await tick(0);
    expect(h.talk.state).toBe("answering");
    await end(h, "One.");
    expect(h.toasts).toContain("cut off");
    expect(h.talk.state).toBe("resuming");
  });

  it("no first sentence in time times out and resumes", async () => {
    const h = setup();
    await ask(h);
    await tick(16000);
    expect(h.toasts.some((t) => t.includes("too long"))).toBe(true);
    expect(h.stream.signal?.aborted).toBe(true);
    expect(h.talk.state).toBe("reading");
  });

  it("a playback failure toasts and resumes", async () => {
    const h = setup();
    h.speaker.play = () => Promise.reject(new Error("Could not play the answer audio."));
    await ask(h);
    h.stream.emit("One.");
    await tick(0);
    expect(h.toasts[0]).toContain("Could not play");
    expect(h.talk.state).toBe("reading");
  });
});

describe("speaking follows the audio", () => {
  it("is not set while a sentence is still being synthesised, only when the speaker reports playing", async () => {
    const h = setup();
    const speaking: boolean[] = [];
    (h.talk as unknown as { d: { onSpeaking?: (s: boolean) => void } }).d.onSpeaking = (s) => speaking.push(s);
    await ask(h);
    h.stream.emit("One.");
    await tick(0);
    expect(speaking).toEqual([]);
    h.talk.notePlaying(true);
    expect(speaking).toEqual([true]);
    h.talk.notePlaying(false);
    expect(speaking).toEqual([true, false]);
  });
});

describe("pause / resume of the live answer (#90)", () => {
  it("pauses the speaker where it is and resumes it, staying in the same answer", async () => {
    const h = setup();
    await ask(h);
    expect(h.talk.pauseAnswer()).toBe(false); // nothing is being said yet
    h.stream.emit("One.");
    h.stream.emit("Two.");
    await tick(0);
    expect(h.talk.pauseAnswer()).toBe(true);
    expect(h.talk.answerPaused).toBe(true);
    expect(h.speaker.log).toContain("pause");
    await tick(20000); // a long pause is not a timeout
    expect(h.talk.state).toBe("answering");
    expect(h.talk.resumeAnswer()).toBe(true);
    expect(h.talk.answerPaused).toBe(false);
    await end(h, "One.");
    h.stream.finish();
    await end(h, "Two.");
    expect(h.talk.state).toBe("resuming");
    expect(h.speaker.log.filter((l) => l.startsWith("play:"))).toEqual(["play:One.", "play:Two."]);
  });

  it("a new question while paused cancels the paused answer", async () => {
    const h = setup();
    await ask(h);
    h.stream.emit("One.");
    await tick(0);
    h.talk.pauseAnswer();
    await h.talk.pttDown();
    expect(h.talk.answerPaused).toBe(false);
    expect(h.speaker.log).toContain("stop");
    expect(h.talk.resumeAnswer()).toBe(false);
  });
});
