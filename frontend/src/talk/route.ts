/**
 * Who answers a question asked while reading (#34): the character the question is addressed to, when that is one the
 * reader may talk to now (met so far, or anyone with spoilers on); otherwise null, and the last one picked (or the
 * narrator) answers. Only an address counts, not a mention: "Alice, why did you shrink?" goes to Alice, "What do you
 * think of Alice?" stays with whoever was asked. An address is the name
 *  - at the start, followed by a comma (or ?, !, :, a dash) or nothing: "Alice, ..." / "Hey Alice ..." / "Alice?";
 *  - at the end after a comma or dash: "..., Alice?";
 *  - after "ask", "talk to", "speak to/with", "question for" at the start: "Ask the Hatter why ...", "Can I speak with
 *    Alice?" (not "Why did the Queen ask Alice ...?").
 * A name may be given in full, by an alias, or by one of its words ("Hatter" for "The Mad Hatter") unless another
 * candidate shares that word. Matching ignores case and works for any script.
 */

export type RouteCandidate = { id: string; name: string; aliases?: string[] };

const FILLERS = "hey|hi|hello|ok|okay|so|and|now|oh|well|dear|um|uh|excuse me";
const GREETINGS = "hey|hi|hello|dear|oh";
const CUES = "ask|asking|talk to|talk with|speak to|speak with|question for|let me talk to|i want to talk to";
// what may come before a cue at the start: "Can I speak with ...", "Could you ask ...", "I'd like to talk to ..."
const REQUESTS = "please|can i|could i|may i|can you|could you|would you|will you|i want to|i'd like to|i would like to|let me|i have a";
// words that never name anyone on their own ("Mr" of "Mr Dursley", "the" of "the Queen")
const SKIP = new Set(["the", "a", "an", "of", "and", "mr", "mrs", "ms", "miss", "mister", "dr", "sir", "madam", "lady", "lord", "de", "la", "le", "von", "van", "du", "da"]);

const NOT_WORD = "(?![\\p{L}\\p{N}])";
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
const words = (s: string) => s.toLowerCase().match(/[\p{L}\p{N}'’]+/gu) ?? [];

/** Every way a candidate can be called: full name, aliases, and its own (unshared, meaningful) name words. */
function variants(c: RouteCandidate, shared: Set<string>): string[] {
  const full = [c.name, ...(c.aliases ?? [])].map((s) => s.trim()).filter(Boolean);
  const parts = full.flatMap(words).filter((w) => w.length >= 3 && !SKIP.has(w) && !shared.has(w));
  return [...new Set([...full.map((s) => s.toLowerCase()), ...parts])];
}

/** Where in `q` the variant is used as an address (the match index), or -1. */
function addressAt(q: string, v: string): number {
  const name = `(?:the\\s+)?${escape(v)}${NOT_WORD}`;
  // full-width and ideographic punctuation too ("ナルト、..."); the dash stays last in each class (no range)
  const patterns = [
    new RegExp(`^\\s*(?:(?:${FILLERS})[\\s,]+)*${name}\\s*(?:[,:!?.\u2014\u2013\u3001\uff0c\uff1a\uff01\uff1f\u3002-]|$)`, "iu"),
    new RegExp(`^\\s*(?:${GREETINGS})[\\s,]+${name}`, "iu"),
    new RegExp(`[,\u2014\u2013\u3001\uff0c-]\\s*${name}\\s*[?.!\u2026\uff1f\uff01\u3002]*\\s*$`, "iu"),
    // a cue at the start only ("Why did the Queen ask Alice ..." is about Alice, not to her)
    new RegExp(`^\\s*(?:(?:${FILLERS})[\\s,]+)*(?:(?:${REQUESTS})\\s+)*(?:${CUES})\\s+${name}`, "iu"),
  ];
  let best = -1;
  for (const p of patterns) {
    const m = p.exec(q);
    if (m && (best < 0 || m.index < best)) best = m.index;
  }
  return best;
}

/** The id of the candidate `question` is addressed to, or null when it names none of them as an address. */
export function addressedSpeaker(question: string, candidates: RouteCandidate[]): string | null {
  const q = question.trim().slice(0, 500); // a typed question is at most 500 characters; a transcript is bounded the same
  if (!q || candidates.length === 0) return null;
  const counts = new Map<string, number>();
  for (const c of candidates) for (const w of new Set([c.name, ...(c.aliases ?? [])].flatMap(words))) counts.set(w, (counts.get(w) ?? 0) + 1);
  const shared = new Set([...counts].filter(([, n]) => n > 1).map(([w]) => w));
  let pick: { id: string; at: number; len: number } | null = null;
  for (const c of candidates) {
    for (const v of variants(c, shared)) {
      const at = addressAt(q, v);
      if (at < 0) continue;
      if (!pick || at < pick.at || (at === pick.at && v.length > pick.len)) pick = { id: c.id, at, len: v.length };
    }
  }
  return pick?.id ?? null;
}
