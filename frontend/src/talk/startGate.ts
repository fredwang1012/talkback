/**
 * The lip-sync start gate, shared by Talkback (sentencePlayer) and read mode (ReaderEngine).
 *
 * The voice waits for its clip before the first word, at most `startWaitMs`: when the clip's stream delivers its first
 * fragment it is joined to the sync (faceSync), and the gate opens when the sync says the clip is buffered enough to
 * start with the voice from 0. Both then start together and nothing has to be moved later. If the wait runs out, the
 * gate opens anyway and the clip joins late when it arrives (the sync rejoins it by rendezvous; the talk loop shows
 * until then). A clip that never comes (no face for this voice, render failed) opens the gate at once.
 *
 * The caller starts the voice when `ready` resolves and calls `cancel` when it stops caring (the sentence was stopped
 * or left): a clip that arrives after that is not joined, and the caller releases it.
 */

import type { AudioLike, ClipSync, VideoClip } from "./sentencePlayer";

export type GateOutcome = "ready" | "timeout" | "none" | "cancelled";
export type ClipGate = { ready: Promise<GateOutcome>; cancel: () => void };

type Timers = { setTimeout: (fn: () => void, ms: number) => unknown; clearTimeout: (h: unknown) => void };
const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export function gateVoiceOnClip(
  audio: AudioLike, video: Promise<VideoClip | null>, sync: ClipSync, startWaitMs: number, timers: Timers = realTimers,
): ClipGate {
  let over = false;
  let opened = false;
  let resolve: (o: GateOutcome) => void = () => {};
  const ready = new Promise<GateOutcome>((r) => { resolve = r; });
  let cap: unknown = timers.setTimeout(() => { cap = null; open("timeout"); }, startWaitMs);
  const open = (o: GateOutcome) => {
    if (opened) return;
    opened = true;
    if (cap !== null) { timers.clearTimeout(cap); cap = null; }
    resolve(o);
  };
  void video.then((v) => {
    if (over) return; // cancelled: the caller releases the clip
    if (!v) { open("none"); return; } // no clip for this voice, or the render failed: the talk loop stands in
    const joined = sync.join(audio, v, opened); // late: the voice was already told it may start
    if (!opened) void Promise.resolve(joined).then(() => open("ready"));
  }, () => open("none"));
  return { ready, cancel: () => { over = true; open("cancelled"); } };
}
