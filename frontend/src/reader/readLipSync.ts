/**
 * Read-mode lip-sync (#80, always on since #92): pure decisions for ReaderEngine and the Reader page.
 *
 * Each TTS chunk being read gets a Boson avatar clip of its audio (the same stream, start gate and drift control as
 * Talkback: talk/clipVideo, talk/startGate, talk/faceSync). The server lip-syncs the face BOUND TO THE VOICE it is
 * given, so a chunk can only be lip-synced on a face that is bound to a voice; the voice sent is the chunk's own when
 * its reader's face is bound to it, else the narrator's when the narrator's face is bound to the narrator's voice
 * (the narrator's face then reads that chunk, loop and clip alike).
 */

import type { Face } from "../api";

/** Clips are opened at least this many chunks ahead of the one playing: enough to hide Boson's ~2 s start behind a
 * normal chunk, few enough to keep renders in flight low (the server locks per clip and rate-limits renders). */
export const READ_CLIP_AHEAD = 1;
/** Short chunks (#104): more clips open ahead, until the next unopened clip has this much speech as head start
 * (Boson's first fragment takes ~1.5-3 s, then ~0.8 s must be buffered before the voice starts with it)... */
export const READ_CLIP_LEAD_S = 4;
/** ...but never more than this many ahead. */
export const READ_CLIP_AHEAD_MAX = 3;
/** The first word after Play waits this long at most for its clip; past that the talk loop starts with the voice. */
export const READ_START_WAIT_MS = 6000;
/** Between chunks the voice waits less: the next clip is normally prefetched. Was 1 s, which short chunks overran
 * (their next clip had ~1 s of head start), so the voice started before its face (#104). */
export const READ_CHAIN_WAIT_MS = 3000;

/**
 * How many clips to hold ahead of chunk `i` (at least `min`, at most `max`): one more while the speech from the start
 * of chunk `i` to the start of the next clip held is shorter than `leadS`. `speechS(k)` is chunk k's speech length.
 */
export function clipAheadFor(i: number, last: number, speechS: (k: number) => number,
  { min = READ_CLIP_AHEAD, max = READ_CLIP_AHEAD_MAX, leadS = READ_CLIP_LEAD_S } = {}): number {
  let ahead = min;
  let lead = 0;
  for (let k = i; k < i + ahead; k++) lead += speechS(k);
  while (ahead < max && i + ahead < last && lead < leadS) { lead += speechS(i + ahead); ahead++; }
  return ahead;
}

/**
 * Lip-sync is always on in read mode (#92) when there is audio, at every speed (#105): the clip is rendered at the
 * voice's natural pace and played at the voice's playbackRate (talk/faceSync), so it stays in step at 0.75x-1.5x too.
 */
export const readClipsOn = (audioAvailable: boolean): boolean => audioAvailable;

/** Seconds of speech in a chunk's text (the clip is as long as the audio), for the spend estimate before metadata. */
export const speechSeconds = (ttsText: string): number => Math.max(1, ttsText.length / 15);

/** Chunks whose clips are held while chunk `i` plays: [i, i + ahead], clamped to the book. */
export function clipWindow(i: number, ahead: number, last: number): [number, number] {
  const a = Math.max(0, Math.min(i, last));
  return [a, Math.max(a, Math.min(a + ahead, last))];
}

export type ReadFaces = {
  faceFor: (speaker: string) => Face | null | undefined; // the reader's own voice -> face resolution (what the Stage shows)
  voiceFor: (speaker: string) => string;                 // the reading voice of a speaker
};

/** The face shown while a chunk by `speaker` is read: the speaker's own face if they have one, else the narrator's. */
export const readFaceFor = (faces: ReadFaces, speaker: string): Face | null =>
  faces.faceFor(speaker) ?? faces.faceFor("narrator") ?? null;

/**
 * The voice whose bound face lip-syncs a chunk by `speaker` read in `chunkVoice`, or null when no lip-sync is possible
 * (the face shown is not bound to the voice that speaks). The clip must match the face on screen (readFaceFor).
 */
export function lipSyncVoiceFor(faces: ReadFaces, speaker: string, chunkVoice: string | undefined): string | null {
  const own = faces.faceFor(speaker);
  if (own) return chunkVoice && own.voice === chunkVoice ? chunkVoice : null;
  const narrator = faces.faceFor("narrator");
  const narratorVoice = faces.voiceFor("narrator");
  return narrator && narrator.voice === narratorVoice ? narratorVoice : null;
}
