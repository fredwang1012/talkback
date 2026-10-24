import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  TalkController, type Latency, type ReaderControl, type SessionEvent, type TalkSession, type TalkState,
} from "../src/talk/TalkController";

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  micFails = false;
  openFails = false;
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); if (this.openFails) throw new Error("no key"); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); }
  async startMic() { if (this.micFails) throw new Error("denied"); this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(instructions: string) { this.calls.push(`respond:${instructions}`); }
  async interrupt() { this.calls.push("interrupt"); }
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}

class FakeReader implements ReaderControl {
  playing: boolean;
  index: number;
  log: string[] = [];
  constructor(playing = true, index = 5) { this.playing = playing; this.index = index; }
  isPlaying() { return this.playing; }
  currentIndex() { return this.index; }
  currentPara() { return Math.floor(this.index / 2); }
  pause() { this.playing = false; this.log.push("pause"); }
  play() { this.playing = true; this.log.push("play"); }
  resumeAt(i: number) { this.playing = true; this.index = i; this.log.push(`resumeAt:${i}`); }
}

function setup(opts: { playing?: boolean; retrieve?: () => Promise<{ instructions: string }> } = {}) {
  const session = new FakeSession();
  const reader = new FakeReader(opts.playing ?? true);
  const states: TalkState[] = [];
  const toasts: string[] = [];
  const latencies: Latency[] = [];
  const retrieved: unknown[][] = [];
  const talk = new TalkController({
    session, reader,
    retrieve: async (...args) => { retrieved.push(args); return opts.retrieve ? opts.retrieve() : { instructions: "INSTR" }; },
    voiceFor: (id) => (id === "holmes" ? "oliver" : "marcus"),
    onState: (s) => states.push(s), onToast: (m) => toasts.push(m), onLatency: (l) => latencies.push(l),
  });
  return { session, reader, talk, states, toasts, latencies, retrieved };
}
const tick = (ms: number) => vi.advanceTimersByTimeAsync(ms);

async function ask(h: ReturnType<typeof setup>, text = "what do you think?") {
  await h.talk.pttDown();
  await tick(600);
  const up = h.talk.pttUp();
  await tick(0);
  h.session.emit({ type: "transcript", text });
  await tick(0);
  await up;
}

async function finishAnswer(h: ReturnType<typeof setup>) {
  h.session.emit({ type: "audio-start" });
  await tick(0);
  h.session.emit({ type: "response-done", status: "completed" });
  h.session.emit({ type: "playback-drained" });
  await tick(0);
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("TalkController core loop", () => {
  it("interrupt -> ask -> answer -> resume from the same sentence", async () => {
    const h = setup();
    await h.talk.pttDown();
    expect(h.reader.log).toEqual(["pause"]);
    expect(h.talk.state).toBe("listening");
    expect(h.session.calls).toContain("open:marcus");
    expect(h.session.calls).toContain("mic-start");

    await tick(600);
    const up = h.talk.pttUp();
    await tick(0);
    expect(h.talk.state).toBe("transcribing");
    expect(h.session.calls).toContain("mic-stop");
    expect(h.session.calls).toContain("commit");
    h.session.emit({ type: "transcript", text: "  What do you make of him?  " });
    await tick(0);
    await up;
    expect(h.retrieved[0]).toEqual(["What do you make of him?", 2, "narrator"]);
    expect(h.talk.state).toBe("answering");
    expect(h.session.calls).toContain("respond:INSTR");

    h.session.emit({ type: "audio-start" });
    await tick(0);
    expect(h.latencies[0]).toHaveProperty("firstAudioMs");
    expect(h.latencies[0]).toHaveProperty("commitToTranscriptMs");
    h.session.emit({ type: "response-done", status: "completed" });
    h.session.emit({ type: "playback-drained" });
    await tick(0);
    expect(h.talk.state).toBe("resuming");
    expect(h.reader.log).toEqual(["pause"]); // waits the grace window first
    await tick(1500);
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("a short press is a tap: no question, toggles play/pause", async () => {
    const playing = setup({ playing: true });
    await playing.talk.pttDown();
    await tick(100);
    await playing.talk.pttUp();
    expect(playing.session.calls).toContain("mic-discard");
    expect(playing.session.calls).not.toContain("commit");
    expect(playing.talk.state).toBe("reading");
    expect(playing.reader.playing).toBe(false); // was playing -> now paused

    const paused = setup({ playing: false });
    await paused.talk.pttDown();
    await tick(100);
    await paused.talk.pttUp();
    expect(paused.reader.playing).toBe(true); // was paused -> now playing
  });

  it("uses an empty query when the transcript never arrives, and still answers", async () => {
    const h = setup();
    await h.talk.pttDown();
    await tick(600);
    const up = h.talk.pttUp();
    await tick(2600); // transcript timeout is 2.5 s
    await up;
    expect(h.retrieved[0][0]).toBe("");
    expect(h.talk.state).toBe("answering");
  });

  it("a failed lookup toasts and resumes the story instead of hanging", async () => {
    const h = setup({ retrieve: () => Promise.reject(new Error("offline")) });
    await ask(h);
    expect(h.toasts[0]).toContain("offline");
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("pressing during the resume window starts a follow-up and keeps the original resume point", async () => {
    const h = setup();
    await ask(h);
    await finishAnswer(h);
    expect(h.talk.state).toBe("resuming");
    h.reader.index = 99; // must not be re-captured as the resume point
    await tick(1000);
    await h.talk.pttDown();
    expect(h.talk.state).toBe("listening");
    await tick(2000); // the old resume timer must be dead
    expect(h.reader.log).toEqual(["pause"]);
    expect(h.talk.state).toBe("listening");
    await tick(600);
    const up = h.talk.pttUp();
    await tick(0);
    h.session.emit({ type: "transcript", text: "and then?" });
    await tick(0);
    await up;
    await finishAnswer(h);
    await tick(1500);
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("pressing while an answer plays interrupts it", async () => {
    const h = setup();
    await ask(h);
    h.session.emit({ type: "audio-start" });
    await tick(0);
    await h.talk.pttDown();
    expect(h.session.calls).toContain("interrupt");
    expect(h.talk.state).toBe("listening");
  });

  it("pressing while transcribing abandons the stale turn (no answer for the old question)", async () => {
    const h = setup();
    await h.talk.pttDown();
    await tick(600);
    const up = h.talk.pttUp();
    await tick(0);
    expect(h.talk.state).toBe("transcribing");
    await h.talk.pttDown();
    h.session.emit({ type: "transcript", text: "old question" });
    await tick(0);
    await up;
    expect(h.session.calls.filter((c) => c.startsWith("respond"))).toHaveLength(0);
    expect(h.talk.state).toBe("listening");
  });

  it("with auto-resume off it stays idle and never restarts the reader", async () => {
    const h = setup();
    h.talk.setAutoResume(false);
    await ask(h);
    await finishAnswer(h);
    expect(h.talk.state).toBe("talk-idle");
    await tick(5000);
    expect(h.reader.log).toEqual(["pause"]);
  });

  it("surfaces a real session error and goes back to reading", async () => {
    const h = setup();
    await ask(h);
    h.session.emit({ type: "error", message: "Billing refused the request" });
    await tick(0);
    expect(h.toasts).toContain("Billing refused the request");
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("microphone denial toasts and keeps reading", async () => {
    const h = setup();
    h.session.micFails = true;
    await h.talk.pttDown();
    await tick(0);
    expect(h.toasts[0]).toContain("Microphone");
    expect(h.talk.state).toBe("reading");
    expect(h.reader.log).toEqual(["pause", "resumeAt:5"]);
  });

  it("connection failure toasts and keeps reading", async () => {
    const h = setup();
    h.session.openFails = true;
    await h.talk.pttDown();
    await tick(0);
    expect(h.toasts[0]).toContain("no key");
    expect(h.talk.state).toBe("reading");
  });

  it("a response that ends without audio still finishes the turn", async () => {
    const h = setup();
    await ask(h);
    h.session.emit({ type: "response-done", status: "completed" });
    await tick(1500);
    expect(h.talk.state).toBe("reading");
  });

  it("an answer that never starts times out, cancels and resumes", async () => {
    const h = setup();
    await ask(h);
    await tick(16000);
    expect(h.toasts.some((t) => t.includes("too long"))).toBe(true);
    expect(h.session.calls).toContain("interrupt");
    expect(h.talk.state).toBe("reading");
  });

  it("speaker switch changes the voice used for the session", async () => {
    const h = setup();
    await h.talk.setSpeaker("holmes");
    expect(h.session.calls).toContain("voice:oliver");
    await h.talk.pttDown();
    expect(h.session.calls).toContain("open:oliver");
    expect(h.session.calls.some((c) => c === "open:marcus")).toBe(false);
  });

  it("survives ten consecutive cycles without getting stuck", async () => {
    const h = setup();
    for (let i = 0; i < 10; i++) {
      await ask(h, `question ${i}`);
      expect(h.talk.state).toBe("answering");
      await finishAnswer(h);
      await tick(1500);
      expect(h.talk.state).toBe("reading");
      expect(h.reader.playing).toBe(true);
    }
    expect(h.retrieved).toHaveLength(10);
    expect(h.states.filter((s) => s === "answering")).toHaveLength(10);
  });
});
