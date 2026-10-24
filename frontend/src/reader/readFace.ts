/**
 * Read-mode face timing (#66). The talking loop is a <video>: if it is created at the moment the voice starts it has
 * to load before its mouth can move, so the face starts late. So the reading face is mounted as soon as playback is
 * asked for (also while the first chunk's audio is still loading), and the loops of the next chunk's reader are
 * preloaded, so a change of speaker does not start late either.
 */

type Loops = { id: string; talk: string | null; idle: string | null };

/** Whether the reading face (rather than the Talk face) is up: whenever the reader is playing or loading to play,
 * not only once the audio has started (that was too late for the loop video to be ready). */
export const showReadingFace = (state: { playing: boolean; loading: boolean }): boolean => state.playing || state.loading;

/** Loop videos to warm up now: the next chunk's reader's loops, unless it is the face already on screen. */
export function loopsToPreload(current: Loops | null | undefined, next: Loops | null | undefined): string[] {
  if (!next || next.id === current?.id) return [];
  return [next.talk, next.idle].filter((u): u is string => !!u);
}
