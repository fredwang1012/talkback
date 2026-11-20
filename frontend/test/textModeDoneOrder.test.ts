/**
 * Text mode (B1): the model can finish writing before the first sentence is heard (its TTS takes ~2.5 s). The
 * response-done must not reach the controller before audio-start, or it reads as "an answer with no sound" and reading
 * resumes over the character (code review, 2026-10-28).
 */
import { describe, expect, it } from "vitest";
import { TalkClient, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";
import type { SessionEvent } from "../src/talk/TalkController";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: Record<string, any>[] = [];
  constructor(public url: string, public protocols: string[]) {}
  send(data: string) { this.sent.push(JSON.parse(data)); }
  close(code = 1000, reason = "") { this.readyState = 3; this.onclose?.({ code, reason }); }
  open() { this.readyState = 1; this.onopen?.(); }
  recv(evt: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(evt) }); }
}
/** A speaker whose sound starts only when the test says so (real TTS is slow). */
class SlowSpeaker implements SentenceSpeaker {
  onPlaying: (on: boolean) => void = () => {};
  private pending: { start: () => void; end: () => void }[] = [];
  silent = false;
  prefetch() {}
  play() {
    return new Promise<void>((resolve) => this.pending.push({
      start: () => { if (!this.silent) this.onPlaying(true); },
      end: () => { if (!this.silent) this.onPlaying(false); resolve(); },
    }));
  }
  startSound() { this.pending[0]?.start(); }
  finish() { this.pending.shift()?.end(); }
  stop() { this.pending.splice(0).forEach((p) => p.end()); }
}
const player: PlayerLike = { resume: async () => {}, enqueueBase64() {}, flush: async () => 0, reset() {}, close: async () => {} };
const mic: MicLike = { start: async () => {}, stop: async () => {} };
const flush = () => new Promise((r) => setTimeout(r, 0));

async function open() {
  const sockets: FakeSocket[] = [];
  const speaker = new SlowSpeaker();
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: () => mic, cancelWaitMs: 20,
    createSpeaker: (onPlaying) => { speaker.onPlaying = onPlaying; return speaker; },
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await flush();
  sockets[0].open();
  await p;
  return { client, ws: sockets[0], events, speaker };
}
const order = (events: SessionEvent[]) => events.map((e) => e.type).filter((t) => ["audio-start", "response-done", "playback-drained"].includes(t));

describe("text mode: response-done waits for the first sound", () => {
  it("the model finishes writing before the first sentence plays: audio-start still comes first", async () => {
    const { client, ws, events, speaker } = await open();
    client.respond("PASSAGES", "Who are you?");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", delta: "[contentment] I am the Cat. You must be mad too." });
    ws.recv({ type: "response.output_text.done", response_id: "r1", text: "[contentment] I am the Cat. You must be mad too." });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed" } });
    await flush();
    expect(order(events)).toEqual([]); // nothing yet: the sound has not started
    speaker.startSound();
    expect(order(events)).toEqual(["audio-start", "response-done"]);
    speaker.finish(); await flush();
    speaker.startSound(); speaker.finish(); await flush();
    expect(order(events)).toEqual(["audio-start", "response-done", "playback-drained"]);
  });

  it("sentences that never make a sound still end the turn (response-done, then drained)", async () => {
    const { client, ws, events, speaker } = await open();
    speaker.silent = true;
    client.respond("PASSAGES", "Hm?");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "response.output_text.delta", response_id: "r1", delta: "Hm. Yes." });
    ws.recv({ type: "response.output_text.done", response_id: "r1", text: "Hm. Yes." });
    ws.recv({ type: "response.done", response: { id: "r1", status: "completed" } });
    await flush();
    speaker.finish(); await flush(); speaker.finish(); await flush();
    expect(order(events)).toEqual(["response-done", "playback-drained"]);
  });
});
