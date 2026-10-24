import { describe, expect, it } from "vitest";
import { cleanTitle } from "./cleanTitle";

describe("cleanTitle", () => {
  it("drops page numbers and stray glyphs", () => {
    expect(cleanTitle("6.4 Variant tf-idf functions 129 $")).toBe("6.4 Variant tf-idf functions");
    expect(cleanTitle("Chapter 12: The Patronus 1989")).toBe("Chapter 12: The Patronus");
    expect(cleanTitle("\u25ee 1.2 A first take \u2022")).toBe("1.2 A first take");
    expect(cleanTitle("Chapter 1: 1984")).toBe("Chapter 1: 1984");
    expect(cleanTitle("1 Boolean retrieval")).toBe("1 Boolean retrieval");
  });
});
