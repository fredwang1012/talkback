/**
 * Live face (#140): the LiveKit Agents avatar audio protocol, against a fake room. Audio goes to the avatar as one byte
 * stream per segment (topic lk.audio_stream, 24 kHz mono attributes), a segment ends when its stream closes and the
 * avatar reports lk.playback_finished, and a barge-in sends lk.clear_buffer.
 */
import { describe, expect, it, vi } from "vitest";
import { LiveFace, RPC_CLEAR_BUFFER, RPC_PLAYBACK_FINISHED, RPC_PLAYBACK_STARTED, b64ToBytes, floatToPcm16, type RoomLike } from "../src/talk/liveFace";
import { LivePlayer, createLiveSpeaker } from "../src/talk/livePlayers";

vi.mock("../src/api", () => ({ startLiveAvatar: vi.fn(), stopLiveAvatar: vi.fn(async () => {}) }));

class FakeRoom implements RoomLike {
  rpc = new Map<string, (d: any) => Promise<string>>();
  handlers = new Map<string, ((...a: any[]) => void)[]>();
  streams: { opts: any; chunks: Uint8Array[]; closed: boolean }[] = [];
  rpcs: { destinationIdentity: string; method: string }[] = [];
  connected = "";
  async connect(url: string, token: string) { this.connected = `${url}|${token}`; }
  async disconnect() { this.connected = ""; }
  on(ev: string, fn: (...a: any[]) => void) { this.handlers.set(ev, [...(this.handlers.get(ev) ?? []), fn]); return this; }
  registerRpcMethod(m: string, fn: (d: any) => Promise<string>) { this.rpc.set(m, fn); }
  localParticipant = {
    streamBytes: async (opts: any) => {
      const s = { opts, chunks: [] as Uint8Array[], closed: false };
      this.streams.push(s);
      return { write: async (c: Uint8Array) => { s.chunks.push(c); }, close: async () => { s.closed = true; } };
    },
    performRpc: async (p: any) => { this.rpcs.push(p); return "ok"; },
  };
  avatar(method: string, from = "boson-avatar") { return this.rpc.get(method)!({ callerIdentity: from, payload: "{}", requestId: "1", responseTimeout: 1000 }); }
}
const SESSION = { session: "avrt_1", url: "wss://x.livekit.cloud", token: "tok", identity: "reader-1", avatar_identity: "boson-avatar", max_seconds: 900 };
const tick = () => new Promise((r) => setTimeout(r, 0));

async function joined(now = () => 1000) {
  const room = new FakeRoom();
  const face = new LiveFace(room, SESSION, now);
  await face.join();
  return { room, face };
}

describe("LiveFace", () => {
  it("joins with the session's url and token", async () => {
    const { room } = await joined();
    expect(room.connected).toBe("wss://x.livekit.cloud|tok");
  });

  it("one byte stream per segment, to the avatar only, with the 24 kHz mono attributes", async () => {
    const { room, face } = await joined();
    face.write(new Uint8Array([1, 2]));
    face.write(new Uint8Array([3, 4]));
    const done = face.endSegment();
    await tick();
    expect(room.streams).toHaveLength(1);
    expect(room.streams[0].opts).toMatchObject({ topic: "lk.audio_stream", destinationIdentities: ["boson-avatar"],
      attributes: { sample_rate: "24000", num_channels: "1" } });
    expect(room.streams[0].chunks.map((c) => [...c])).toEqual([[1, 2], [3, 4]]);
    expect(room.streams[0].closed).toBe(true);
    let resolved = false;
    void done.then(() => { resolved = true; });
    await tick();
    expect(resolved).toBe(false); // waits for the avatar to finish playing
    await room.avatar(RPC_PLAYBACK_FINISHED);
    await tick();
    expect(resolved).toBe(true);
    face.write(new Uint8Array([5]));
    await tick();
    expect(room.streams).toHaveLength(2); // the next segment gets a new stream
  });

  it("playback reports drive onPlaying and playedMs; other participants are ignored", async () => {
    let t = 1000;
    const { room, face } = await joined(() => t);
    const seen: boolean[] = [];
    face.onPlaying = (on) => seen.push(on);
    expect(await room.avatar(RPC_PLAYBACK_STARTED, "someone-else")).toBe("reject");
    await room.avatar(RPC_PLAYBACK_STARTED);
    t = 1750;
    expect(face.playedMs()).toBe(750);
    await room.avatar(RPC_PLAYBACK_FINISHED);
    expect(seen).toEqual([true, false]);
    expect(face.playedMs()).toBe(0);
  });

  it("a barge-in sends clear_buffer and releases the waiting segment", async () => {
    const { room, face } = await joined();
    face.write(new Uint8Array([1]));
    let resolved = false;
    void face.endSegment().then(() => { resolved = true; });
    face.clear();
    await tick();
    expect(room.rpcs).toEqual([{ destinationIdentity: "boson-avatar", method: RPC_CLEAR_BUFFER, payload: "" }]);
    expect(resolved).toBe(true);
  });
});

describe("live players", () => {
  it("LivePlayer: Realtime deltas go to the avatar, endTurn closes the segment, flush clears it", async () => {
    const { room, face } = await joined();
    const speaking: boolean[] = [];
    const p = new LivePlayer(face, (s) => speaking.push(s));
    p.enqueueBase64(btoa(String.fromCharCode(7, 8)));
    p.endTurn();
    await tick();
    expect([...room.streams[0].chunks[0]]).toEqual([7, 8]);
    expect(room.streams[0].closed).toBe(true);
    await room.avatar(RPC_PLAYBACK_STARTED);
    expect(speaking).toEqual([true]);
    await p.flush();
    expect(room.rpcs.map((r) => r.method)).toEqual([RPC_CLEAR_BUFFER]);
  });

  it("createLiveSpeaker: each sentence is one segment and play waits for the avatar", async () => {
    const { room, face } = await joined();
    const decode = vi.fn(async (url: string) => new Uint8Array([url.length]));
    const speaker = createLiveSpeaker(async (text) => `/media/${text}.mp3`, face, { decode });
    speaker.prefetch("Hi.", 0);
    let ended = false;
    const play = speaker.play("Hi.").then(() => { ended = true; });
    await tick(); await tick();
    expect(decode).toHaveBeenCalledTimes(1);
    expect(room.streams).toHaveLength(1);
    expect(ended).toBe(false);
    await room.avatar(RPC_PLAYBACK_FINISHED);
    await play;
    expect(ended).toBe(true);
  });

  it("helpers: base64 and float to PCM16", () => {
    expect([...b64ToBytes(btoa("\x01\x02"))]).toEqual([1, 2]);
    expect([...floatToPcm16(new Float32Array([0, 1, -1]))]).toEqual([0, 0, 0xff, 0x7f, 0x00, 0x80]);
  });
});
