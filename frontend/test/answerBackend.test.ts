import { describe, expect, it } from "vitest";
import type { Health } from "../src/api";
import { answersViaGemini } from "../src/talk/useTalk";

const health = (answer_backend: Health["answer_backend"]): Health =>
  ({ ok: true, boson_key_set: true, access_required: false, answer_backend });

describe("talk answer path selection", () => {
  it("uses Gemini only when the server says gemini", () => {
    expect(answersViaGemini(health("gemini"))).toBe(true);
  });
  it("uses the Realtime path for realtime, unknown health, or an older server", () => {
    expect(answersViaGemini(health("realtime"))).toBe(false);
    expect(answersViaGemini(null)).toBe(false);
    expect(answersViaGemini({ ok: true, boson_key_set: true, access_required: false } as Health)).toBe(false);
  });
});
