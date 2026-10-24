/**
 * Hands-free "stacking chats": every turn gets its own question and answer bubble, text goes only to the answer it
 * belongs to, and an interrupted answer is closed at once so nothing late lands in it.
 */
import { describe, expect, it } from "vitest";
import { addQuestion, appendSentenceTo, cutHeard, finishAnswer, markInterrupted, setTextOf } from "../src/talk/chatLog";

describe("chat binding by message id", () => {
  it("an empty question adds no bubbles", () => {
    expect(addQuestion([], "   ", "alice", "voice")).toEqual([]);
  });

  it("an interrupted answer is closed at once (late text cannot land in it); an empty one is removed", () => {
    let m = addQuestion([], "Who is Alice?", "alice", "voice");
    m = setTextOf(m, m[1].id, "A girl who");
    m = markInterrupted(m);
    expect(m[1]).toMatchObject({ text: "A girl who", interrupted: true, pending: false });
    expect(setTextOf(m, m[1].id, "A girl who fell down").at(1)?.text).toBe("A girl who");
    expect(appendSentenceTo(m, m[1].id, "More.").at(1)?.text).toBe("A girl who");
    let e = addQuestion([], "Who?", "alice", "voice");
    e = markInterrupted(e);
    expect(e.map((x) => x.role)).toEqual(["user"]); // no empty bubble left behind
  });

  it("text goes to its own answer only, never to an older or newer one", () => {
    let m = addQuestion([], "Q1", "alice", "voice");
    const a1 = m[1].id;
    m = setTextOf(m, a1, "Answer one.");
    m = finishAnswer(m);
    m = addQuestion(m, "Q2", "alice", "voice");
    const a2 = m[3].id;
    m = setTextOf(m, a1, "Late text of one."); // closed: ignored
    m = appendSentenceTo(m, a2, "Answer two.");
    m = appendSentenceTo(m, a2, "More.");
    expect(m.map((x) => x.text)).toEqual(["Q1", "Answer one.", "Q2", "Answer two. More."]);
  });

  it("cutHeard only trims the newest answer, never an older interrupted one", () => {
    let m = addQuestion([], "Q1", "alice", "voice");
    m = setTextOf(m, m[1].id, "Answer one is long.");
    m = markInterrupted(m);
    m = addQuestion(m, "Q2", "alice", "voice");
    m = setTextOf(m, m[3].id, "Answer two.");
    m = finishAnswer(m);
    expect(cutHeard(m, "Answer")[1].text).toBe("Answer one is long.");
    // a typed question over the answer: the new empty pending reply is skipped, the interrupted one is cut
    let t = addQuestion([], "Q1", "alice", "voice");
    t = setTextOf(t, t[1].id, "Answer one is long.");
    t = markInterrupted(t);
    t = addQuestion(t, "Q2", "alice", "typed");
    expect(cutHeard(t, "Answer one")[1].text).toBe("Answer one");
  });
});
