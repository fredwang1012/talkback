import { afterEach, describe, expect, it, vi } from "vitest";
import { answerLanguage, clearLanguage, toIso6393, transcriptionHint } from "../src/talk/language";
import { speak } from "../src/api";

describe("clearLanguage (#135): only a confident guess steers transcription", () => {
  it("a script is always clear", () => {
    expect(clearLanguage("この手紙は誰が書いたのですか")?.code).toBe("ja");
  });
  it("Latin text needs at least two stopwords of one language, more than any other", () => {
    expect(clearLanguage("¿Quién escribió esta carta y por qué?")?.code).toBe("es");
    expect(clearLanguage("What do you make of this visitor?")?.code).toBe("en");
    expect(clearLanguage("Alice")).toBeNull();
    expect(clearLanguage("la carta")).toBeNull(); // one shared stopword: es, fr, it all claim it
  });
});

describe("transcriptionHint (#135)", () => {
  it("the newest clear question decides; English (or nothing clear) gives no hint", () => {
    expect(transcriptionHint([])).toBeNull();
    expect(transcriptionHint(["What do you make of this?"])).toBeNull();
    expect(transcriptionHint(["What is this?", "¿Quién escribió esta carta y por qué?"])).toBe("es");
    expect(transcriptionHint(["¿Quién escribió esta carta y por qué?", "Alice"])).toBe("es"); // unclear: the earlier one stands
    expect(transcriptionHint(["¿Quién escribió esta carta y por qué?", "Who wrote the letter and why is it here?"])).toBeNull();
  });
  it("looks at the last three questions only", () => {
    expect(transcriptionHint(["¿Quién escribió esta carta y por qué?", "Alice", "Bob", "Carol"])).toBeNull();
  });
});

describe("ISO 639-3 for /api/speak (#135)", () => {
  it("maps the detector's codes; English and unknown give nothing", () => {
    expect(toIso6393("es")).toBe("spa");
    expect(toIso6393("fr")).toBe("fra");
    expect(toIso6393("ja")).toBe("jpn");
    expect(toIso6393("zh")).toBe("zho");
    expect(toIso6393("en")).toBeNull();
    expect(toIso6393(null)).toBeNull();
    expect(toIso6393("xx")).toBeNull();
  });
  it("answerLanguage is the 639-3 code of the hint", () => {
    expect(answerLanguage(["¿Quién escribió esta carta y por qué?"])).toBe("spa");
    expect(answerLanguage(["Who wrote this and why?"])).toBeNull();
  });
});

describe("speak sends the language only when there is one (#135)", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("adds `language` to the body for a non-English answer", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ voice: "v", audio_url: "/x", timestamps: null, cached: false })));
    vi.stubGlobal("fetch", fetchMock);
    await speak("Hola.", "marcus", undefined, "spa");
    await speak("Hi.", "marcus", "joy", null);
    const body = (i: number) => JSON.parse(((fetchMock.mock.calls[i] as unknown as [string, RequestInit])[1]).body as string);
    expect(body(0)).toEqual({ text: "Hola.", voice: "marcus", language: "spa" });
    expect(body(1)).toEqual({ text: "Hi.", voice: "marcus", emotion: "joy" });
  });
});
