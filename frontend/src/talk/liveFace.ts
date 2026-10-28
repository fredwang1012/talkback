/**
 * Live face (#140): one of Boson's preset avatars rendered live into a LiveKit Cloud room.
 *
 * The browser joins the room as the avatar's publisher and streams each reply's audio to it, 24 kHz 16-bit mono PCM,
 * over the LiveKit Agents avatar audio protocol: a byte stream on topic `lk.audio_stream` (one stream per spoken segment,
 * closed to end it) and the RPC `lk.clear_buffer` to cut it off. The avatar publishes its own video and audio tracks,
 * so the voice the reader hears and the lips they see come from one stream and stay in sync. It reports playback with
 * the RPCs `lk.playback_started` / `lk.playback_finished`, which drive `onPlaying` and end `endSegment`.
 *
 * Only Boson's presets can be live (there is no API to make an avatar from a photo): faces of our own keep the
 * per-sentence lip-sync clips. When a session cannot start (no slot free, LiveKit unreachable) the caller keeps the clips.
 */
// types only: livekit-client is loaded when a live face starts, so the reader page does not carry it
import type { ByteStreamWriter, RemoteParticipant, RemoteTrack, RemoteTrackPublication, RpcInvocationData } from "livekit-client";
import { startLiveAvatar, stopLiveAvatar, type LiveAvatarSession } from "../api";

export const AUDIO_STREAM_TOPIC = "lk.audio_stream";
export const RPC_CLEAR_BUFFER = "lk.clear_buffer";
export const RPC_PLAYBACK_STARTED = "lk.playback_started";
export const RPC_PLAYBACK_FINISHED = "lk.playback_finished";
export const LIVE_RATE = 24000;
// livekit-client's RoomEvent and Track.Kind values (string enums), spelled out to avoid loading the library here
const EV_TRACK_SUBSCRIBED = "trackSubscribed";
const EV_PARTICIPANT_DISCONNECTED = "participantDisconnected";
const EV_DISCONNECTED = "disconnected";

/** What the face needs from LiveKit; the real Room in the app, a fake in tests. */
export interface RoomLike {
  connect(url: string, token: string): Promise<void>;
  disconnect(): Promise<void>;
  on(event: string, fn: (...args: any[]) => void): unknown;
  registerRpcMethod(method: string, fn: (d: RpcInvocationData) => Promise<string>): void;
  localParticipant: {
    streamBytes(opts: { name?: string; topic?: string; destinationIdentities?: string[]; attributes?: Record<string, string> }): Promise<Pick<ByteStreamWriter, "write" | "close">>;
    performRpc(p: { destinationIdentity: string; method: string; payload: string }): Promise<string>;
  };
}

export type LiveMedia = { video: MediaStreamTrack | null; audio: MediaStreamTrack | null };

export class LiveFace {
  onPlaying: (on: boolean) => void = () => {};
  onMedia: (m: LiveMedia) => void = () => {};
  onEnded: () => void = () => {};
  private writer: Promise<Pick<ByteStreamWriter, "write" | "close">> | null = null;
  private chain: Promise<unknown> = Promise.resolve();
  private finished: (() => void)[] = []; // endSegment waiters, oldest first
  private media: LiveMedia = { video: null, audio: null };
  private closed = false;
  private seq = 0;
  playingSince: number | null = null; // when the current segment's sound started (playedMs)

  private room: RoomLike;
  readonly session: LiveAvatarSession;
  private now: () => number;

  constructor(room: RoomLike, session: LiveAvatarSession, now: () => number = () => Date.now()) {
    this.room = room;
    this.session = session;
    this.now = now;
  }

  /** Starts a session for a preset avatar and joins its room. Throws `LiveUnavailable` when the caller should keep clips. */
  static async start(avatarId: string, makeRoom?: () => RoomLike): Promise<LiveFace> {
    const session = await startLiveAvatar(avatarId);
    const room = makeRoom ? makeRoom() : new (await import("livekit-client")).Room() as unknown as RoomLike;
    const face = new LiveFace(room, session);
    try {
      await face.join();
    } catch (e) {
      void stopLiveAvatar(session.session).catch(() => {});
      throw e;
    }
    return face;
  }

  /** Joins the room and listens for the avatar's tracks and playback reports. */
  async join(): Promise<void> {
    const avatar = this.session.avatar_identity;
    const fromAvatar = (d: RpcInvocationData) => d.callerIdentity === avatar;
    this.room.registerRpcMethod(RPC_PLAYBACK_STARTED, async (d) => {
      if (!fromAvatar(d)) return "reject";
      this.playingSince = this.now();
      this.onPlaying(true);
      return "ok";
    });
    this.room.registerRpcMethod(RPC_PLAYBACK_FINISHED, async (d) => {
      if (!fromAvatar(d)) return "reject";
      this.playingSince = null;
      this.onPlaying(false);
      this.finished.shift()?.();
      return "ok";
    });
    this.room.on(EV_TRACK_SUBSCRIBED, (track: RemoteTrack, _pub: RemoteTrackPublication, who: RemoteParticipant) => {
      if (who.identity !== avatar) return;
      if (track.kind === "video") this.media = { ...this.media, video: track.mediaStreamTrack };
      else if (track.kind === "audio") this.media = { ...this.media, audio: track.mediaStreamTrack };
      this.onMedia(this.media);
    });
    this.room.on(EV_PARTICIPANT_DISCONNECTED, (who: RemoteParticipant) => { if (who.identity === avatar) this.end(); });
    this.room.on(EV_DISCONNECTED, () => this.end());
    await this.room.connect(this.session.url, this.session.token);
  }

  get mediaNow(): LiveMedia { return this.media; }
  get alive(): boolean { return !this.closed; }

  /** Queues PCM16 (24 kHz mono) for the current segment; the first write opens the segment's byte stream. */
  write(pcm: Uint8Array): void {
    if (this.closed || !pcm.length) return;
    if (!this.writer) {
      this.writer = this.room.localParticipant.streamBytes({
        name: `AUDIO_${++this.seq}`, topic: AUDIO_STREAM_TOPIC, destinationIdentities: [this.session.avatar_identity],
        attributes: { sample_rate: String(LIVE_RATE), num_channels: "1" },
      });
    }
    const w = this.writer;
    this.chain = this.chain.then(async () => (await w).write(pcm)).catch(() => {});
  }

  /** Ends the segment (closes its stream). Resolves when the avatar reports it played it all (or was cleared). */
  endSegment(): Promise<void> {
    const w = this.writer;
    if (!w || this.closed) return Promise.resolve();
    this.writer = null;
    const done = new Promise<void>((resolve) => this.finished.push(resolve));
    this.chain = this.chain.then(async () => (await w).close()).catch(() => {});
    return done;
  }

  /** Barge-in: the avatar drops what it has not played; every waiter is released. */
  clear(): void {
    const w = this.writer;
    this.writer = null;
    if (w) this.chain = this.chain.then(async () => (await w).close()).catch(() => {});
    if (!this.closed) {
      void this.room.localParticipant.performRpc({ destinationIdentity: this.session.avatar_identity, method: RPC_CLEAR_BUFFER, payload: "" }).catch(() => {});
    }
    this.releaseWaiters();
  }

  /** Milliseconds of the current segment heard so far (0 when nothing plays). */
  playedMs(): number { return this.playingSince === null ? 0 : Math.max(0, this.now() - this.playingSince); }

  /** Leaves the room and releases the Boson session (its slot is billed while held). */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.releaseWaiters();
    await this.room.disconnect().catch(() => {});
    await stopLiveAvatar(this.session.session).catch(() => {});
  }

  private end(): void {
    if (this.closed) return;
    this.closed = true;
    this.releaseWaiters();
    this.onPlaying(false);
    this.onEnded();
  }

  private releaseWaiters(): void {
    const w = this.finished.splice(0);
    w.forEach((f) => f());
    this.playingSince = null;
  }
}

/** Base64 PCM (a Realtime audio delta) to bytes. */
export function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Float samples (-1..1) to 16-bit little-endian PCM bytes. */
export function floatToPcm16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return out;
}

/** An answer clip (MP3 from /api/speak) decoded and resampled to 24 kHz mono PCM16 for the avatar. */
export async function clipToPcm(url: string): Promise<Uint8Array> {
  const buf = await (await fetch(url)).arrayBuffer();
  const ctx = new OfflineAudioContext(1, 1, LIVE_RATE);
  const audio = await ctx.decodeAudioData(buf); // resampled to the context's 24 kHz
  let mono = audio.getChannelData(0);
  if (audio.numberOfChannels > 1) {
    mono = new Float32Array(audio.length);
    for (let c = 0; c < audio.numberOfChannels; c++) {
      const ch = audio.getChannelData(c);
      for (let i = 0; i < ch.length; i++) mono[i] += ch[i] / audio.numberOfChannels;
    }
  }
  return floatToPcm16(mono);
}
