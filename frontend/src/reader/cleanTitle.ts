const TRAILING_PAGE = /^((?:(?:chapter|part|book|section|unit)\s+\w+[:.]?|\d+(?:\.\d+)*\.?)\s+.*?[\p{L}]\S*)\s+\d{1,4}(?:\s+\W+)?$/iu;

/** A chapter title without stray glyphs (bullets, private-use / math symbols) or a trailing page number. */
export function cleanTitle(title: string): string {
  const t = title.replace(/[\p{So}\p{Sm}\p{Sk}\p{Co}\p{Cf}\p{Cc}\u2022\u2023\u25e6\u00b7\u2219\u2043\ufffd]/gu, "")
    .replace(/\s+/g, " ").trim();
  const m = TRAILING_PAGE.exec(t);
  return m ? m[1] : t;
}
