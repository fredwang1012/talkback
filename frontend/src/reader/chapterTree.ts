export type SectionLike = { id: string; title: string; start_para: number; level?: number };
export type ChapterRow = { index: number; depth: number; hasChildren: boolean; open: boolean };

const levelOf = (s: SectionLike) => Math.max(1, s.level ?? 1);

/** True when any section is nested under another (textbooks); novels stay a flat list. */
export function isNested(sections: SectionLike[]): boolean {
  return sections.some((s) => levelOf(s) > 1);
}

/** Indices of the sections that contain section `i` (nearest parent first). */
export function ancestorsOf(sections: SectionLike[], i: number): number[] {
  const out: number[] = [];
  let level = levelOf(sections[i]);
  for (let j = i - 1; j >= 0 && level > 1; j--) {
    if (levelOf(sections[j]) < level) { out.push(j); level = levelOf(sections[j]); }
  }
  return out;
}

/** The rows to show: a section is visible when all of its ancestors are open. `open` holds section indices. */
export function visibleRows(sections: SectionLike[], open: ReadonlySet<number>): ChapterRow[] {
  const rows: ChapterRow[] = [];
  const stack: { level: number; open: boolean }[] = []; // ancestors of the section being visited
  sections.forEach((s, index) => {
    const level = levelOf(s);
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    const hasChildren = index + 1 < sections.length && levelOf(sections[index + 1]) > level;
    if (stack.every((a) => a.open)) rows.push({ index, depth: stack.length, hasChildren, open: open.has(index) });
    stack.push({ level, open: open.has(index) });
  });
  return rows;
}

/** `open` with the chapter holding the current position (and its ancestors) expanded. */
export function openAround(sections: SectionLike[], current: number, open: ReadonlySet<number>): Set<number> {
  const next = new Set(open);
  next.add(current);
  ancestorsOf(sections, current).forEach((a) => next.add(a));
  return next;
}
