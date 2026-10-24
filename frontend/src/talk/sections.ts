/**
 * Finding places in a book by section, for the voice agent's tools (tools.ts). Pure and local: no model, no API.
 * Textbook sections nest ("2", "2.3", "2.3.1"); a section is found by its number, by words from its title, or, for a
 * topic, by a small keyword search (tf-idf with a plural stemmer) over the paragraphs that are read aloud.
 */

export type SectionLike = { id: string; title: string; start_para: number; level?: number };
export type ParagraphLike = { idx: number; text: string; kind?: string | null };

const STOP = new Set(("a an the of and or for to in on at by with from into is are be been how what which that this these those it its as via vs " +
  "about do does we our you your can").split(" "));

/** A tiny English stemmer for plurals: "indexes" -> "index", "queries" -> "query", "words" -> "word". */
export function stem(w: string): string {
  if (w.length > 4 && w.endsWith("ies")) return w.slice(0, -3) + "y";
  if (w.length > 4 && /(?:xes|ches|shes|sses|zes)$/.test(w)) return w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !/(?:ss|us|is)$/.test(w)) return w.slice(0, -1);
  return w;
}

/** Content words, stemmed (stop words and apostrophes dropped). */
export function tokens(text: string): string[] {
  return text.toLowerCase().replace(/[’']/g, "").split(/[^a-z0-9]+/).filter((w) => w && !STOP.has(w)).map(stem);
}

/** The number a section title starts with: "2.3 Wildcards" -> "2.3", "Chapter 4: X" -> "4"; null when unnumbered. */
export function titleNumber(title: string): string | null {
  const t = title.trim();
  const m = /^(?:chapter|section|part)?\s*(\d+(?:\.\d+)*)\.?(?=\s|:|$)/i.exec(t);
  return m ? m[1] : null;
}

/** The title without its leading number: "3.2 Wildcard queries" -> "Wildcard queries". */
export function titleText(title: string): string {
  return title.trim().replace(/^(?:chapter|section|part)?\s*\d+(?:\.\d+)*\.?\s*[:.\-–—]?\s*/i, "") || title.trim();
}

const cmpNum = (a: string, b: string) => {
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] ?? -1) - (y[i] ?? -1);
    if (d) return d;
  }
  return 0;
};

/**
 * The section numbered `num` ("2.3", "3"), at any depth. When no title carries that number but it falls in a gap of the
 * numbering with exactly one unnumbered section in it (a heading the PDF parse lost its number on), that section.
 */
export function findSectionByNumber<S extends SectionLike>(sections: S[], num: string): S | null {
  const nums = sections.map((s) => titleNumber(s.title));
  const exact = sections.find((_, i) => nums[i] === num);
  if (exact) return exact;
  const after = nums.findIndex((n) => n !== null && cmpNum(n, num) > 0);
  if (after <= 0) return null;
  let before = after - 1;
  while (before >= 0 && nums[before] === null) before--;
  if (before < 0) return null;
  const gap = sections.slice(before + 1, after);
  return gap.length === 1 ? gap[0] : null;
}

/** The deepest section containing paragraph `para` (the last one starting at or before it). */
export function sectionAt<S extends SectionLike>(sections: S[], para: number): S | null {
  let found: S | null = null;
  for (const s of sections) if (s.start_para <= para) found = s;
  return found;
}

/**
 * The section whose title best matches `query` (the share of the query's words in the title, then the share of the
 * title covered). Ties ("References and further reading" in every chapter) go to the first one from the reader's section
 * on, else the last one before it. Null when no word matches.
 */
export function findSectionByTitle<S extends SectionLike>(sections: S[], query: string, readerPara: number): { section: S; score: number } | null {
  const q = [...new Set(tokens(query))];
  if (!q.length) return null;
  const scored = sections.map((s) => {
    const t = new Set(tokens(titleText(s.title)));
    const hit = q.filter((w) => t.has(w)).length;
    return { s, score: hit / q.length, cover: t.size ? hit / t.size : 0 };
  }).filter((x) => x.score > 0);
  if (!scored.length) return null;
  const best = scored.reduce((b, x) => (x.score > b.score || (x.score === b.score && x.cover > b.cover) ? x : b));
  const ties = scored.filter((x) => x.score === best.score && x.cover === best.cover);
  const from = sectionAt(sections, readerPara)?.start_para ?? 0;
  const pick = ties.find((x) => x.s.start_para >= from) ?? ties[ties.length - 1];
  return { section: pick.s, score: pick.score };
}

/** The next or previous section (any depth, or chapters only) from paragraph `para`; null at either end. */
export function adjacentSection<S extends SectionLike>(sections: S[], para: number, dir: "next" | "previous", unit: "section" | "chapter" = "section"): S | null {
  const list = unit === "chapter" ? sections.filter((s) => (s.level ?? 1) <= 1) : sections;
  const cur = sectionAt(list, para);
  if (dir === "next") return list.find((s) => s.start_para > (cur?.start_para ?? -1)) ?? null;
  if (!cur) return null;
  return list.filter((s) => s.start_para < cur.start_para).at(-1) ?? null;
}

export type TopicHit<S> = { section: S | null; para: number; via: "title" | "text" };

/**
 * Where the book covers `query`: a section whose title has every query word, else the paragraph that matches best
 * (tf-idf, half the query words at least, a bonus for the whole phrase and for a matching section title; ties go to the
 * earliest paragraph, where a topic is usually introduced), else a section title with half the words. Code listings
 * are not searched; nothing past `maxPara` is (the novel spoiler limit).
 */
export function findTopic<S extends SectionLike>(sections: S[], paragraphs: ParagraphLike[], query: string, opts: { maxPara?: number; readerPara?: number } = {}): TopicHit<S> | null {
  const maxPara = opts.maxPara ?? Number.POSITIVE_INFINITY;
  const q = [...new Set(tokens(query))];
  if (!q.length) return null;
  const allowed = sections.filter((s) => s.start_para <= maxPara);
  const title = findSectionByTitle(allowed, query, opts.readerPara ?? 0);
  if (title && title.score === 1) return { section: title.section, para: title.section.start_para, via: "title" };

  const docs = paragraphs.filter((p) => p.kind !== "code" && p.idx <= maxPara).map((p) => {
    const tf = new Map<string, number>();
    for (const w of tokens(p.text)) tf.set(w, (tf.get(w) ?? 0) + 1);
    return { p, tf };
  });
  const n = docs.length;
  const idf = new Map(q.map((w) => {
    const df = docs.filter((d) => d.tf.has(w)).length;
    return [w, Math.log(1 + (n - df + 0.5) / (df + 0.5))];
  }));
  const sumIdf = q.reduce((a, w) => a + (idf.get(w) ?? 0), 0);
  const phrase = q.join(" ");
  let best: { para: number; score: number } | null = null;
  for (const { p, tf } of docs) {
    const hit = q.filter((w) => tf.has(w));
    if (!hit.length || hit.length * 2 < q.length) continue;
    let score = hit.reduce((a, w) => a + (idf.get(w) ?? 0) * (1 + Math.log(tf.get(w) as number)), 0);
    if (q.length > 1 && tokens(p.text).join(" ").includes(phrase)) score += sumIdf;
    const sec = sectionAt(sections, p.idx);
    if (sec) { const t = new Set(tokens(titleText(sec.title))); score += 0.5 * sumIdf * (q.filter((w) => t.has(w)).length / q.length); }
    if (!best || score > best.score) best = { para: p.idx, score };
  }
  if (best) return { section: sectionAt(sections, best.para), para: best.para, via: "text" };
  if (title && title.score >= 0.5) return { section: title.section, para: title.section.start_para, via: "title" };
  return null;
}

/** The paragraph where section `s` ends (exclusive): the next section at its depth or shallower; Infinity for the last. */
export function sectionEnd<S extends SectionLike>(sections: S[], s: S): number {
  const i = sections.indexOf(s);
  const next = sections.slice(i + 1).find((x) => (x.level ?? 1) <= (s.level ?? 1) && x.start_para > s.start_para);
  return next ? next.start_para : Number.POSITIVE_INFINITY;
}
