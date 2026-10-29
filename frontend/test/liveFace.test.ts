/**
 * Live face (#140): the LiveKit Agents avatar audio protocol, against a fake room. Audio goes to the avatar as one byte
 * stream per segment (topic lk.audio_stream, 24 kHz mono attributes), a segment ends when its stream closes and the
 * avatar reports lk.playback_finished, and a barge-in sends lk.clear_buffer.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { FINISH_SLACK_MS, JOIN_WAIT_MS, LiveFace, RPC_CLEAR_BUFFER, RPC_PLAYBACK_FINISHED, RPC_PLAYBACK_STARTED, START_GRACE_MS, b64ToBytes, floatToPcm16, type RoomLike } from "../src/talk/liveFace";
import { LivePlayer, RoutedPlayer, createLiveSpeaker, createRoutedSpeaker } from "../src/talk/livePlayers";
import type { PlayerLike } from "../src/talk/TalkClient";
import type { SentenceSpeaker } from "../src/talk/sentencePlayer";
import * as api from "../src/api";

vi.mock("../src/api", () => {
  class LiveUnavailable extends Error {}
  return { LiveUnavailable, startLiveAvatar: vi.fn(), stopLiveAvatar: vi.fn(async () => {}) };
});
afterEach(() => { vi.useRealTimers(); vi.mocked(api.stopLiveAvatar).mockClear(); });

class FakeRoom implements RoomLike {
  rpc = new Map<string, (d: any) => Promise<string>>();
  handlers = new Map<string, ((...a: any[]) => void)[]>();
  streams: { opts: any; chunks: Uint8Array[]; closed: boolean }[] = [];
  rpcs: { destinationIdentity: string; method: string }[] = [];
  connected = "";
  remoteParticipants = new Map<string, unknown>([["boson-avatar", {}]]);
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
    p.reset();
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

describe("review fixes", () => {
  it("a segment whose finished report never comes ends after its audio length plus slack", async () => {
    vi.useFakeTimers();
    const { face } = await joined();
    const seen: boolean[] = [];
    face.onPlaying = (on) => seen.push(on);
    face.write(new Uint8Array(48000)); // 1 s of 24 kHz PCM16
    let done = false;
    void face.endSegment().then(() => { done = true; });
    await vi.advanceTimersByTimeAsync(START_GRACE_MS);
    expect(seen).toEqual([true]); // no lk.playback_started: assumed after the grace
    await vi.advanceTimersByTimeAsync(1000 + FINISH_SLACK_MS - START_GRACE_MS - 1);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(2);
    expect(done).toBe(true);
    expect(seen).toEqual([true, false]);
  });

  it("an avatar that never joins: the start fails as LiveUnavailable and the session is released", async () => {
    vi.useFakeTimers();
    vi.mocked(api.startLiveAvatar).mockResolvedValueOnce(SESSION);
    const room = new FakeRoom();
    room.remoteParticipants.clear();
    const started = LiveFace.start("builtin_claire_v1", () => room).catch((e) => e);
    await vi.advanceTimersByTimeAsync(JOIN_WAIT_MS + 1);
    const err = await started;
    expect(err).toBeInstanceOf(api.LiveUnavailable);
    expect(api.stopLiveAvatar).toHaveBeenCalledWith("avrt_1");
  });

  it("a failure making the room after Boson started the session still releases it", async () => {
    vi.mocked(api.startLiveAvatar).mockResolvedValueOnce(SESSION);
    await expect(LiveFace.start("builtin_claire_v1", () => { throw new Error("chunk load failed"); })).rejects.toThrow("chunk load failed");
    expect(api.stopLiveAvatar).toHaveBeenCalledWith("avrt_1");
  });

  it("the avatar leaving releases the server's record too, once", async () => {
    const { room, face } = await joined();
    let ended = 0;
    face.onEnded = () => { ended++; };
    room.handlers.get("participantDisconnected")![0]({ identity: "boson-avatar" });
    room.handlers.get("disconnected")![0]();
    expect(ended).toBe(1);
    expect(api.stopLiveAvatar).toHaveBeenCalledTimes(1);
    expect(face.alive).toBe(false);
  });

  it("closing on page hide asks for a keepalive release", async () => {
    const { face } = await joined();
    await face.close({ keepalive: true });
    expect(api.stopLiveAvatar).toHaveBeenCalledWith("avrt_1", { keepalive: true });
  });

  it("RoutedPlayer picks the route per reply, never mid-reply, and live replies cannot be paused", async () => {
    const { room, face } = await joined();
    let live: LiveFace | null = null;
    const local = { enq: [] as string[], resume: async () => {}, enqueueBase64(b: string) { this.enq.push(b); }, flush: async () => 5,
      reset() {}, close: async () => {}, pausePlayback: async () => {} } as unknown as PlayerLike & { enq: string[] };
    const p = new RoutedPlayer(() => live, local, () => {});
    p.reset();
    p.enqueueBase64("QQ==");
    expect(local.enq).toEqual(["QQ=="]);
    expect(p.canPause).toBe(true);
    live = face; // the face started mid-reply: this reply stays local
    p.enqueueBase64("Qg==");
    expect(local.enq).toEqual(["QQ==", "Qg=="]);
    p.reset(); // next reply
    expect(p.live).toBe(true);
    expect(p.canPause).toBe(false);
    p.enqueueBase64(btoa("	"));
    p.endTurn();
    await tick();
    expect(room.streams).toHaveLength(1);
    live = null;
    p.reset();
    expect(p.live).toBe(false);
  });

  it("createRoutedSpeaker speaks through the face while one runs, else locally; pause only locally", async () => {
    const { face } = await joined();
    let live: LiveFace | null = null;
    const played: string[] = [];
    const local: SentenceSpeaker = { prefetch() {}, play: async (t: string) => { played.push(`local:${t}`); }, stop() {}, pause() {}, resume: () => true };
    const s = createRoutedSpeaker(() => live, () => ({ prefetch() {}, play: async (t: string) => { played.push(`live:${t}`); }, stop() {} }), local);
    await s.play("One.");
    expect(s.canPause).toBe(true);
    live = face;
    await s.play("Two.");
    expect(s.canPause).toBe(false);
    expect(played).toEqual(["local:One.", "live:Two."]);
  });
});
