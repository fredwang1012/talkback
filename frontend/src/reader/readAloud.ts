import type { SentenceSpeaker } from "../talk/sentencePlayer";

/** How many parts ahead are synthesised while one plays. Prefetching every part at once queued the first request
 * behind dozens of others on the server (4 TTS calls at a time) and timed it out on long recaps. */
export const PREFETCH_AHEAD = 2;

type Run = { done: Promise<void>; stop: () => void; at: () => number };

/** Speak `parts` in order from `start`. `stop` cancels for good: a stopped speaker must not go on to the next part. */
function runSentences(parts: string[], start: number, speaker: SentenceSpeaker): Run {
  let cancelled = false;
  let i = start;
  const done = (async () => {
    for (; i < parts.length; i++) {
      if (cancelled) return;
      parts.slice(i + 1, i + 1 + PREFETCH_AHEAD).forEach((p, k) => speaker.prefetch(p, k));
      await speaker.play(parts[i]);
    }
  })();
  return { done, stop: () => { cancelled = true; speaker.stop(); }, at: () => i };
}

/**
 * Speaks one text at a time (the recap, or an "Earlier books" summary). `toggle(key, parts)` starts it, pauses it
 * when `key` is playing, and resumes a paused text from the part it stopped on. Starting another text pauses the
 * first, so it can be resumed too. `restart` starts a text over from the beginning. A paused text is never spoken
 * on by itself (the recap Stop button used to fall through to the next part).
 */
export function createReadAloud(
  makeSpeaker: () => SentenceSpeaker,
  onChange: (playing: string | null) => void,
  onError: (e: unknown) => void,
) {
  let current: { key: string; parts: string[]; run: Run } | null = null;
  const resumeAt = new Map<string, { sig: string; index: number }>(); // key -> where a paused text stopped
  const sig = (parts: string[]) => parts.join("\u0000");
  const set = (c: typeof current) => { current = c; onChange(c ? c.key : null); };

  const pause = () => {
    const c = current;
    if (!c) return;
    resumeAt.set(c.key, { sig: sig(c.parts), index: Math.min(c.run.at(), c.parts.length - 1) });
    set(null);
    c.run.stop();
  };

  const start = (key: string, parts: string[], from: number) => {
    pause();
    if (!parts.length) return;
    resumeAt.delete(key);
    const run = runSentences(parts, from, makeSpeaker());
    const mine = { key, parts, run };
    set(mine);
    run.done
      .catch((e) => { if (current === mine) { resumeAt.set(key, { sig: sig(parts), index: run.at() }); onError(e); } })
      .then(() => { if (current === mine) set(null); });
  };

  return {
    /** Pause whatever is playing (it can be resumed). */
    stop: pause,
    playing: () => current?.key ?? null,
    /** True when `key` was paused part-way through these same parts. */
    paused: (key: string, parts: string[]) => resumeAt.get(key)?.sig === sig(parts),
    toggle(key: string, parts: string[]) {
      if (current?.key === key) { pause(); return; }
      const saved = resumeAt.get(key);
      start(key, parts, saved && saved.sig === sig(parts) ? saved.index : 0);
    },
    restart(key: string, parts: string[]) {
      resumeAt.delete(key);
      start(key, parts, 0);
    },
  };
}
