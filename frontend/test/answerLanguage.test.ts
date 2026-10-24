import { describe, expect, it, vi } from "vitest";
import { answerClip } from "../src/talk/clipVideo";
import { createSentenceSpeaker, type AudioLike } from "../src/talk/sentencePlayer";

describe("a non-English answer sentence (#135): language sent, timestamps null is fine", () => {
  it("speaks with the language, and the clip plays through the sentence speaker without word timing", async () => {
    const speak = vi.fn(async () => ({ chunk_id: "c", voice: "marcus", audio_url: "/media/tts/hola.wav", timestamps: null, cached: false }));
    const open = vi.fn(async () => ({ url: "blob:v", release: () => {} }));
    const clips: (AudioLike & { url: string })[] = [];
    const speaker = createSentenceSpeaker(
      (text) => answerClip(speak, text, "marcus", { emotion: "joy", language: "spa", faceId: "f1" }, open),
      (url) => { const a = { url, onended: null, onerror: null, play: () => Promise.resolve(), pause() {} } as AudioLike & { url: string }; clips.push(a); return a; },
    );
    const done = speaker.play("¡Hola!");
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    clips[0].onended?.();
    await done;
    expect(speak).toHaveBeenCalledWith("¡Hola!", "marcus", "joy", "spa");
    expect(clips[0].url).toBe("/media/tts/hola.wav");
  });

  it("an English answer sends no language", async () => {
    const speak = vi.fn(async () => ({ audio_url: "/x.wav", timestamps: [] }));
    const clip = await answerClip(speak, "Hi.", "marcus", { faceId: null });
    expect(speak).toHaveBeenCalledWith("Hi.", "marcus", undefined, null);
    expect(clip).toEqual({ audio_url: "/x.wav" });
  });
});
