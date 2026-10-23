/** Issue #106: the Talkback Voice and Avatar picks belong to the "Talk to" character, per book, kept across reloads. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { choiceFor, dropTalkReferences, loadTalkChoices, saveTalkChoices, withTalkChoice } from "../src/reader/castLogic";

let store: Map<string, string>;
beforeEach(() => {
  store = new Map();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v); },
    removeItem: (k: string) => { store.delete(k); },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("per-character Talk choices (#106)", () => {
  it("a pick sticks to the character it was made for; others stay on their own default (Auto)", () => {
    let cs = loadTalkChoices("hp1");
    cs = withTalkChoice(cs, "narrator", { voice: "oliver", face: "f-1" });
    expect(choiceFor(cs, "narrator")).toEqual({ voice: "oliver", face: "f-1" });
    expect(choiceFor(cs, "hermione")).toEqual({ voice: null, face: null });
    cs = withTalkChoice(cs, "hermione", { voice: "chloe", face: null });
    expect(choiceFor(cs, "hermione")).toEqual({ voice: "chloe", face: null });
    expect(choiceFor(cs, "narrator")).toEqual({ voice: "oliver", face: "f-1" });
  });

  it("persists per book across reloads", () => {
    saveTalkChoices("hp1", withTalkChoice({}, "hermione", { voice: "chloe", face: "none" }));
    expect(choiceFor(loadTalkChoices("hp1"), "hermione")).toEqual({ voice: "chloe", face: "none" });
    expect(choiceFor(loadTalkChoices("alice"), "hermione")).toEqual({ voice: null, face: null });
  });

  it("setting a character back to Auto forgets it", () => {
    const cs = withTalkChoice(withTalkChoice({}, "harry", { voice: "jake", face: null }), "harry", { voice: null, face: null });
    expect(cs).toEqual({});
  });

  it("the old single choice (one for every character) becomes the narrator's", () => {
    store.set("talkback:talk:hp1", JSON.stringify({ voice: "nora", face: "f-2" }));
    const cs = loadTalkChoices("hp1");
    expect(choiceFor(cs, "narrator")).toEqual({ voice: "nora", face: "f-2" });
    expect(choiceFor(cs, "harry")).toEqual({ voice: null, face: null });
  });

  it("ignores junk in storage", () => {
    store.set("talkback:talk:hp1", JSON.stringify({ bySpeaker: { harry: { voice: 3, face: "f" }, ron: "x" } }));
    expect(loadTalkChoices("hp1")).toEqual({ harry: { voice: null, face: "f" } });
    store.set("talkback:talk:hp1", "{nope");
    expect(loadTalkChoices("hp1")).toEqual({});
  });

  it("a deleted voice or face falls back to Auto for every character that used it", () => {
    const cs = { narrator: { voice: "vc_me", face: "f-1" }, harry: { voice: "vc_me", face: null } };
    expect(dropTalkReferences(cs, { voice: "vc_me" })).toEqual({ narrator: { voice: null, face: "f-1" } });
  });
});
