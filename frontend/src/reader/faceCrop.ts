/** Head-and-shoulders square crop for face uploads (#109). Free of React so it can be unit-tested. */
import type { FaceBox } from "../api";
import { squareJpeg } from "../audio/camera";

/** A square in image pixels. */
export type Crop = { x: number; y: number; side: number };

export const HEAD_AND_SHOULDERS = 2.4; // crop side / face height
const EYES_IN_FACE = 0.45;             // eyes sit about here down the face box (hair to chin)
const EYES_IN_CROP = 0.4;              // and about here down the crop
const TOP_BIAS = 0.1;                  // without a face: the top at 10% of the spare height
export const MIN_SIDE = 32;

/** The square kept inside a w x h image, at least MIN_SIDE (or the image) and at most the image's short edge. */
export function clampCrop(c: Crop, w: number, h: number): Crop {
  const max = Math.min(w, h);
  const side = Math.round(Math.min(max, Math.max(Math.min(MIN_SIDE, max), c.side)));
  return {
    x: Math.round(Math.min(Math.max(0, c.x), w - side)),
    y: Math.round(Math.min(Math.max(0, c.y), h - side)),
    side,
  };
}

/** Proposed square for a w x h image: head and shoulders around the face, or a centred, top-biased square. */
export function proposeCrop(face: FaceBox | null, w: number, h: number): Crop {
  const max = Math.min(w, h);
  if (!face || face.w <= 0 || face.h <= 0)
    return clampCrop({ x: (w - max) / 2, y: (h - max) * TOP_BIAS, side: max }, w, h);
  const faceH = face.h * h;
  const side = Math.min(max, HEAD_AND_SHOULDERS * faceH);
  const cx = (face.x + face.w / 2) * w;
  const eyes = (face.y + EYES_IN_FACE * face.h) * h;
  return clampCrop({ x: cx - side / 2, y: eyes - EYES_IN_CROP * side, side }, w, h);
}

/** The square dragged by (dx, dy) image pixels, kept inside the image. */
export function moveCrop(c: Crop, dx: number, dy: number, w: number, h: number): Crop {
  return clampCrop({ x: c.x + dx, y: c.y + dy, side: c.side }, w, h);
}

/** The square resized from its bottom-right corner by (dx, dy): top-left fixed, still square and inside the image. */
export function resizeCrop(c: Crop, dx: number, dy: number, w: number, h: number): Crop {
  const side = Math.max(MIN_SIDE, Math.min(c.side + Math.max(dx, dy), w - c.x, h - c.y));
  return clampCrop({ x: c.x, y: c.y, side }, w, h);
}

/** The crop of `source` as portrait.jpg, at most 1024 x 1024, JPEG quality 0.9. */
export function cropJpeg(source: CanvasImageSource, c: Crop, makeCanvas?: () => HTMLCanvasElement): Promise<File> {
  return squareJpeg(source, c.x, c.y, c.side, makeCanvas);
}
