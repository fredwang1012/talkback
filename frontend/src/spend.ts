/** Running estimate of Boson spend this session (brief section 8 "cost guard"). Rates from docs.boson.ai/pricing. */
export const RATES = {
  ttsPer1kChars: 0.015,
  audioInPerMin: 0.0023,
  audioOutPerMin: 0.014,
  transcriptionPerMin: 0.0025,
  avatarPerMin: 0.09, // Higgs Avatar at the default 640x640; see AVATAR_PER_MIN for the other sizes
};
/** Higgs Avatar $/min by output size (docs.boson.ai/pricing); scripts/usage.py has the same table. */
export const AVATAR_PER_MIN: Record<string, number> = { "640x640": 0.09, "480x640": 0.07, "640x480": 0.07 };
const BYTES_PER_SECOND = 24000 * 2; // PCM16 mono @ 24 kHz

export type SpendSnapshot = { ttsChars: number; inSeconds: number; outSeconds: number; avatarSeconds: number; usd: number };

export class SpendTracker {
  private ttsChars = 0;
  private inBytes = 0;
  private outBytes = 0;
  private avatarSeconds = 0;
  private avatarRate: number = RATES.avatarPerMin;
  private listeners = new Set<(s: SpendSnapshot) => void>();

  /** Only chunks Boson actually synthesised cost money; cache hits are free. */
  addTts(chars: number): void { this.ttsChars += chars; this.notify(); }
  addAudioIn(base64Length: number): void { this.inBytes += (base64Length * 3) / 4; this.notify(); }
  addAudioOut(base64Length: number): void { this.outBytes += (base64Length * 3) / 4; this.notify(); }
  /** The lip-sync output size the server renders at (from /api/health); an unknown size is priced as 640x640. */
  setAvatarSize(size: string): void { this.avatarRate = AVATAR_PER_MIN[size] ?? RATES.avatarPerMin; this.notify(); }
  /** Seconds of lip-sync video freshly rendered (cached clips are free). */
  addAvatar(seconds: number): void { this.avatarSeconds += seconds; this.notify(); }
  reset(): void { this.ttsChars = this.inBytes = this.outBytes = this.avatarSeconds = 0; this.notify(); }

  snapshot(): SpendSnapshot {
    const inSeconds = this.inBytes / BYTES_PER_SECOND;
    const outSeconds = this.outBytes / BYTES_PER_SECOND;
    const usd =
      (this.ttsChars / 1000) * RATES.ttsPer1kChars +
      (inSeconds / 60) * (RATES.audioInPerMin + RATES.transcriptionPerMin) +
      (outSeconds / 60) * RATES.audioOutPerMin +
      (this.avatarSeconds / 60) * this.avatarRate;
    return { ttsChars: this.ttsChars, inSeconds, outSeconds, avatarSeconds: this.avatarSeconds, usd };
  }

  subscribe(fn: (s: SpendSnapshot) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private notify(): void {
    const s = this.snapshot();
    this.listeners.forEach((fn) => fn(s));
  }
}

export const spend = new SpendTracker();
