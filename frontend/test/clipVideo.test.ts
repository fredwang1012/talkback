/** Lip-sync for any voice x face pair (#113): the clip names the face shown; no face shown = audio only. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cloneVoice, lipSync, lipSyncBody, lipSyncStream } from "../src/api";
import { lipSyncClip } from "../src/talk/clipVideo";
import type { VideoClip } from "../src/talk/sentencePlayer";

const AUDIO = "/media/tts/abc.mp3";

describe("lipSyncClip", () => {
  const open = vi.fn(async (): Promise<VideoClip> => ({ url: "blob:x", release: () => {} }));
  afterEach(() => open.mockClear());

  it("lip-syncs whenever a real face is shown, whatever the voice (preset, cloned, uploaded face)", async () => {
    for (const [voice, face] of [["marcus", "f-upload"], ["voice_clone", "f-other"], ["voice_clone", "f-own"]] as const) {
      const clip = lipSyncClip(AUDIO, voice, face, open);
      expect(clip.openVideo).toBeDefined();
      await clip.openVideo!();
      expect(open).toHaveBeenLastCalledWith(AUDIO, voice, face);
    }
  });

  it("is audio only when no face is shown or there is no voice", () => {
    expect(lipSyncClip(AUDIO, "marcus", null, open)).toEqual({ audio_url: AUDIO });
    expect(lipSyncClip(AUDIO, "", "f-upload", open)).toEqual({ audio_url: AUDIO });
    expect(open).not.toHaveBeenCalled();
  });
});

describe("lip-sync requests carry the face id", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("builds the body with face_id, omitted when there is none (server falls back to the voice's face)", () => {
    expect(JSON.parse(lipSyncBody(AUDIO, "marcus", "f1"))).toEqual({ audio_url: AUDIO, voice: "marcus", face_id: "f1" });
    expect(JSON.parse(lipSyncBody(AUDIO, "marcus", null))).toEqual({ audio_url: AUDIO, voice: "marcus" });
  });

  it("lipSync and lipSyncStream send it", async () => {
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, body: JSON.parse(init.body as string) });
      return new Response(JSON.stringify({ video_url: "/media/avatar/x.mp4" }), { status: 200, headers: { "content-type": "application/json" } });
    }));
    expect(await lipSync(AUDIO, "chloe", "f-up")).toBe("/media/avatar/x.mp4");
    await lipSyncStream(AUDIO, "chloe", "f-up", new AbortController().signal);
    expect(calls.map((c) => c.url)).toEqual(["/api/avatar/talk", "/api/avatar/stream"]);
    for (const c of calls) expect(c.body).toEqual({ audio_url: AUDIO, voice: "chloe", face_id: "f-up" });
  });
});

describe("cloneVoice with an existing face", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("links the face by id instead of uploading a copy", async () => {
    let form: FormData | null = null;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      form = init.body as FormData;
      return new Response(JSON.stringify({ voice: "v1", name: "me", face: "/portraits/faces/f-up.png" }), { status: 200 });
    }));
    await cloneVoice(new Blob(["a"]), "a.wav", "hello there", "me", true, null, "f-up");
    expect(form!.get("face_id")).toBe("f-up");
    expect(form!.get("face")).toBeNull();
  });
});
