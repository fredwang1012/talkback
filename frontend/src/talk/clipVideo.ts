import { lipSync, lipSyncStream } from "../api";
import { chooseLipSyncMode, openAvatarStream } from "./avatarStream";
import type { Clip, VideoClip } from "./sentencePlayer";

/**
 * The lip-sync video of one sentence or chunk: the face shown (`faceId`, #113) speaking that cached TTS audio, which
 * `voice` spoke. Streamed when the browser can, else a plain clip URL. Cached clips come back at once and cost
 * nothing; `onRender` is called when the server starts a fresh (paid) render.
 */
export async function openClipVideo(audioUrl: string, voice: string, faceId: string | null, onRender?: () => void): Promise<VideoClip> {
  if (chooseLipSyncMode() === "stream") {
    const c = await openAvatarStream({
      post: async (signal) => {
        const res = await lipSyncStream(audioUrl, voice, faceId, signal);
        if (res.ok && !(res.headers.get("content-type") ?? "").includes("json")) onRender?.();
        return res;
      },
      MediaSourceCtor: MediaSource, createObjectURL: (o) => URL.createObjectURL(o), revokeObjectURL: (u) => URL.revokeObjectURL(u),
    });
    return { url: c.url, release: c.release };
  }
  return { url: await lipSync(audioUrl, voice, faceId), release: () => {} };
}

/**
 * A spoken sentence as a clip: with a lip-sync video whenever a real face is shown (`faceId`; any voice x face pair
 * lip-syncs since #113), else audio only, and the talk loop or still face stands in. A render that fails is dropped
 * by the speaker (sentencePlayer), so the voice never waits on it for long.
 */
export function lipSyncClip(audioUrl: string, voice: string, faceId: string | null, open = openClipVideo): Clip {
  return faceId && voice ? { audio_url: audioUrl, openVideo: () => open(audioUrl, voice, faceId), lipsync: { voice, faceId } } : { audio_url: audioUrl };
}

type SpeakFn = (text: string, voice: string, emotion?: string, language?: string | null) => Promise<{ audio_url: string; timestamps?: unknown }>;

/**
 * One answer sentence of the Gemini path as a clip: Boson TTS in the answer's language (`language`, ISO 639-3, only
 * when not English, #135), lip-synced when a face is shown. A non-English sentence comes back with `timestamps: null`
 * (the backend asks Boson for normalized speech, which has no word timing): nothing here needs it, because answers are
 * shown and played a sentence at a time.
 */
export async function answerClip(speakFn: SpeakFn, text: string, voice: string, opts: { emotion?: string; language?: string | null; faceId: string | null },
  open = openClipVideo): Promise<Clip> {
  const r = await speakFn(text, voice, opts.emotion, opts.language ?? null);
  return lipSyncClip(r.audio_url, voice, opts.faceId, open);
}
