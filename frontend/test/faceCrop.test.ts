import { afterEach, describe, expect, it, vi } from "vitest";
import { clampCrop, cropJpeg, moveCrop, proposeCrop, resizeCrop } from "../src/reader/faceCrop";
import { detectFace } from "../src/api";

afterEach(() => vi.unstubAllGlobals());

function fakeCanvas() {
  const drawImage = vi.fn();
  const canvas = {
    width: 0, height: 0,
    getContext: () => ({ drawImage }),
    toBlob: (cb: (b: Blob | null) => void, type: string, q: number) => { expect(q).toBeCloseTo(0.9); cb(new Blob(["x"], { type })); },
  } as unknown as HTMLCanvasElement;
  return { canvas, drawImage };
}

describe("proposeCrop", () => {
  it("frames head and shoulders: centred on the face, ~2.4x its height, eyes about 40% from the top", () => {
    const c = proposeCrop({ x: 0.4, y: 0.2, w: 0.2, h: 0.2 }, 1000, 1500);
    expect(c.side).toBe(720);                    // 2.4 * 300 px face height
    expect(c.x + c.side / 2).toBeCloseTo(500, 0); // face centre
    const eyes = 0.2 * 1500 + 0.45 * 300;
    expect((eyes - c.y) / c.side).toBeCloseTo(0.4, 1);
  });

  it("is clamped inside the image", () => {
    const c = proposeCrop({ x: 0.0, y: 0.0, w: 0.15, h: 0.1 }, 2000, 2000);
    expect(c.x).toBe(0);
    expect(c.y).toBe(0);
    expect(c.side).toBe(480);
  });

  it("shrinks when the face is too big for the image", () => {
    const c = proposeCrop({ x: 0.2, y: 0.1, w: 0.6, h: 0.8 }, 800, 600);
    expect(c.side).toBe(600);
    expect(c.y).toBe(0);
    expect(c.x).toBeGreaterThanOrEqual(0);
    expect(c.x + c.side).toBeLessThanOrEqual(800);
  });

  it("falls back to a centred square near the top without a face", () => {
    expect(proposeCrop(null, 1000, 1500)).toEqual({ x: 0, y: 50, side: 1000 });
    expect(proposeCrop(null, 1600, 900)).toEqual({ x: 350, y: 0, side: 900 });
  });
});

describe("clampCrop", () => {
  it("keeps the square inside the image with a minimum size", () => {
    expect(clampCrop({ x: -50, y: 900, side: 400 }, 1000, 1000)).toEqual({ x: 0, y: 600, side: 400 });
    expect(clampCrop({ x: 0, y: 0, side: 5000 }, 1000, 800)).toEqual({ x: 0, y: 0, side: 800 });
    expect(clampCrop({ x: 10, y: 10, side: 2 }, 1000, 800).side).toBeGreaterThanOrEqual(32);
  });
});

describe("moveCrop / resizeCrop", () => {
  it("moves the square but never out of the image", () => {
    expect(moveCrop({ x: 100, y: 100, side: 300 }, 50, -20, 1000, 800)).toEqual({ x: 150, y: 80, side: 300 });
    expect(moveCrop({ x: 100, y: 100, side: 300 }, 5000, 5000, 1000, 800)).toEqual({ x: 700, y: 500, side: 300 });
  });

  it("resizes from the corner, staying square, with its top-left fixed and inside the image", () => {
    expect(resizeCrop({ x: 100, y: 100, side: 300 }, 40, 10, 1000, 800)).toEqual({ x: 100, y: 100, side: 340 });
    expect(resizeCrop({ x: 100, y: 100, side: 300 }, 5000, 0, 1000, 800)).toEqual({ x: 100, y: 100, side: 700 });
    expect(resizeCrop({ x: 100, y: 100, side: 300 }, -1000, -1000, 1000, 800).side).toBe(32);
  });
});

describe("cropJpeg", () => {
  it("draws the square and encodes a JPEG of at most 1024 px", async () => {
    const { canvas, drawImage } = fakeCanvas();
    const file = await cropJpeg({} as CanvasImageSource, { x: 100, y: 50, side: 2000 }, () => canvas);
    expect(file.type).toBe("image/jpeg");
    expect(file.name).toBe("portrait.jpg");
    expect(canvas.width).toBe(1024);
    expect(canvas.height).toBe(1024);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 100, 50, 2000, 2000, 0, 0, 1024, 1024);
  });

  it("keeps a small crop at its own size", async () => {
    const { canvas } = fakeCanvas();
    await cropJpeg({} as CanvasImageSource, { x: 0, y: 0, side: 300 }, () => canvas);
    expect(canvas.width).toBe(300);
  });
});

describe("detectFace", () => {
  it("posts the image and returns the box", async () => {
    const fetchMock = vi.fn(async (_u: string, _i?: RequestInit) => new Response(JSON.stringify({ face: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } })));
    vi.stubGlobal("fetch", fetchMock);
    expect(await detectFace(new File(["x"], "me.png", { type: "image/png" }))).toEqual({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/faces/detect");
    expect((fetchMock.mock.calls[0][1]?.body as FormData).get("image")).toBeInstanceOf(File);
  });

  it("is null on no face, an error or a bad answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ face: null }))));
    expect(await detectFace(new File(["x"], "a.png"))).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("busy", { status: 429 })));
    expect(await detectFace(new File(["x"], "a.png"))).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ face: { x: "a" } }))));
    expect(await detectFace(new File(["x"], "a.png"))).toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    expect(await detectFace(new File(["x"], "a.png"))).toBeNull();
  });
});
