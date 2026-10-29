import { describe, expect, it } from "vitest";
import { addressedSpeaker } from "../src/talk/route";

// the Alice sample's cast (committed sample book), as the Talkback choices list them
const cast = [
  { id: "narrator", name: "Narrator" },
  { id: "alice", name: "Alice" },
  { id: "hatter", name: "The Mad Hatter", aliases: ["Hatter"] },
  { id: "rabbit", name: "White Rabbit" },
  { id: "queen", name: "Queen of Hearts" },
  { id: "white-queen", name: "White Queen" },
];
const to = (q: string) => addressedSpeaker(q, cast);

describe("addressedSpeaker (#34): who a question asked while reading goes to", () => {
  it("a name at the start, as an address, picks that character", () => {
    expect(to("Alice, why did you follow the rabbit?")).toBe("alice");
    expect(to("alice: what was in the bottle?")).toBe("alice");
    expect(to("Hey Alice what happened next")).toBe("alice");
    expect(to("Hatter, why is a raven like a writing desk?")).toBe("hatter");
    expect(to("The Mad Hatter, are you mad?")).toBe("hatter");
    expect(to("Narrator, where are we now?")).toBe("narrator");
    expect(to("Alice?")).toBe("alice");
  });

  it("a name at the end after a comma, or after ask / talk to, picks that character", () => {
    expect(to("What do you think of the Duchess, Alice?")).toBe("alice");
    expect(to("Ask the Hatter why he is late")).toBe("hatter");
    expect(to("I want to talk to the Queen of Hearts")).toBe("queen");
    expect(to("Can I speak with Alice please")).toBe("alice");
  });

  it("a mere mention keeps whoever was asked (null)", () => {
    expect(to("What do you think of Alice?")).toBeNull();
    expect(to("Why was the Hatter angry?")).toBeNull();
    expect(to("Alice said what?")).toBeNull();
    expect(to("Who is the White Rabbit")).toBeNull();
    expect(to("what happened to alice")).toBeNull();
    // "ask" / "talk to" in the middle of a question is part of the story, not a cue
    expect(to("Why did the Queen ask Alice to play croquet?")).toBeNull();
    expect(to("Did the Duchess want to talk to the Hatter?")).toBeNull();
    expect(to("Could you ask Alice what she saw?")).toBe("alice"); // a request at the start still is
    expect(to("")).toBeNull();
  });

  it("only offered characters count: one not met yet (or a minor one) is never picked", () => {
    expect(addressedSpeaker("Alice, hello", [{ id: "narrator", name: "Narrator" }])).toBeNull();
    expect(addressedSpeaker("Alice, hello", [])).toBeNull();
  });

  it("a word two names share does not pick either; the full name does", () => {
    expect(to("White, what now?")).toBeNull(); // White Rabbit and White Queen
    expect(to("White Queen, what now?")).toBe("white-queen");
    expect(to("Queen, off with whose head?")).toBeNull(); // "queen" is in two names
    expect(to("Hearts, off with whose head?")).toBe("queen");
  });

  it("the earliest address wins; case, accents and other scripts work", () => {
    expect(to("Alice, what would the Hatter say, Rabbit?")).toBe("alice");
    expect(addressedSpeaker("Émile, où es-tu ?", [{ id: "emile", name: "Émile" }])).toBe("emile");
    expect(addressedSpeaker("ナルト、元気？", [{ id: "naruto", name: "ナルト" }])).toBe("naruto");
    expect(addressedSpeaker("MR DURSLEY, why?", [{ id: "vernon", name: "Mr Dursley" }])).toBe("vernon");
    expect(addressedSpeaker("Mr, why?", [{ id: "vernon", name: "Mr Dursley" }])).toBeNull(); // "Mr" alone names nobody
  });
});
