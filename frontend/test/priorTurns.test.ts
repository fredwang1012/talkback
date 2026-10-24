import { describe, expect, it } from "vitest";
import { addQuestion, appendSentence, finishAnswer, priorTurns, type ChatMessage } from "../src/talk/chatLog";

const ask = (m: ChatMessage[], q: string, who: string, a?: string) => {
  let n = addQuestion(m, q, who, "typed");
  if (a) n = finishAnswer(appendSentence(n, a));
  return n;
};

describe("priorTurns", () => {
  it("is per character: only their questions and replies, oldest first", () => {
    let m = ask([], "Q1", "harry", "A1");
    m = ask(m, "Q2", "hagrid", "A2");
    m = ask(m, "Q3", "harry", "A3");
    expect(priorTurns(m, "harry")).toEqual([
      { role: "user", text: "Q1" }, { role: "character", text: "A1" }, { role: "user", text: "Q3" }, { role: "character", text: "A3" },
    ]);
    expect(priorTurns(m, "narrator")).toEqual([]);
  });
  it("leaves out the turn being asked right now", () => {
    const m = ask(ask([], "Q1", "harry", "A1"), "Q2", "harry"); // Q2 asked, answer pending
    expect(priorTurns(m, "harry")).toEqual([{ role: "user", text: "Q1" }, { role: "character", text: "A1" }]);
  });
  it("keeps only the last few, with capped text", () => {
    let m: ChatMessage[] = [];
    for (let i = 0; i < 10; i++) m = ask(m, `Q${i}`, "harry", "x".repeat(900));
    const t = priorTurns(m, "harry");
    expect(t).toHaveLength(6);
    expect(t[0].text).toBe("Q7");
    expect(t[1].text.length).toBe(500);
  });
});
