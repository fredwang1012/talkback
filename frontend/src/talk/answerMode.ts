/**
 * How a Realtime answer is voiced (B1). "audio" (the default): Boson speaks the answer itself as PCM (lowest
 * latency, about 0.6 s to first audio; no emotion tags, no per-sentence lip-sync). "text" (opt-in): the model writes the answer (output_modalities
 * ["text"]) and each sentence is spoken by Higgs TTS with an emotion tag in the speaker's voice, lip-synced like a
 * Gemini answer, about 2.8 s to first audio. `?answers=text` in the URL, or localStorage `talkback:answers` = `text`,
 * turns it on; the URL wins over the stored value.
 */
export type AnswerMode = "text" | "audio";

export function answerMode(search: string, stored: string | null): AnswerMode {
  let fromUrl: string | null = null;
  try { fromUrl = new URLSearchParams(search).get("answers"); } catch { /* a bad query string */ }
  const pick = (fromUrl ?? stored ?? "").trim().toLowerCase();
  return pick === "text" ? "text" : "audio";
}

export function answerModeHere(): AnswerMode {
  let stored: string | null = null;
  try { stored = localStorage.getItem("talkback:answers"); } catch { /* blocked */ }
  return answerMode(typeof location === "undefined" ? "" : location.search, stored);
}
