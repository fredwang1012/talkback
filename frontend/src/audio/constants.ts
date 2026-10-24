/**
 * PCM16 mono at 24 kHz in both directions. The playback AudioContext also runs at 24000, so one sample out of the
 * API is exactly one sample into the speakers; that one-to-one mapping is what makes `audio_end_ms` exact on barge-in.
 */
export const AUDIO_RATE = 24000;
