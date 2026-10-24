import { describe, expect, it } from "vitest";
import { loopsToPreload, showReadingFace } from "../src/reader/readFace";

describe("read-mode face timing", () => {
  it("the reading face is up while the first chunk is still loading, so its loops are ready when the voice starts", () => {
    expect(showReadingFace({ playing: false, loading: true })).toBe(true);
    expect(showReadingFace({ playing: true, loading: false })).toBe(true);
    expect(showReadingFace({ playing: false, loading: false })).toBe(false);
  });

  it("preloads the next reader's loops only when the face changes", () => {
    const a = { id: "a", talk: "/a-talk.mp4", idle: "/a-idle.mp4" };
    const b = { id: "b", talk: "/b-talk.mp4", idle: null };
    expect(loopsToPreload(a, b)).toEqual(["/b-talk.mp4"]);
    expect(loopsToPreload(a, a)).toEqual([]);
    expect(loopsToPreload(null, a)).toEqual(["/a-talk.mp4", "/a-idle.mp4"]);
    expect(loopsToPreload(a, undefined)).toEqual([]);
  });
});
