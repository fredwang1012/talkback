/**
 * #80: download a finished character answer as one MP4. The server joins the answer's cached per-sentence lip-sync
 * clips (POST /api/clip/answer); an answer without a face (or whose sentence videos were never all made) becomes its
 * cached voice over a still picture: the portrait, or the letter circle, drawn here and sent as raw I420 pixels.
 * Never a paid render: the server answers 409 for missing clips and the still version is made instead.
 */
import { authHeaders, timedFetch } from "../api";
import type { ReplayItem } from "./replay";

/** The still picture's side in pixels; must match the server's STILL_SIDE. */
export const STILL_SIDE = 320;
const TTS_URL = /^\/media\/tts\/[0-9a-f]{64}\.mp3$/;

export type ClipSentence = { audio_url: string; voice: string; face_id?: string };
export type ClipPlan = { sentences: ClipSentence[]; faced: boolean };

/**
 * What a download needs from a stored reply: its sentences' cached audio (and the voice and face each was lip-synced
 * with). Null when the reply cannot be downloaded: a Realtime voice reply (raw audio, no cached sentences), or none.
 */
export function clipPlan(item: ReplayItem | undefined): ClipPlan | null {
  if (!item || item.kind !== "sentences" || !item.parts.length) return null;
  if (!item.parts.every((p) => TTS_URL.test(p.clip.audio_url))) return null;
  const faced = item.parts.every((p) => p.clip.lipsync);
  return {
    faced,
    sentences: item.parts.map((p) => (faced && p.clip.lipsync
      ? { audio_url: p.clip.audio_url, voice: p.clip.lipsync.voice, face_id: p.clip.lipsync.faceId }
      : { audio_url: p.clip.audio_url, voice: "" })),
  };
}

/** RGBA pixels (canvas getImageData) as I420 (BT.601 limited range, chroma averaged over 2x2), what the server codes. */
export function rgbaToI420(rgba: Uint8ClampedArray | Uint8Array, w: number, h: number): Uint8Array {
  const out = new Uint8Array(w * h * 3 / 2);
  const cw = w / 2;
  const uAt = w * h;
  const vAt = uAt + cw * (h / 2);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      out[y * w + x] = Math.round(16 + (65.738 * rgba[i] + 129.057 * rgba[i + 1] + 25.064 * rgba[i + 2]) / 256);
    }
  }
  for (let y = 0; y < h / 2; y++) {
    for (let x = 0; x < cw; x++) {
      let r = 0, g = 0, b = 0;
      for (const [dx, dy] of [[0, 0], [1, 0], [0, 1], [1, 1]]) {
        const i = ((2 * y + dy) * w + 2 * x + dx) * 4;
        r += rgba[i]; g += rgba[i + 1]; b += rgba[i + 2];
      }
      r /= 4; g /= 4; b /= 4;
      out[uAt + y * cw + x] = Math.round(128 + (-37.945 * r - 74.494 * g + 112.439 * b) / 256);
      out[vAt + y * cw + x] = Math.round(128 + (112.439 * r - 94.154 * g - 18.285 * b) / 256);
    }
  }
  return out;
}

export function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** The still: the face or portrait image (cover-cropped into a circle), else the speaker's initial in a circle. */
export async function drawStill(image: string | null, letter: string, side = STILL_SIDE): Promise<Uint8Array> {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = side;
  const g = canvas.getContext("2d");
  if (!g) throw new Error("This browser cannot draw the picture.");
  g.fillStyle = "#1c1a17";
  g.fillRect(0, 0, side, side);
  g.save();
  g.beginPath();
  g.arc(side / 2, side / 2, side * 0.44, 0, Math.PI * 2);
  g.clip();
  let drawn = false;
  if (image) {
    try {
      const img = new Image();
      img.src = image;
      await img.decode();
      const s = Math.min(img.naturalWidth, img.naturalHeight);
      g.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, side, side);
      drawn = true;
    } catch { /* the letter instead */ }
  }
  if (!drawn) {
    g.fillStyle = "#2b2721";
    g.fillRect(0, 0, side, side);
    g.fillStyle = "#c9a45c";
    g.font = `${Math.round(side * 0.42)}px Georgia, serif`;
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(letter.toUpperCase(), side / 2, side / 2 + side * 0.02);
  }
  g.restore();
  return rgbaToI420(g.getImageData(0, 0, side, side).data, side, side);
}

export class ClipError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

/** Used only when the server's Content-Disposition carries no file name. */
const FALLBACK_NAME = "talkback-answer.mp4";

/** The file name in a Content-Disposition header (the server picks it), or null. */
export function dispositionName(header: string | null): string | null {
  const m = header?.match(/filename="([^"]+)"/) ?? header?.match(/filename=([^;\s]+)/);
  return m ? m[1] : null;
}

export type AnswerClip = { blob: Blob; name: string };

/** POST /api/clip/answer; the MP4 and the file name the server gave it. */
export async function fetchAnswerClip(body: { book_id: string; speaker: string; sentences: ClipSentence[]; still?: string }): Promise<AnswerClip> {
  const res = await timedFetch("/api/clip/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  }, 60000);
  if (!res.ok) {
    const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
    throw new ClipError(typeof detail === "string" ? detail : `Could not make the clip (${res.status})`, res.status);
  }
  return { blob: await res.blob(), name: dispositionName(res.headers.get("Content-Disposition")) ?? FALLBACK_NAME };
}

/** Why a faced answer could not be joined and gets the still version instead (by the server's status), or none:
 * then the error is reported (busy, rate limited, too long: the still would fail the same way). */
const STILL_INSTEAD: Record<number, string> = {
  409: "Some of this answer's face videos were never made",
  404: "This answer's face is no longer available",
  422: "This answer's face videos could not be joined into one",
};

/**
 * The MP4 of one answer: its lip-synced video when every sentence's clip is cached and joinable, else (no face, or a
 * clip missing, its face deleted, or clips that do not join: `note` says so) its voice over the still. `still` draws
 * the picture only when it is needed.
 */
export async function makeAnswerClip(
  bookId: string, speaker: string, plan: ClipPlan, still: () => Promise<Uint8Array>, fetchClip = fetchAnswerClip,
): Promise<AnswerClip & { note: string | null }> {
  const base = { book_id: bookId, speaker, sentences: plan.sentences };
  let note: string | null = null;
  if (plan.faced) {
    try {
      return { ...await fetchClip(base), note };
    } catch (e) {
      const why = e instanceof ClipError ? STILL_INSTEAD[e.status] : undefined;
      if (!why) throw e;
      note = `${why}, so the clip shows the still picture (nothing was rendered or charged).`;
    }
  }
  const sentences = plan.sentences.map((s) => ({ audio_url: s.audio_url, voice: s.voice }));
  return { ...await fetchClip({ ...base, sentences, still: toBase64(await still()) }), note };
}

/** Save a Blob as a file through a temporary link. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
