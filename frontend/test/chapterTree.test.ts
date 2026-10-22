import { describe, expect, it } from "vitest";
import { ancestorsOf, isNested, openAround, visibleRows } from "../src/reader/chapterTree";

const S = (title: string, level?: number) => ({ id: title, title, start_para: 0, level });
const sections = [S("1", 1), S("1.1", 2), S("1.1.1", 3), S("1.1.2", 3), S("1.2", 2), S("2", 1), S("2.1", 2)];

describe("chapter tree", () => {
  it("old books without levels are flat", () => {
    expect(isNested([S("a"), S("b")])).toBe(false);
    expect(isNested(sections)).toBe(true);
  });
  it("finds ancestors nearest first", () => {
    expect(ancestorsOf(sections, 3)).toEqual([1, 0]);
    expect(ancestorsOf(sections, 5)).toEqual([]);
  });
  it("collapsed shows only top-level rows; open shows children", () => {
    expect(visibleRows(sections, new Set()).map((r) => r.index)).toEqual([0, 5]);
    const rows = visibleRows(sections, new Set([0, 1]));
    expect(rows.map((r) => r.index)).toEqual([0, 1, 2, 3, 4, 5]);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 2, 2, 1, 0]);
    expect(rows[0]).toMatchObject({ hasChildren: true, open: true });
    expect(rows[2].hasChildren).toBe(false);
  });
  it("a closed parent hides its open grandchildren", () => {
    expect(visibleRows(sections, new Set([1])).map((r) => r.index)).toEqual([0, 5]);
  });
  it("expands the chapter around the current position", () => {
    const open = openAround(sections, 3, new Set());
    expect([...open].sort()).toEqual([0, 1, 3]);
    expect(visibleRows(sections, open).map((r) => r.index)).toEqual([0, 1, 2, 3, 4, 5]);
  });
});
