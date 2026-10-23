import { describe, expect, it } from "vitest";
import type { Face } from "../src/api";
import { clipAheadFor, clipWindow, lipSyncVoiceFor, readClipsOn, readFaceFor, speechSeconds } from "../src/reader/readLipSync";

const face = (id: string, voice: string | null): Face => ({ id, name: id, image: `/${id}.png`, voice, talk: null, idle: null });
const nar = face("f-nar", "clone-nar");
const harry = face("f-harry", "clone-harry");
const faces = (map: Record<string, Face | null>, voices: Record<string, string>) => ({
  faceFor: (s: string) => map[s] ?? null,
  voiceFor: (s: string) => voices[s] ?? "marcus",
});

describe("read-mode lip-sync decisions", () => {
  it("holds clips for the playing chunk and a bounded number ahead, clamped to the book", () => {
    expect(clipWindow(3, 1, 9)).toEqual([3, 4]);
    expect(clipWindow(9, 1, 9)).toEqual([9, 9]);
    expect(clipWindow(3, 2, 4)).toEqual([3, 4]);
    expect(clipWindow(-1, 1, 9)).toEqual([0, 1]);
  });

  it("a chunk is read by its speaker's face when they have one, else the narrator's", () => {
    const f = faces({ narrator: nar, harry }, { narrator: "clone-nar", harry: "clone-harry", hermione: "chloe" });
    expect(readFaceFor(f, "harry")).toBe(harry);
    expect(readFaceFor(f, "hermione")).toBe(nar);
    expect(readFaceFor(faces({}, {}), "hermione")).toBeNull();
  });

  it("lip-syncs on the chunk's own voice only when the face shown is bound to it", () => {
    const f = faces({ narrator: nar, harry }, { narrator: "clone-nar", harry: "clone-harry" });
    expect(lipSyncVoiceFor(f, "harry", "clone-harry")).toBe("clone-harry");
    expect(lipSyncVoiceFor(f, "harry", "jake")).toBeNull(); // the voice changed under the face
    expect(lipSyncVoiceFor(f, "harry", undefined)).toBeNull();
  });

  it("a speaker without a face is read by the narrator's face, lip-synced on the narrator's voice", () => {
    const f = faces({ narrator: nar }, { narrator: "clone-nar", hermione: "chloe" });
    expect(lipSyncVoiceFor(f, "hermione", "chloe")).toBe("clone-nar");
    const unbound = faces({ narrator: face("f-nar", null) }, { narrator: "marcus", hermione: "chloe" });
    expect(lipSyncVoiceFor(unbound, "hermione", "chloe")).toBeNull();
    expect(lipSyncVoiceFor(faces({}, {}), "hermione", "chloe")).toBeNull();
  });

  it("lip-syncs whenever there is audio, but only at 1x: at other speeds the talk loop stands in (#92)", () => {
    expect(readClipsOn(true, 1)).toBe(true);
    expect(readClipsOn(true, 1.25)).toBe(false);
    expect(readClipsOn(true, 0.75)).toBe(false);
    expect(readClipsOn(false, 1)).toBe(false);
  });

  it("estimates the seconds of speech in a chunk for the spend tracker", () => {
    expect(speechSeconds("x".repeat(150))).toBe(10);
    expect(speechSeconds("")).toBe(1);
  });
});

describe("clipAheadFor (#104)", () => {
  const lens = [8, 0.9, 0.8, 1.1, 0.7, 3, 8];
  const sp = (k: number) => lens[k];
  it("one ahead behind a normal chunk", () => {
    expect(clipAheadFor(0, 6, sp)).toBe(1);
    expect(clipAheadFor(5, 9, (k) => [8, 0.9, 0.8, 1.1, 0.7, 3, 8, 8, 8][k])).toBe(2); // 3 s < 4 s of head start
  });
  it("short chunks open more clips ahead, capped", () => {
    expect(clipAheadFor(1, 6, sp)).toBe(3);
    expect(clipAheadFor(3, 6, sp)).toBe(3); // 1.1 + 0.7 < 4, + 3 >= 4
    expect(clipAheadFor(1, 6, sp, { max: 2 })).toBe(2);
  });
  it("never past the last chunk", () => {
    expect(clipAheadFor(4, 6, sp)).toBe(2); // 0.7 + 3 < 4, but chunk 6 is the last
    expect(clipAheadFor(6, 6, sp)).toBe(1);
  });
});
