import { describe, expect, it } from "vitest";
import { isAutoplayBlock, micErrorMessage, tryPlayQuietly, TAP_TO_PLAY } from "../src/audio/mediaErrors";
import { ReaderEngine, type AudioLike } from "../src/reader/ReaderEngine";

const notAllowed = () => Object.assign(new Error("The request is not allowed by the user agent"), { name: "NotAllowedError" });
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("media errors", () => {
  it("recognises NotAllowedError only", () => {
    expect(isAutoplayBlock(notAllowed())).toBe(true);
    expect(isAutoplayBlock(new Error("x"))).toBe(false);
    expect(isAutoplayBlock(null)).toBe(false);
  });
  it("explains microphone failures in plain words", () => {
    expect(micErrorMessage(notAllowed())).toMatch(/allow the microphone/);
    expect(micErrorMessage(Object.assign(new Error(), { name: "NotFoundError" }))).toMatch(/Microphone not found/);
    expect(micErrorMessage(new Error("weird"))).toMatch(/try again/);
  });
  it("swallows a refused muted play", async () => {
    expect(await tryPlayQuietly({ play: () => Promise.reject(notAllowed()) })).toBe(false);
    expect(await tryPlayQuietly({ play: () => Promise.resolve() })).toBe(true);
  });
});

describe("ReaderEngine autoplay refusal", () => {
  it("pauses with a tap-to-play notice instead of an error", async () => {
    let refuse = true;
    const audio: AudioLike = {
      src: "", currentTime: 0, playbackRate: 1, preservesPitch: false, preload: "", onended: null,
      play: () => (refuse ? Promise.reject(notAllowed()) : Promise.resolve()), pause() {},
    };
    const engine = new ReaderEngine(
      [{ id: "c0", speaker: "n", voice: "v", tts_text: "t", char_start: 0, char_end: 1, para: 0 }],
      { fetchTts: async () => ({ audio_url: "/a.mp3", timestamps: null }), createAudio: () => audio });
    engine.play();
    await flush();
    expect(engine.snapshot()).toEqual({ state: "paused", index: 0, error: TAP_TO_PLAY });
    refuse = false;
    engine.play();
    await flush();
    expect(engine.snapshot()).toEqual({ state: "playing", index: 0, error: null });
  });
});
