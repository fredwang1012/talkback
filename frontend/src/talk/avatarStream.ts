/**
 * Streaming lip-sync: plays Boson's fragmented-MP4 stream through Media Source Extensions so the face starts at the
 * first frames instead of after the whole clip renders. The video is muted; our own TTS audio stays the sound and
 * drives word highlighting. Bytes are queued as they arrive and fed to the SourceBuffer once a <video> attaches.
 */

/** What Boson's stream carries (measured): H.264 High 3.0 plus AAC-LC, 640x640. */
export const AVATAR_STREAM_MIME = 'video/mp4; codecs="avc1.64001e, mp4a.40.2"';

export type LipSyncMode = "stream" | "async";

type MSSupport = { isTypeSupported(type: string): boolean };

export function supportsAvatarStream(ms: MSSupport | undefined = (globalThis as { MediaSource?: MSSupport }).MediaSource): boolean {
  try { return !!ms && typeof ms.isTypeSupported === "function" && ms.isTypeSupported(AVATAR_STREAM_MIME); } catch { return false; }
}

/** Stream when the browser can play it, else the render-then-download path. */
export const chooseLipSyncMode = (ms?: MSSupport): LipSyncMode => (supportsAvatarStream(ms) ? "stream" : "async");

/** Counts complete top-level `mdat` boxes in a byte stream: the first one is the first playable fragment. */
export class MdatScanner {
  mdats = 0;
  broken = false; // an unparseable box size: stop counting, the caller falls back to "any data"
  private remaining = 0;
  private head: number[] = [];
  private type = "";

  push(chunk: Uint8Array): void {
    let i = 0;
    while (i < chunk.length && !this.broken) {
      if (this.remaining > 0) {
        const n = Math.min(this.remaining, chunk.length - i);
        this.remaining -= n;
        i += n;
        if (this.remaining === 0) this.finishBox();
        continue;
      }
      this.head.push(chunk[i++]);
      if (this.head.length === 8) {
        const h = this.head;
        const size = ((h[0] << 24) | (h[1] << 16) | (h[2] << 8) | h[3]) >>> 0;
        this.type = String.fromCharCode(h[4], h[5], h[6], h[7]);
        this.head = [];
        if (size < 8) { this.broken = true; return; }
        this.remaining = size - 8;
        if (this.remaining === 0) this.finishBox();
      }
    }
  }

  private finishBox() { if (this.type === "mdat") this.mdats++; }
}

export type AvatarClip = { url: string; release: () => void };

export type StreamDeps = {
  post: (signal: AbortSignal) => Promise<Response>;
  MediaSourceCtor: new () => MediaSource;
  createObjectURL: (o: MediaSource) => string;
  revokeObjectURL: (url: string) => void;
  readyTimeoutMs?: number;
};

/**
 * Starts the stream and resolves once the first playable fragment has arrived (so audio and video can start
 * together), or with a plain file URL when the server already has the clip cached. `release` aborts the request and
 * frees the MediaSource and its object URL; call it when the sentence ends, is stopped, or is never played.
 */
export async function openAvatarStream(deps: StreamDeps): Promise<AvatarClip> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), deps.readyTimeoutMs ?? 60000);
  let url: string | null = null;
  let released = false;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  const release = () => {
    if (released) return;
    released = true;
    clearTimeout(timer);
    ctl.abort();
    reader?.cancel().catch(() => {});
    if (url) deps.revokeObjectURL(url);
  };
  try {
    const res = await deps.post(ctl.signal);
    if (!res.ok) {
      const detail = await res.json().then((j: { detail?: string }) => j.detail).catch(() => undefined);
      throw new Error(detail ?? `Lip-sync stream failed (${res.status})`);
    }
    if ((res.headers.get("content-type") ?? "").includes("json")) { // cached on the server: a plain file
      clearTimeout(timer);
      return { url: ((await res.json()) as { video_url: string }).video_url, release: () => {} };
    }
    if (!res.body) throw new Error("This browser cannot read a streamed response.");
    const rd = res.body.getReader();
    reader = rd;

    const ms = new deps.MediaSourceCtor();
    url = deps.createObjectURL(ms);
    const queue: Uint8Array[] = [];
    let sb: SourceBuffer | null = null;
    let done = false;
    const kick = () => {
      if (released || !sb || sb.updating) return;
      try {
        if (queue.length) sb.appendBuffer(queue.shift() as BufferSource);
        else if (done && ms.readyState === "open") ms.endOfStream();
      } catch { done = true; queue.length = 0; } // e.g. buffer quota: stop feeding, keep what played
    };
    ms.addEventListener("sourceopen", () => {
      if (sb || released) return;
      try {
        sb = ms.addSourceBuffer(AVATAR_STREAM_MIME);
        sb.addEventListener("updateend", kick);
        kick();
      } catch { done = true; }
    });

    const scanner = new MdatScanner();
    let bytes = 0;
    let signalReady: () => void = () => {};
    let signalFail: (e: unknown) => void = () => {};
    const ready = new Promise<void>((resolve, reject) => { signalReady = resolve; signalFail = reject; });
    let isReady = false;
    const markReady = () => { if (!isReady) { isReady = true; clearTimeout(timer); signalReady(); } };

    void (async () => {
      try {
        for (;;) {
          const { value, done: end } = await rd.read();
          if (end) break;
          if (!value?.length) continue;
          bytes += value.length;
          scanner.push(value);
          queue.push(value);
          kick();
          if (scanner.mdats >= 1 || scanner.broken) markReady();
        }
        done = true;
        kick();
        if (bytes === 0) signalFail(new Error("The avatar stream was empty."));
        else markReady();
      } catch (e) {
        done = true;
        kick();
        if (!isReady) signalFail(e); // after the first fragment a drop just ends the video where it stopped
      }
    })();

    await ready;
    return { url, release };
  } catch (e) {
    release();
    throw e;
  }
}
