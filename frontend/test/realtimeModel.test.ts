/** The Realtime model id: the socket URL and the session.update must name the same model (Boson binds the session to it). */
import { describe, expect, it, vi } from "vitest";
import { TalkClient, sessionConfig, type MicLike, type PlayerLike, type SocketLike } from "../src/talk/TalkClient";

const MODEL = "higgs-realtime-1.5-preview";

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
}
const player: PlayerLike = { resume: async () => {}, enqueueBase64() {}, flush: async () => 0, reset() {}, close: async () => {} };
const mic: MicLike = { start: vi.fn(async () => {}), stop: vi.fn(async () => {}) };

describe("Realtime model id", () => {
  it("sessionConfig names the 1.5 preview", () => {
    expect(sessionConfig("oliver").session).toMatchObject({ type: "realtime", model: MODEL });
  });

  it("the socket URL and the first session.update carry the same model", async () => {
    const sockets: FakeSocket[] = [];
    const client = new TalkClient({
      tokenProvider: async () => "bai-eph-1",
      createSocket: (url, protocols) => { const s = new FakeSocket(url, protocols); sockets.push(s); return s; },
      createPlayer: () => player,
      createMic: () => mic,
    });
    const p = client.ensureOpen("oliver");
    await new Promise((r) => setTimeout(r, 0));
    sockets[0].open();
    await p;
    expect(sockets[0].url).toBe(`wss://api.boson.ai/v1/realtime?model=${MODEL}`);
    expect(sockets[0].protocols).toEqual(["realtime", "bai-client-secret.bai-eph-1"]);
    expect(sockets[0].sent[0]).toMatchObject({ type: "session.update", session: { model: MODEL } });
  });
});
