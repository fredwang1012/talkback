import type { SentenceSpeaker } from "../talk/sentencePlayer";

/** /api/speak takes at most 400 characters, so a recap is spoken a few sentences at a time. */
export function speechParts(text: string, max = 380): string[] {
  const parts: string[] = [];
  let cur = "";
  for (const s of text.replace(/\s+/g, " ").trim().match(/[^.!?]+[.!?]*\s*/g) ?? []) {
    if (cur && cur.length + s.length > max) { parts.push(cur.trim()); cur = ""; }
    cur += s;
    while (cur.length > max) { parts.push(cur.slice(0, max).trim()); cur = cur.slice(max); }
  }
  if (cur.trim()) parts.push(cur.trim());
  return parts;
}

/** How many parts ahead are synthesised while one plays. Prefetching every part at once queued the first request
 * behind dozens of others on the server (4 TTS calls at a time) and timed it out on long recaps. */
export const PREFETCH_AHEAD = 2;

type Run = { done: Promise<void>; stop: () => void; at: () => number; pause?: () => void; resume: () => boolean };

/** Speak `parts` in order from `start`. `stop` cancels for good: a stopped speaker must not go on to the next part.
 * `pause` / `resume` hold the speaker at this moment (only when the speaker can; `resume` false: it cannot go on). */
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
  return {
    done, stop: () => { cancelled = true; speaker.stop(); }, at: () => i,
    pause: speaker.pause ? () => speaker.pause?.() : undefined,
    resume: () => !cancelled && !!speaker.resume?.(),
  };
}

/** How many paused texts keep their clip held (beyond that they resume from the start of their part). */
const MAX_HELD = 3;

/**
 * Speaks one text at a time (a narrator recap in the Talkback chat). `toggle(key, parts)` starts it, pauses it
 * when `key` is playing, and resumes a paused text. Pause holds the speaker at that moment and Resume continues from
 * there, mid-sentence (#88); only if the clip is gone (or the speaker cannot pause) does it restart the part it was
 * on. Starting another text pauses the first, so it can be resumed too. `restart` starts a text over. A paused text is
 * never spoken on by itself. `reset` stops everything for good.
 */
export function createReadAloud(
  makeSpeaker: () => SentenceSpeaker,
  onChange: (playing: string | null) => void,
  onError: (e: unknown) => void,
) {
  let current: { key: string; parts: string[]; run: Run } | null = null;
  const resumeAt = new Map<string, { sig: string; index: number }>(); // key -> the part a paused text stopped on
  const held = new Map<string, { sig: string; run: Run }>(); // key -> a paused run still holding its clip
  const sig = (parts: string[]) => parts.join("\u0000");
  const set = (c: typeof current) => { current = c; onChange(c ? c.key : null); };
  const drop = (key: string) => { const h = held.get(key); if (h) { held.delete(key); h.run.stop(); } };

  const pause = () => {
    const c = current;
    if (!c) return;
    resumeAt.set(c.key, { sig: sig(c.parts), index: Math.min(c.run.at(), c.parts.length - 1) });
    set(null);
    if (!c.run.pause) { c.run.stop(); return; }
    c.run.pause();
    held.delete(c.key);
    held.set(c.key, { sig: sig(c.parts), run: c.run });
    while (held.size > MAX_HELD) drop(held.keys().next().value as string);
  };

  const watch = (key: string, parts: string[], run: Run) => {
    const mine = { key, parts, run };
    set(mine);
    run.done
      .catch((e) => { if (current?.run === run) { resumeAt.set(key, { sig: sig(parts), index: run.at() }); onError(e); } })
      .then(() => { if (current?.run === run) set(null); if (held.get(key)?.run === run) held.delete(key); });
  };

  const start = (key: string, parts: string[], from: number) => {
    pause();
    drop(key);
    if (!parts.length) return;
    resumeAt.delete(key);
    watch(key, parts, runSentences(parts, from, makeSpeaker()));
  };

  return {
    /** Pause whatever is playing (it can be resumed). */
    stop: pause,
    /** Stop everything for good: nothing playing, nothing held, nothing to resume. */
    reset() {
      const c = current;
      set(null);
      c?.run.stop();
      for (const k of [...held.keys()]) drop(k);
      resumeAt.clear();
    },
    playing: () => current?.key ?? null,
    /** True when `key` was paused part-way through these same parts. */
    paused: (key: string, parts: string[]) => resumeAt.get(key)?.sig === sig(parts) || held.get(key)?.sig === sig(parts),
    toggle(key: string, parts: string[]) {
      if (current?.key === key) { pause(); return; }
      const h = held.get(key);
      if (h && h.sig === sig(parts)) {
        pause();
        held.delete(key);
        if (h.run.resume()) { resumeAt.delete(key); watch(key, parts, h.run); return; }
        h.run.stop(); // the clip is gone: go back to the start of the part it was on
      }
      const saved = resumeAt.get(key);
      start(key, parts, saved && saved.sig === sig(parts) ? saved.index : 0);
    },
    restart(key: string, parts: string[]) {
      resumeAt.delete(key);
      start(key, parts, 0);
    },
  };
}
