import { describe, expect, it, vi } from "vitest";
import { cameraErrorMessage, captureJpeg, squareCrop, stopStream } from "../src/audio/camera";

function fakeCanvas() {
  const drawImage = vi.fn();
  const canvas = {
    width: 0, height: 0,
    getContext: () => ({ drawImage }),
    toBlob: (cb: (b: Blob | null) => void, type: string, q: number) => { expect(q).toBeCloseTo(0.9); cb(new Blob(["x"], { type })); },
  } as unknown as HTMLCanvasElement;
  return { canvas, drawImage };
}

describe("camera capture", () => {
  it("stops every track (cancel / unmount path)", () => {
    const tracks = [{ stop: vi.fn() }, { stop: vi.fn() }];
    stopStream({ getTracks: () => tracks });
    tracks.forEach((t) => expect(t.stop).toHaveBeenCalledTimes(1));
    expect(() => stopStream(null)).not.toThrow();
  });

  it("center-crops to a square, at most 1024", () => {
    expect(squareCrop(1280, 720)).toEqual({ sx: 280, sy: 0, side: 720, out: 720 });
    expect(squareCrop(3000, 2000)).toMatchObject({ sx: 500, side: 2000, out: 1024 });
  });

  it("returns an unmirrored image/jpeg File named portrait.jpg", async () => {
    const { canvas, drawImage } = fakeCanvas();
    const file = await captureJpeg({ videoWidth: 1280, videoHeight: 720 }, () => canvas);
    expect(file.type).toBe("image/jpeg");
    expect(file.name).toBe("portrait.jpg");
    expect(canvas.width).toBe(720);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 280, 0, 720, 720, 0, 0, 720, 720);
  });

  it("hands the File to the callback", async () => {
    const { canvas } = fakeCanvas();
    const onUse = vi.fn();
    onUse(await captureJpeg({ videoWidth: 640, videoHeight: 480 }, () => canvas));
    expect(onUse.mock.calls[0][0]).toBeInstanceOf(File);
    expect(onUse.mock.calls[0][0].type).toBe("image/jpeg");
  });

  it("explains NotAllowedError and a missing camera", () => {
    expect(cameraErrorMessage({ name: "NotAllowedError" })).toMatch(/Camera access is blocked/);
    expect(cameraErrorMessage({ name: "NotFoundError" })).toMatch(/Camera not found/);
  });
});
