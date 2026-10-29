/**
 * #34: talk while reading. A question addressed to a character goes to them (voice, retrieval, bubble); reading
 * pauses and resumes at the same sentence; on hands-free the book read aloud counts as echo.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TalkController, type ReaderControl, type SessionEvent, type TalkSession, type TalkState } from "../src/talk/TalkController";
import { addressedSpeaker } from "../src/talk/route";

class FakeSession implements TalkSession {
  calls: string[] = [];
  listeners = new Set<(e: SessionEvent) => void>();
  isOpen = true;
  async ensureOpen(voice: string) { this.calls.push(`open:${voice}`); }
  async setVoice(voice: string) { this.calls.push(`voice:${voice}`); }
  async startMic() { this.calls.push("mic-start"); }
  async stopMic(discard = false) { this.calls.push(discard ? "mic-discard" : "mic-stop"); }
  commit() { this.calls.push("commit"); }
  respond(_i: string, question?: string) { this.calls.push(`respond:${question}`); }
  async interrupt() { this.calls.push("interrupt"); }
  async setMonitor(on: boolean) { this.calls.push(`monitor:${on}`); }
  setServerTurns(on: boolean) { this.calls.push(`server:${on}`); }
  setMicHold(hold: boolean) { this.calls.push(`hold:${hold}`); }
  clearInput() {}
  on(fn: (e: SessionEvent) => void) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(e: SessionEvent) { [...this.listeners].forEach((fn) => fn(e)); }
}

class FakeReader implements ReaderControl {
  log: string[] = [];
  constructor(public playing = true, public index = 7) {}
  isPlaying() { return this.playing; }
  currentIndex() { return this.index; }
  currentPara() { return 3; }
  pause() { this.playing = false; this.log.push("pause"); }
  play() { this.playing = true; this.log.push("play"); }
  resumeAt(i: number) { this.playing = true; this.index = i; this.log.push(`resumeAt:${i}`); }
}

const cast = [{ id: "narrator", name: "Narrator" }, { id: "alice", name: "Alice" }, { id: "hatter", name: "The Hatter" }];
const voices: Record<string, string> = { narrator: "marcus", alice: "emma", hatter: "oliver" };

function setup(opts: { gemini?: boolean; readingIsEcho?: boolean; serverTurns?: boolean; playing?: boolean } = {}) {
  const session = new FakeSession();
  const reader = new FakeReader(opts.playing ?? true);
  const states: TalkState[] = [];
  const order: string[] = []; // what the UI saw, in order
  const retrieved: string[] = [];
  const answered: string[] = [];
  let now = 0;
  const talk = new TalkController({
    session, reader, now: () => now, readingIsEcho: opts.readingIsEcho, serverTurns: opts.serverTurns,
    retrieve: async (_q, _p, speaker) => { retrieved.push(speaker); return { instructions: "I" }; },
    ...(opts.gemini ? {
      answer: async (_q, _p, speaker, onSentence) => { answered.push(speaker); onSentence("Curiouser."); },
      speaker: { play: async () => {}, prefetch: () => {}, stop: () => {} },
    } : {}),
    voiceFor: (id) => voices[id] ?? "marcus",
    route: (q) => addressedSpeaker(q, cast),
    onRoute: (id) => order.push(`route:${id}`),
    onTranscript: (t) => order.push(`question:${t}`),
    onState: (s) => states.push(s),
  });
  const advance = async (ms: number) => { now += ms; await vi.advanceTimersByTimeAsync(ms); };
  return { session, reader, talk, states, order, retrieved, answered, advance };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("TalkController: routing a question to the character it names (#34)", () => {
  it("a typed 'Alice, ...' while reading: Alice answers in her voice, then reading resumes at the same sentence", async () => {
    const h = setup();
    await h.talk.ask("Alice, why are you so small?");
    expect(h.order).toEqual(["route:alice", "question:Alice, why are you so small?"]); // the bubble is already hers
    expect(h.reader.log).toEqual(["pause"]);
    expect(h.session.calls).toContain("voice:emma");
    expect(h.retrieved).toEqual(["alice"]);
    expect(h.session.calls.at(-1)).toBe("respond:Alice, why are you so small?");
    h.session.emit({ type: "audio-start" });
    h.session.emit({ type: "response-done", status: "completed" });
    h.session.emit({ type: "playback-drained" });
    await h.advance(1500); // auto-resume delay
    expect(h.reader.log).toEqual(["pause", "resumeAt:7"]);
    expect(h.states.at(-1)).toBe("reading");
  });

  it("a question naming nobody stays with the last one picked (no route, no voice change)", async () => {
    const h = setup();
    await h.talk.setSpeaker("hatter");
    h.session.calls = [];
    await h.talk.ask("What do you think of Alice?");
    expect(h.order).toEqual(["question:What do you think of Alice?"]);
    expect(h.session.calls.some((c) => c.startsWith("voice:"))).toBe(false);
    expect(h.retrieved).toEqual(["hatter"]);
  });

  it("the routed speaker stays for the next question", async () => {
    const h = setup();
    await h.talk.ask("Hatter, why?");
    h.session.emit({ type: "response-done", status: "completed" });
    await h.talk.ask("And then what?");
    expect(h.retrieved).toEqual(["hatter", "hatter"]);
  });

  it("a spoken question is routed after its transcript, before it is answered", async () => {
    const h = setup();
    await h.talk.pttDown();
    await h.advance(600);
    const up = h.talk.pttUp();
    await h.advance(0);
    h.session.emit({ type: "transcript", text: "Alice, where is the rabbit?" });
    await up;
    await h.advance(0);
    expect(h.order).toEqual(["route:alice", "question:Alice, where is the rabbit?"]);
    const calls = h.session.calls;
    expect(calls.indexOf("voice:emma")).toBeGreaterThan(calls.indexOf("commit"));
    expect(calls.indexOf("voice:emma")).toBeLessThan(calls.findIndex((c) => c.startsWith("respond:")));
    expect(h.retrieved).toEqual(["alice"]);
  });

  it("on the Gemini path the answer is written and voiced for the routed speaker, without a session voice change", async () => {
    const h = setup({ gemini: true });
    await h.talk.ask("Alice, hello");
    await h.advance(0);
    expect(h.answered).toEqual(["alice"]);
    expect(h.session.calls.some((c) => c.startsWith("voice:"))).toBe(false);
  });
});

describe("TalkController: hands-free while the book is read aloud (#34)", () => {
  /** Mic level readings every 20 ms for `ms`. */
  const level = async (h: ReturnType<typeof setup>, rms: number, ms: number) => {
    for (let t = 0; t < ms; t += 20) { h.talk.noteLevel(rms); await h.advance(20); }
  };

  it("local gate: speech over the reading needs the stricter barge-in profile (a 200 ms burst is not a question)", async () => {
    const h = setup({ readingIsEcho: true });
    await h.talk.setHandsFree(true);
    await level(h, 0.01, 400); // the book's echo as the mic hears it
    await level(h, 0.1, 200);
    expect(h.states).not.toContain("listening");
    await level(h, 0.1, 100); // sustained: a real barge-in pauses the reading
    expect(h.states).toContain("listening");
    expect(h.reader.log).toEqual(["pause"]);
  });

  it("without readingIsEcho the old gate applies (unchanged behaviour)", async () => {
    const h = setup();
    await h.talk.setHandsFree(true);
    await level(h, 0.01, 400);
    await level(h, 0.1, 200);
    expect(h.states).toContain("listening");
  });

  it("server turns: the mic is held while the book is read aloud and released when reading stops", async () => {
    const h = setup({ readingIsEcho: true, serverTurns: true });
    await h.talk.setHandsFree(true);
    h.session.calls = [];
    await level(h, 0.01, 40);
    expect(h.session.calls).toEqual(["hold:true"]);
    h.reader.playing = false; // the reader paused
    await level(h, 0.01, 40);
    expect(h.session.calls).toEqual(["hold:true", "hold:false"]);
  });

  it("server turns: clear speech over the reading cuts in (barge-in pauses the book)", async () => {
    const h = setup({ readingIsEcho: true, serverTurns: true });
    await h.talk.setHandsFree(true);
    await level(h, 0.01, 400);
    await level(h, 0.12, 300);
    expect(h.states).toContain("listening");
    expect(h.reader.log).toEqual(["pause"]);
  });
});
