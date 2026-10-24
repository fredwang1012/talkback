import { lipSync, lipSyncStream } from "../api";
import { chooseLipSyncMode, openAvatarStream } from "./avatarStream";
import type { VideoClip } from "./sentencePlayer";

/**
 * The lip-sync video of one sentence or chunk (the face bound to `voice` speaking that cached TTS audio): streamed
 * when the browser can, else a plain clip URL. Cached clips come back at once and cost nothing; `onRender` is called
 * when the server starts a fresh (paid) render.
 */
export async function openClipVideo(audioUrl: string, voice: string, onRender?: () => void): Promise<VideoClip> {
  if (chooseLipSyncMode() === "stream") {
    const c = await openAvatarStream({
      post: async (signal) => {
        const res = await lipSyncStream(audioUrl, voice, signal);
        if (res.ok && !(res.headers.get("content-type") ?? "").includes("json")) onRender?.();
        return res;
      },
      MediaSourceCtor: MediaSource, createObjectURL: (o) => URL.createObjectURL(o), revokeObjectURL: (u) => URL.revokeObjectURL(u),
    });
    return { url: c.url, release: c.release };
  }
  return { url: await lipSync(audioUrl, voice), release: () => {} };
}
