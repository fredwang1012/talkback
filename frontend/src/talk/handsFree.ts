/**
 * Hands-free turns (#10): a small voice gate over the microphone level, so the reader can interrupt the character and
 * ask the next question without holding the mic. Runs in the browser on the RMS the capture worklet already reports.
 *
 * Why not Boson's `server_vad`: with it the server answers every turn by itself (`create_response` is accepted but has
 * no effect), so the spoken answer would lose the per-response retrieval instructions that keep it spoiler-safe and in
 * character. Our turns stay manual (`turn_detection: null`, `input_audio_buffer.commit`); this gate decides when a turn
 * starts (barge-in) and ends. Echo cancellation is on in the capture, so the character's own voice mostly stays out;
 * headphones make it reliable; without them the stricter `HANDS_FREE_OVER_SPEECH` profile applies while the character talks.
 *
 * Tuned for laptop microphones (quiet, some fan noise): RMS of float samples, speech is typically 0.05-0.2, room noise
 * 0.005-0.02. Speech must last `minSpeechMs` to count (a cough or a click does not), and a turn ends after `silenceMs`
 * of quiet (long enough for a breath mid-question).
 */

export type VoiceGateOptions = { threshold: number; minSpeechMs: number; silenceMs: number };
export const HANDS_FREE: VoiceGateOptions = { threshold: 0.04, minSpeechMs: 150, silenceMs: 700 };
/**
 * While the character is speaking through the speakers (#103): what the browser's echo canceller leaves of their
 * voice is quiet (well under 0.05 on a laptop) but not nothing, and a cough or a click must not cut them off. A
 * barge-in then needs speech clearly louder than that residue, sustained for most of half a second.
 */
export const HANDS_FREE_OVER_SPEECH: VoiceGateOptions = { threshold: 0.10, minSpeechMs: 400, silenceMs: 700 };

export type VoiceGateEvent = "start" | "stop" | null;

export function createVoiceGate(o: VoiceGateOptions = HANDS_FREE) {
  let speaking = false;
  let above: number | null = null; // when the level first went over the threshold
  let below: number | null = null; // when it first dropped under it, while speaking
  return {
    /**
     * Feed one level reading at time `now` (ms); returns "start" when speech begins, "stop" when it has ended.
     * `profile` replaces the thresholds for this reading (the stricter one while the character talks).
     */
    feed(rms: number, now: number, profile: VoiceGateOptions = o): VoiceGateEvent {
      if (!speaking) {
        if (rms >= profile.threshold) {
          above ??= now;
          if (now - above >= profile.minSpeechMs) { speaking = true; below = null; return "start"; }
        } else above = null;
        return null;
      }
      if (rms < profile.threshold) {
        below ??= now;
        if (now - below >= profile.silenceMs) { speaking = false; above = null; below = null; return "stop"; }
      } else below = null;
      return null;
    },
    get speaking(): boolean { return speaking; },
    reset(): void { speaking = false; above = null; below = null; },
  };
}
