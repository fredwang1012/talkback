import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  REALTIME_MODEL, REALTIME_MODEL_STABLE, TalkClient, realtimeModel, realtimeUrl, resetRealtimeFallback, sessionConfig,
  type MicLike, type PlayerLike, type SocketLike,
} from "../src/talk/TalkClient";

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
const player: PlayerLike = { resume: async () => {}, enqueueBase64() {}, flush: async () => 0, reset() {}, close: async () => {} } as unknown as PlayerLike;
const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };
const flush = () => new Promise((r) => setTimeout(r, 0));

function setup() {
  const sockets: FakeSocket[] = [];
  const client = new TalkClient({
    tokenProvider: async () => "bai-eph-1",
    createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
    createPlayer: () => player,
    createMic: () => mic,
  });
  return { client, sockets };
}

beforeEach(() => resetRealtimeFallback());

describe("Realtime model selection", () => {
  it("defaults to the 1.5 preview", () => {
    expect(REALTIME_MODEL).toBe("higgs-realtime-1.5-preview");
    expect(realtimeModel("")).toBe(REALTIME_MODEL);
    expect(realtimeUrl(REALTIME_MODEL)).toBe("wss://api.boson.ai/v1/realtime?model=higgs-realtime-1.5-preview");
  });

  it("?rt= overrides the model for an A/B, and ignores anything that is not a Higgs Realtime id", () => {
    expect(realtimeModel("?rt=higgs-realtime")).toBe("higgs-realtime");
    expect(realtimeModel("?x=1&rt=higgs-realtime-1.5-preview")).toBe(REALTIME_MODEL);
    expect(realtimeModel("?rt=evil&x=1")).toBe(REALTIME_MODEL);
    expect(realtimeModel("?rt=")).toBe(REALTIME_MODEL);
  });

  it("the socket URL and the session.update carry the same model", async () => {
    const h = setup();
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    expect(h.sockets[0].url).toContain("model=higgs-realtime-1.5-preview");
    expect(h.sockets[0].sent[0].session.model).toBe("higgs-realtime-1.5-preview");
    expect(sessionConfig("v", [], { model: REALTIME_MODEL_STABLE }).session).toMatchObject({ model: "higgs-realtime" });
  });
});

describe("Realtime 1.5 fallback", () => {
  it("an error on the first session.update reopens on the stable model, and the page remembers", async () => {
    const h = setup();
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    h.sockets[0].recv({ type: "error", error: { type: "invalid_request_error", code: "model_not_found", message: "unknown model" } });
    await flush(); await flush();
    expect(h.sockets).toHaveLength(2);
    expect(h.sockets[1].url.endsWith("model=higgs-realtime")).toBe(true);
    h.sockets[1].open();
    await flush();
    expect(h.sockets[1].sent[0].session.model).toBe("higgs-realtime");
    // remembered for the page: the next session starts on the stable model straight away
    const h2 = setup();
    const p2 = h2.client.ensureOpen("marcus");
    await flush();
    h2.sockets[0].open();
    await p2;
    expect(h2.sockets[0].url.endsWith("model=higgs-realtime")).toBe(true);
  });

  it("a close before the session was ever acknowledged falls back for the next session", async () => {
    const h = setup();
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    h.sockets[0].close(1008, "model not allowed");
    expect(realtimeModel("")).toBe("higgs-realtime");
  });

  it("an acknowledged session or an unrelated error does not fall back; the stable model never does", async () => {
    const h = setup();
    const p = h.client.ensureOpen("marcus");
    await flush();
    h.sockets[0].open();
    await p;
    h.sockets[0].recv({ type: "session.created", session: {} });
    h.sockets[0].recv({ type: "error", error: { message: "boom" } });
    await flush();
    expect(h.sockets).toHaveLength(1);
    expect(realtimeModel("")).toBe(REALTIME_MODEL);

    resetRealtimeFallback();
    const s = setup();
    const ps = s.client.ensureOpen("marcus");
    await flush();
    s.sockets[0].open();
    await ps;
    expect(s.sockets[0].url).toContain("model=higgs-realtime-1.5-preview");
  });
});
