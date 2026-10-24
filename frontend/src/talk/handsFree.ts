/**
 * Hands-free turns (#10): a small voice gate over the microphone level, so the reader can interrupt the character and
 * ask the next question without holding the mic. Runs in the browser on the RMS the capture worklet already reports.
 *
 * #135: by default hands-free now runs on Boson's own turn detection (`semantic_vad` with `create_response: false`,
 * verified live 2026-10-23: the server detects, commits and transcribes the turn, then waits for our response.create,
 * so the answer still carries the per-response retrieval instructions). This gate is still fed while the server
 * decides: the mic is held (nothing sent to the server) while the character speaks and a short tail after, and this
 * gate (strict profile, relative to the echo level) is what hears a barge-in then; it is the whole mechanism again
 * when the server mode is refused, keeps missing turns, or is forced off
 * (`localGateForced`: `?gate=local`, or localStorage `talkback:gate` = `local`). Push-to-talk never uses either.
 * Echo cancellation is on in the capture, so the character's own voice mostly stays out; headphones make it
 * reliable; without them the stricter `HANDS_FREE_OVER_SPEECH` profile applies while the character talks.
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
 * barge-in then needs speech at least this threshold and at least `ECHO_RATIO` times the echo level measured during
 * this playback (see `createEchoFloor`), sustained for a quarter of a second: the character stops within about 300 ms
 * of the reader starting to talk. Since #159 (0.04, 1x echo) the length, not the loudness, is what keeps the echo out;
 * an echo over 0.04 sustained for 250 ms (loud speakers) can still barge in.
 */
// export const HANDS_FREE_OVER_SPEECH: VoiceGateOptions = { threshold: 0.10, minSpeechMs: 250, silenceMs: 700 };
export const HANDS_FREE_OVER_SPEECH: VoiceGateOptions = { threshold: 0.04, minSpeechMs: 250, silenceMs: 700 };
/** A barge-in must be this many times louder than the echo level the mic hears while the character speaks. */
// export const ECHO_RATIO = 2.5;
export const ECHO_RATIO = 1;
/** How much of the recent playback the echo level is taken over (a median, so a burst of the reader's speech does not move it). */
export const ECHO_WINDOW_MS = 2000;
/** The start of a playback during which every mic reading is taken as the character's echo. */
export const ECHO_LEARN_MS = 300;

/**
 * The level of the character's own voice as the mic hears it (what the echo canceller leaves), measured while the
 * character speaks: the median of the readings of the last `ECHO_WINDOW_MS`. The barge-in threshold is relative to it,
 * so a loud speaker never interrupts itself, while the reader, close to the mic, still does.
 */
export function createEchoFloor(windowMs = ECHO_WINDOW_MS) {
  let readings: { t: number; rms: number }[] = [];
  return {
    /**
     * A reading while the character speaks. The first ECHO_LEARN_MS of a playback (the reader has only just heard it
     * start) all count; after that only readings under the barge-in threshold, so the reader's own speech over the
     * character never raises the bar it has to clear.
     */
    feed(rms: number, now: number): void {
      while (readings.length && now - readings[0].t > windowMs) readings.shift();
      const learning = !readings.length || now - readings[0].t < ECHO_LEARN_MS;
      if (learning || rms < this.profile().threshold) readings.push({ t: now, rms });
    },
    median(): number {
      if (!readings.length) return 0;
      const v = readings.map((r) => r.rms).sort((a, b) => a - b);
      return v[Math.floor(v.length / 2)];
    },
    /** The over-speech profile with its threshold raised over the echo heard now. */
    profile(base: VoiceGateOptions = HANDS_FREE_OVER_SPEECH): VoiceGateOptions {
      return { ...base, threshold: Math.max(base.threshold, ECHO_RATIO * this.median()) };
    },
    reset(): void { readings = []; },
  };
}

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

/** The old local gate is forced for hands-free (#135): `?gate=local` in the URL, or localStorage `talkback:gate=local`. */
export function localGateForced(search: string, stored: string | null): boolean {
  try { if (new URLSearchParams(search).get("gate") === "local") return true; } catch { /* a bad query string */ }
  return stored === "local";
}

/** `localGateForced` for this page (storage may be blocked: then only the URL counts). */
export function localGateForcedHere(): boolean {
  let stored: string | null = null;
  try { stored = localStorage.getItem("talkback:gate"); } catch { /* blocked */ }
  return localGateForced(typeof location === "undefined" ? "" : location.search, stored);
}
