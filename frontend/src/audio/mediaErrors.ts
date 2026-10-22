/** Browser media/permission errors turned into plain messages (issue #4: raw NotAllowedError text reached the user). */

export const TAP_TO_PLAY = "Tap play to continue. Your browser paused the audio until you interact with the page.";

/** True for the DOMException the browser throws when autoplay or a permission is refused. */
export function isNotAllowed(e: unknown): boolean {
  return typeof e === "object" && e !== null && (e as { name?: string }).name === "NotAllowedError";
}

/** A refused play(): not an error the reader should see, just a state that needs a tap. */
export function isAutoplayBlock(e: unknown): boolean {
  return isNotAllowed(e);
}

/** Plain explanation for a getUserMedia failure, with how to fix it. */
export function micErrorMessage(e: unknown): string {
  const name = typeof e === "object" && e !== null ? (e as { name?: string }).name : undefined;
  if (name === "NotAllowedError" || name === "SecurityError")
    return "Microphone access is blocked. Click the lock or camera icon in the address bar, allow the microphone, then try again.";
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return "Microphone not found. Plug one in or check your system sound settings, then try again.";
  if (name === "NotReadableError" || name === "AbortError")
    return "Microphone busy or unavailable. Close other apps using it, then try again.";
  return "Microphone could not be started. Check the browser's microphone permission, then try again.";
}

/** Play an element and swallow an autoplay refusal (muted decorative video). Resolves true if it is playing. */
export async function tryPlayQuietly(el: { play(): Promise<void> | void }): Promise<boolean> {
  try { await el.play(); return true; } catch { return false; }
}
