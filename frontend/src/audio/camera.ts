/** Webcam portrait capture helpers (issue #107). Kept free of React so they can be unit-tested. */

export const MAX_PORTRAIT = 1024;

/** Plain explanation for a camera getUserMedia failure, with how to fix it. */
export function cameraErrorMessage(e: unknown): string {
  const name = typeof e === "object" && e !== null ? (e as { name?: string }).name : undefined;
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Camera access is blocked. Click the lock or camera icon in the address bar, allow the camera, then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "Camera not found. Plug one in or check your system camera settings, then try again.";
  if (name === "NotReadableError" || name === "AbortError")
    return "Camera busy or unavailable. Close other apps using it, then try again.";
  return "Camera could not be started. Check the browser's camera permission, then try again.";
}

export function stopStream(stream: { getTracks(): { stop(): void }[] } | null | undefined): void {
  stream?.getTracks().forEach((t) => t.stop());
}

/** Square center-crop box for a w x h frame, and the output edge (at most MAX_PORTRAIT). */
export function squareCrop(w: number, h: number): { sx: number; sy: number; side: number; out: number } {
  const side = Math.min(w, h);
  return { sx: Math.floor((w - side) / 2), sy: Math.floor((h - side) / 2), side, out: Math.min(side, MAX_PORTRAIT) };
}

/** Draw the square (sx, sy, side) of `source` at most MAX_PORTRAIT px wide and encode it as portrait.jpg (quality 0.9). */
export async function squareJpeg(
  source: CanvasImageSource, sx: number, sy: number, side: number,
  makeCanvas: () => HTMLCanvasElement = () => document.createElement("canvas"),
): Promise<File> {
  const out = Math.min(side, MAX_PORTRAIT);
  const canvas = makeCanvas();
  canvas.width = out;
  canvas.height = out;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Could not prepare the photo in this browser.");
  ctx.drawImage(source, sx, sy, side, side, 0, 0, out, out);
  const blob = await new Promise<Blob | null>((res) => canvas.toBlob(res, "image/jpeg", 0.9));
  if (!blob) throw new Error("Could not prepare the photo in this browser.");
  return new File([blob], "portrait.jpg", { type: "image/jpeg" });
}

/** Grab the current video frame, center-crop to a square and encode as portrait.jpg. The frame is not mirrored. */
export async function captureJpeg(
  video: { videoWidth: number; videoHeight: number },
  makeCanvas?: () => HTMLCanvasElement,
): Promise<File> {
  const { sx, sy, side } = squareCrop(video.videoWidth, video.videoHeight);
  if (!side) throw new Error("The camera has no picture yet. Wait a moment and try again.");
  return squareJpeg(video as unknown as CanvasImageSource, sx, sy, side, makeCanvas);
}
