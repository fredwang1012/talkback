/**
 * How a Realtime answer is voiced (B1). "text" (the default): the model writes the answer (output_modalities
 * ["text"]) and each sentence is spoken by Higgs TTS with an emotion tag in the speaker's voice, lip-synced like a
 * Gemini answer. "audio": the old direct path, Boson speaks the answer itself as PCM (lower latency, no tags, no
 * lip-sync). `?answers=audio` in the URL, or localStorage `talkback:answers` = `audio`, brings the old path back for a
 * comparison; the URL wins over the stored value.
 */
export type AnswerMode = "text" | "audio";

export function answerMode(search: string, stored: string | null): AnswerMode {
  let fromUrl: string | null = null;
  try { fromUrl = new URLSearchParams(search).get("answers"); } catch { /* a bad query string */ }
  const pick = (fromUrl ?? stored ?? "").trim().toLowerCase();
  return pick === "audio" ? "audio" : "text";
}

export function answerModeHere(): AnswerMode {
  let stored: string | null = null;
  try { stored = localStorage.getItem("talkback:answers"); } catch { /* blocked */ }
  return answerMode(typeof location === "undefined" ? "" : location.search, stored);
}
