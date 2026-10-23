import { describe, expect, it } from "vitest";
import { encodeWav, needsWav } from "../src/audio/toWav";

describe("clone audio format (#108)", () => {
  it("converts browser recordings but not formats Boson takes", () => {
    expect(needsWav("recording.webm")).toBe(true);
    expect(needsWav("recording.ogg")).toBe(true);
    expect(needsWav("me.m4a")).toBe(true);
    expect(needsWav("me.WAV")).toBe(false);
    expect(needsWav("me.mp3")).toBe(false);
    expect(needsWav("me.flac")).toBe(false);
  });

  it("writes mono PCM16 WAV, averaging channels", () => {
    const wav = encodeWav([new Float32Array([1, -1, 0]), new Float32Array([1, 1, 0])], 48000);
    const v = new DataView(wav.buffer);
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
    expect(String.fromCharCode(...wav.slice(8, 12))).toBe("WAVE");
    expect(v.getUint16(22, true)).toBe(1); // mono
    expect(v.getUint32(24, true)).toBe(48000);
    expect(v.getUint32(40, true)).toBe(6);
    expect(v.getInt16(44, true)).toBe(0x7fff);
    expect(v.getInt16(46, true)).toBe(0);
    expect(v.getInt16(48, true)).toBe(0);
  });
});
