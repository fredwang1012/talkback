import type { SentenceSpeaker } from "../talk/sentencePlayer";

type Run = { done: Promise<void>; stop: () => void };

/** Speak the parts in order. `stop` cancels for good: a stopped speaker must not go on to the next part. */
function runSentences(parts: string[], speaker: SentenceSpeaker): Run {
  let cancelled = false;
  const done = (async () => {
    parts.slice(1).forEach((p) => speaker.prefetch(p)); // the later parts synthesise while the first plays
    for (const p of parts) {
      if (cancelled) return;
      await speaker.play(p);
    }
  })();
  return { done, stop: () => { cancelled = true; speaker.stop(); } };
}

/**
 * Speaks one text at a time (the recap, or an "Earlier books" summary). `toggle(key, parts)` starts it, or stops it
 * when `key` is already playing; starting another one stops the first. Stopping is final: the remaining parts of a
 * stopped text are never spoken (the recap Stop button used to fall through to the next part).
 */
export function createReadAloud(
  makeSpeaker: () => SentenceSpeaker,
  onChange: (playing: string | null) => void,
  onError: (e: unknown) => void,
) {
  let current: { key: string; run: Run } | null = null;
  const set = (c: typeof current) => { current = c; onChange(c ? c.key : null); };
  const stop = () => {
    const c = current;
    if (!c) return;
    set(null);
    c.run.stop();
  };
  return {
    stop,
    playing: () => current?.key ?? null,
    toggle(key: string, parts: string[]) {
      if (current?.key === key) { stop(); return; }
      stop();
      if (!parts.length) return;
      const run = runSentences(parts, makeSpeaker());
      const mine = { key, run };
      set(mine);
      run.done.catch((e) => { if (current === mine) onError(e); }).then(() => { if (current === mine) set(null); });
    },
  };
}
