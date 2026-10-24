import { describe, expect, it } from "vitest";
import { detectLanguage } from "../src/talk/language";

describe("detectLanguage", () => {
  it.each([
    ["你觉得这封信是谁写的？", "zh"],
    ["この手紙は誰が書いたのですか", "ja"],
    ["이 편지는 누가 썼나요", "ko"],
    ["¿Quién escribió esta carta y por qué?", "es"],
    ["Pourquoi le roi est-il venu ici ? Qui est cette femme ?", "fr"],
    ["Wer ist diese Frau und warum ist er hier?", "de"],
    ["Кто написал это письмо?", "ru"],
    ["What do you make of this visitor?", "en"],
  ])("%s -> %s", (text, code) => {
    expect(detectLanguage(text)?.code).toBe(code);
  });

  it("returns null for empty or single-character input", () => {
    expect(detectLanguage("")).toBeNull();
    expect(detectLanguage(" a ")).toBeNull();
  });

  it("defaults unknown Latin text to English only when it has some length", () => {
    expect(detectLanguage("Harry Pell Baker")?.code).toBe("en");
    expect(detectLanguage("xyz")).toBeNull();
  });
});
