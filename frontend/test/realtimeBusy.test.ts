/**
 * The 1.5 preview serves about 2 generations at once per key (Boson feedback #12): the next response.create fails with
 * `error {code: "upstream_error"}` and a `response.done`. The answer is asked for once more after a short wait; a
 * second failure is reported once and the next session uses the stable model.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BUSY_RETRY_MS, REALTIME_MODEL_STABLE, TalkClient, realtimeModel, resetRealtimeFallback,
  type MicLike, type PlayerLike, type SocketLike,
} from "../src/talk/TalkClient";
import type { SessionEvent } from "../src/talk/TalkController";

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
  creates() { return this.sent.filter((e) => e.type === "response.create"); }
}
const player: PlayerLike = { resume: async () => {}, enqueueBase64() {}, flush: async () => 0, reset() {}, close: async () => {} };
const mic: MicLike = { start: async () => {}, stop: async () => {} };

async function openClient() {
  const sockets: FakeSocket[] = [];
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player, createMic: () => mic, cancelWaitMs: 20,
  });
  const events: SessionEvent[] = [];
  client.on((e) => events.push(e));
  const p = client.ensureOpen("marcus");
  await Promise.resolve(); await Promise.resolve();
  await vi.advanceTimersByTimeAsync(0);
  sockets[0].open();
  await p;
  sockets[0].recv({ type: "session.created", session: {} });
  return { client, ws: sockets[0], events };
}
const busy = (ws: FakeSocket, id: string) => {
  ws.recv({ type: "response.created", response: { id } });
  ws.recv({ type: "error", error: { type: "server_error", code: "upstream_error", message: "upstream error" } });
  ws.recv({ type: "response.done", response: { id, status: "failed" } });
};
const errors = (events: SessionEvent[]) => events.filter((e) => e.type === "error");

beforeEach(() => { vi.useFakeTimers(); resetRealtimeFallback(); });
afterEach(() => { vi.useRealTimers(); resetRealtimeFallback(); });

describe("busy 1.5 preview (upstream_error)", () => {
  it("asks for the same answer again once, with no error shown", async () => {
    const { client, ws, events } = await openClient();
    client.respond("PASSAGES", "Who is Hagrid?");
    expect(ws.creates()).toHaveLength(1);
    busy(ws, "r1");
    expect(ws.creates()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(BUSY_RETRY_MS);
    expect(ws.creates()).toHaveLength(2);
    expect(ws.creates()[1]).toEqual(ws.creates()[0]);
    expect(errors(events)).toHaveLength(0);
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed" } });
    expect(errors(events)).toHaveLength(0);
    expect(realtimeModel("")).not.toBe(REALTIME_MODEL_STABLE);
  });

  it("a second failure is reported once and the next session uses the stable model", async () => {
    const { client, ws, events } = await openClient();
    client.respond("PASSAGES", "Who is Hagrid?");
    busy(ws, "r1");
    await vi.advanceTimersByTimeAsync(BUSY_RETRY_MS);
    busy(ws, "r2");
    await vi.advanceTimersByTimeAsync(BUSY_RETRY_MS * 2);
    expect(ws.creates()).toHaveLength(2);
    expect(errors(events)).toHaveLength(1);
    expect(realtimeModel("")).toBe(REALTIME_MODEL_STABLE);
  });

  it("a new question starts with a fresh retry", async () => {
    const { client, ws } = await openClient();
    client.respond("PASSAGES", "First?");
    busy(ws, "r1");
    await vi.advanceTimersByTimeAsync(BUSY_RETRY_MS);
    ws.recv({ type: "response.created", response: { id: "r2" } });
    ws.recv({ type: "response.done", response: { id: "r2", status: "completed" } });
    client.respond("PASSAGES", "Second?");
    busy(ws, "r3");
    await vi.advanceTimersByTimeAsync(BUSY_RETRY_MS);
    expect(ws.creates()).toHaveLength(4);
  });

  it("other errors still show at once", async () => {
    const { client, ws, events } = await openClient();
    client.respond("PASSAGES", "Who?");
    ws.recv({ type: "response.created", response: { id: "r1" } });
    ws.recv({ type: "error", error: { type: "invalid_request_error", code: "bad", message: "nope" } });
    expect(errors(events)).toHaveLength(1);
  });
});
