import { describe, expect, it, vi } from "vitest";
import { faceFileName, faceMissing, loadedSize, missingLine, objectUrlEffect, voiceMissing } from "../src/reader/labForms";

const ready = { clip: true, transcript: "Hello there", name: "Me", consent: true, audioAvailable: true, busy: false };

describe("voiceMissing (#111)", () => {
  it("is empty when everything is set", () => {
    expect(voiceMissing(ready)).toEqual([]);
  });
  it("names each missing piece", () => {
    expect(voiceMissing({ ...ready, clip: false, name: " ", consent: false })).toEqual([
      "a recording or an audio file", "a name for the voice", "the consent box ticked",
    ]);
    expect(voiceMissing({ ...ready, transcript: "  " })).toEqual(["the words you said"]);
  });
  it("explains when cloning is off on the server instead of listing fields", () => {
    const m = voiceMissing({ ...ready, audioAvailable: false, clip: false });
    expect(m).toHaveLength(1);
    expect(m[0]).toMatch(/Boson API key/);
  });
});

describe("faceMissing (#111)", () => {
  it("is empty with a cropped photo and a name", () => {
    expect(faceMissing({ file: true, cropping: false, name: "Ann", busy: false })).toEqual([]);
  });
  it("asks for a photo and a name", () => {
    expect(faceMissing({ file: false, cropping: false, name: "", busy: false })).toEqual([
      "a name for the face", "a photo (choose a file or take one)",
    ]);
  });
  it("asks to press Use while a crop is open", () => {
    expect(faceMissing({ file: false, cropping: true, name: "Ann", busy: false })).toEqual(["Use under the photo to keep the crop"]);
  });
});

describe("missingLine", () => {
  it("joins with commas and 'and'", () => {
    expect(missingLine([])).toBeNull();
    expect(missingLine(["a"])).toBe("Still needed: a.");
    expect(missingLine(["a", "b", "c"])).toBe("Still needed: a, b and c.");
  });
});

describe("objectUrlEffect (#111: first chosen photo never showed)", () => {
  it("leaves a live URL after StrictMode's mount, cleanup, mount", () => {
    let n = 0;
    const revoked = new Set<string>();
    const api = { createObjectURL: () => `blob:${++n}`, revokeObjectURL: (u: string) => { revoked.add(u); } };
    const setUrl = vi.fn();
    const file = new Blob(["x"]);
    const cleanup = objectUrlEffect(file, setUrl, api);
    cleanup();                                 // StrictMode's simulated unmount
    objectUrlEffect(file, setUrl, api);        // and remount
    const current = setUrl.mock.calls.at(-1)![0] as string;
    expect(current).toBe("blob:2");
    expect(revoked.has(current)).toBe(false);
    expect(revoked.has("blob:1")).toBe(true);
  });
});

describe("loadedSize", () => {
  it("reads a finished image and ignores one still loading or broken", () => {
    expect(loadedSize({ complete: true, naturalWidth: 640, naturalHeight: 480 })).toEqual({ w: 640, h: 480 });
    expect(loadedSize({ complete: false, naturalWidth: 0, naturalHeight: 0 })).toBeNull();
    expect(loadedSize({ complete: true, naturalWidth: 0, naturalHeight: 0 })).toBeNull();
    expect(loadedSize(null)).toBeNull();
  });
});

describe("faceFileName", () => {
  it("follows the content type", () => {
    expect(faceFileName("image/png")).toBe("portrait.png");
    expect(faceFileName("image/webp")).toBe("portrait.webp");
    expect(faceFileName("image/jpeg")).toBe("portrait.jpg");
    expect(faceFileName(null)).toBe("portrait.jpg");
  });
});
