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

/** The talk states in which a question or its answer is in progress (the reader is paused for it). */
const ANSWER_STATES = ["listening", "transcribing", "retrieving", "answering"];

/** True while a question asked from read mode, or its answer, is in progress or still sounding: the stage shows the
 * answering face, exactly as Talkback does, instead of the paused reading's face or clip. */
export const answerOnStage = (state: string, speaking: boolean): boolean => speaking || ANSWER_STATES.includes(state);

/**
 * Which face the read-mode stage shows. An answer always wins: the reading face, its lip-sync clip and a chunk's
 * pre-rendered clip are all frozen on the paused reading then, and covered the moving face (the read-mode mic bug).
 */
export function stageFace(s: { answering: boolean; avatar: boolean; reading: boolean; face: boolean }): "avatar" | "read" | "talk" | "portrait" {
  if (s.answering) return s.face ? "talk" : "portrait";
  if (s.avatar) return "avatar";
  if (s.face && s.reading) return "read";
  return s.face ? "talk" : "portrait";
}
