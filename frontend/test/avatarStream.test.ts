import { describe, expect, it, vi } from "vitest";
import { AVATAR_STREAM_MIME, MdatScanner, chooseLipSyncMode, openAvatarStream, supportsAvatarStream } from "../src/talk/avatarStream";
import { createSentenceSpeaker } from "../src/talk/sentencePlayer";
import { debugEnabled } from "../src/ui/debugFlag";

const box = (type: string, n: number) => {
  const b = new Uint8Array(n);
  new DataView(b.buffer).setUint32(0, n);
  b.set([...type].map((c) => c.charCodeAt(0)), 4);
  return b;
};

describe("support detection and fallback choice", () => {
  it("streams only when MediaSource supports the avatar codecs", () => {
    const yes = { isTypeSupported: (t: string) => t === AVATAR_STREAM_MIME };
    expect(supportsAvatarStream(yes)).toBe(true);
    expect(chooseLipSyncMode(yes)).toBe("stream");
    expect(chooseLipSyncMode({ isTypeSupported: () => false })).toBe("async");
    expect(chooseLipSyncMode(undefined)).toBe("async"); // no MediaSource (jsdom/node, iOS Safari)
    expect(chooseLipSyncMode({ isTypeSupported: () => { throw new Error("boom"); } })).toBe("async");
  });
});

describe("MdatScanner", () => {
  it("counts a complete mdat even when it arrives in odd pieces", () => {
    const all = new Uint8Array([...box("ftyp", 28), ...box("moov", 100), ...box("moof", 40), ...box("mdat", 500)]);
    const s = new MdatScanner();
    for (let i = 0; i < all.length - 1; i += 7) s.push(all.slice(i, Math.min(i + 7, all.length - 1)));
    expect(s.mdats).toBe(0); // the last byte of the mdat has not arrived
    s.push(all.slice(all.length - 1));
    expect(s.mdats).toBe(1);
  });
});

function fakeDeps(chunks: Uint8Array[], opts: { json?: boolean; status?: number } = {}) {
  const revoked: string[] = [];
  let aborted = false;
  let cancelled = false;
  const post = vi.fn(async (signal: AbortSignal) => {
    signal.addEventListener("abort", () => { aborted = true; });
    if (opts.json) return new Response(JSON.stringify({ video_url: "/media/avatar/x.mp4" }), { headers: { "content-type": "application/json" } });
    if (opts.status) return new Response(JSON.stringify({ detail: "busy" }), { status: opts.status });
    let i = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(c) { if (i < chunks.length) c.enqueue(chunks[i++]); else c.close(); },
      cancel() { cancelled = true; },
    });
    return new Response(body, { headers: { "content-type": "video/mp4" } });
  });
  class FakeMS { readyState = "closed"; addEventListener() {} }
  return {
    deps: { post, MediaSourceCtor: FakeMS as unknown as new () => MediaSource, createObjectURL: () => "blob:ms", revokeObjectURL: (u: string) => revoked.push(u) },
    revoked, state: () => ({ aborted, cancelled }),
  };
}

describe("openAvatarStream", () => {
  const good = [box("ftyp", 28), box("moov", 60), box("moof", 20), box("mdat", 80)];

  it("resolves at the first fragment and release aborts, cancels and revokes once", async () => {
    const f = fakeDeps(good);
    const clip = await openAvatarStream(f.deps);
    expect(clip.url).toBe("blob:ms");
    clip.release(); clip.release();
    expect(f.revoked).toEqual(["blob:ms"]);
    expect(f.state().aborted).toBe(true);
  });

  it("uses the plain file URL for a cached clip", async () => {
    const f = fakeDeps([], { json: true });
    const clip = await openAvatarStream(f.deps);
    expect(clip.url).toBe("/media/avatar/x.mp4");
    expect(f.revoked).toEqual([]);
  });

  it("rejects with the server's message and cleans up on an error status or an empty stream", async () => {
    await expect(openAvatarStream(fakeDeps([], { status: 429 }).deps)).rejects.toThrow("busy");
    const f = fakeDeps([]);
    await expect(openAvatarStream(f.deps)).rejects.toThrow(/empty/);
    expect(f.revoked).toEqual(["blob:ms"]);
    expect(f.state().aborted).toBe(true);
  });
});

describe("sentence speaker releases streamed clips", () => {
  const audio = () => ({ onended: null as (() => void) | null, onerror: null, play: () => {}, pause() {} });

  it("releases after the sentence ends and when stopped before playing", async () => {
    const release = vi.fn();
    const sp = createSentenceSpeaker(async () => ({ audio_url: "a", openVideo: async () => ({ url: "blob:x", release }) }), () => {
      const a = audio(); setTimeout(() => a.onended?.(), 0); return a;
    });
    await sp.play("hi");
    expect(release).toHaveBeenCalledTimes(1);

    const r2 = vi.fn();
    const sp2 = createSentenceSpeaker(async () => ({ audio_url: "a", openVideo: async () => ({ url: "blob:y", release: r2 }) }), audio);
    sp2.prefetch("later");
    sp2.stop();
    await new Promise((r) => setTimeout(r, 0));
    expect(r2).toHaveBeenCalledTimes(1);
  });
});

describe("debug pill flag", () => {
  const st = (v: string | null) => ({ getItem: () => v });
  it("is off unless ?debug or localStorage talkback:debug=1", () => {
    expect(debugEnabled("", st(null))).toBe(false);
    expect(debugEnabled("?x=1", st("0"))).toBe(false);
    expect(debugEnabled("?debug", st(null))).toBe(true);
    expect(debugEnabled("", st("1"))).toBe(true);
  });
});
