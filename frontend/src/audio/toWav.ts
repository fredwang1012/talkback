/**
 * Voice cloning only accepts wav, mp3, opus, pcm and flac (#108). Browsers record webm/ogg, so a recording (or an
 * upload in another format) is decoded and re-encoded as mono PCM16 WAV before it is sent.
 */

const ACCEPTED = /\.(wav|mp3|flac)$/i;

export function needsWav(filename: string): boolean {
  return !ACCEPTED.test(filename);
}

/** Mono PCM16 WAV from float samples (channels averaged). */
export function encodeWav(channels: Float32Array[], rate: number): Uint8Array<ArrayBuffer> {
  const n = channels[0]?.length ?? 0;
  const out = new Uint8Array(44 + n * 2);
  const v = new DataView(out.buffer);
  const tag = (o: number, s: string) => { for (let i = 0; i < s.length; i++) out[o + i] = s.charCodeAt(i); };
  tag(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); tag(8, "WAVE"); tag(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  tag(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (const c of channels) s += c[i];
    s = Math.max(-1, Math.min(1, s / channels.length));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return out;
}

/** The clip as WAV when Boson would not take it as is; otherwise unchanged. */
export async function toCloneAudio(blob: Blob, filename: string): Promise<{ blob: Blob; filename: string }> {
  if (!needsWav(filename)) return { blob, filename };
  const ctx = new AudioContext();
  try {
    const audio = await ctx.decodeAudioData(await blob.arrayBuffer());
    const channels = Array.from({ length: audio.numberOfChannels }, (_, i) => audio.getChannelData(i));
    const base = filename.replace(/\.[^.]*$/, "") || "recording";
    return { blob: new Blob([encodeWav(channels, audio.sampleRate)], { type: "audio/wav" }), filename: `${base}.wav` };
  } catch {
    throw new Error("Could not read that recording. Try again, or upload a WAV, MP3 or FLAC file.");
  } finally {
    void ctx.close();
  }
}
