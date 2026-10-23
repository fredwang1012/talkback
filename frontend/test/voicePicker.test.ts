// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Book, Cast, Face } from "../src/api";
import VoiceLab from "../src/reader/VoiceLab";
import { NovelVoices } from "../src/reader/VoicePicker";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const novel = {
  id: "b", kind: "novel", narrator: { id: "narrator", voice: "nora", is_character: null, portrait: null },
  characters: [{ id: "harry", name: "Harry", voice: "jake" }, { id: "mrsw", name: "Mrs. Weasley", voice: "nora" },
    { id: "minor_m", name: "Other speaker", voice: "oliver" }],
} as unknown as Book;
const textbook = { ...novel, kind: "textbook", characters: [] } as unknown as Book;
const none = { voice: null, face: null };
const cast = (): Cast => ({
  main: ["harry", "mrsw"], narrator: none, characters: { harry: none, mrsw: none }, mode: "two", male: none, female: none,
  narrator_gender: null, genders: {}, auto_genders: { harry: "male", mrsw: "female", minor_m: "male" },
  defaults: { male: "jake", female: "nora", narrator_gender: "female" },
});
const faces: Face[] = [{ id: "f1", name: "Ada", image: "/a.png", voice: null, talk: null, idle: null }];

let root: Root; let host: HTMLElement;
beforeEach(() => { host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

const group = (label: string) => host.querySelector<HTMLElement>(`[role="radiogroup"][aria-label="${label}"]`)!;
const radios = (label: string) => [...group(label).querySelectorAll<HTMLElement>('[role="radio"]')];
const checked = (label: string) => radios(label).find((r) => r.getAttribute("aria-checked") === "true")?.textContent;

function renderTwo(c: Cast = cast()) {
  const onCast = vi.fn(); const onPreview = vi.fn();
  act(() => root.render(createElement(NovelVoices, { book: novel, cast: c, onCast, cloned: { Me: "vc_me" }, faces, onPreview, audioAvailable: true })));
  return { onCast, onPreview };
}

describe("two-voice picker", () => {
  it("shows a male and a female card with voice cards, not dropdowns, and no preview plays by itself", () => {
    const { onPreview } = renderTwo();
    expect(host.querySelector("select")).toBeNull();
    expect(radios("Male voice: voice").map((r) => r.querySelector(".choice-title")?.textContent)).toEqual(["Jake", "Marcus", "Oliver", "Me"]);
    expect(radios("Female voice: voice").map((r) => r.querySelector(".choice-title")?.textContent)).toEqual(["Chloe", "Eleanor", "Nora", "Me"]);
    expect(checked("Male voice: voice")).toContain("Jake");
    expect(checked("Female voice: voice")).toContain("Nora");
    expect(checked("Narrator")).toBe("Female voice");
    expect(host.textContent).toContain("Energetic and dramatic"); // a one-line description per voice
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("one tab stop per group; arrow keys move and select, and focus follows", () => {
    const { onCast } = renderTwo();
    const male = radios("Male voice: voice");
    expect(male.map((r) => r.tabIndex)).toEqual([0, -1, -1, -1]);
    act(() => { male[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(onCast.mock.calls[0][0].male.voice).toBe("marcus");
    expect(document.activeElement).toBe(male[1]);
    act(() => { male[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })); });
    expect(onCast.mock.calls[1][0].male.voice).toBe("vc_me"); // wraps to the last option
  });

  it("clicking picks a voice, a face, the narrator and a character's gender; preview speaks only on click", () => {
    const { onCast, onPreview } = renderTwo();
    act(() => { radios("Female voice: voice")[0].click(); });
    expect(onCast.mock.lastCall![0].female).toEqual({ voice: "chloe", face: null });
    act(() => { radios("Male voice: face")[1].click(); });
    expect(onCast.mock.lastCall![0].male).toEqual({ voice: null, face: "f1" });
    act(() => { radios("Narrator")[0].click(); });
    expect(onCast.mock.lastCall![0].narrator_gender).toBe("male");
    act(() => { radios("Harry reads in")[2].click(); });
    expect(onCast.mock.lastCall![0].genders).toEqual({ harry: "female" });
    act(() => { host.querySelector<HTMLButtonElement>('[aria-label="Preview Oliver"]')!.click(); });
    expect(onPreview).toHaveBeenCalledWith("oliver");
  });

  it("lists characters with their inferred gender, without the generic stand-ins", () => {
    renderTwo({ ...cast(), genders: { mrsw: "male" } });
    expect(checked("Harry reads in")).toBe("Auto (male)");
    expect(checked("Mrs. Weasley reads in")).toBe("Male");
    expect(group("Other speaker reads in")).toBeNull();
  });
});

describe("voices panel", () => {
  const props = (book: Book, onCast = vi.fn()) => ({
    book, cast: cast(), onCast, castError: null, cloned: {}, onCloned: vi.fn(), animating: new Set<string>(), setAnimating: vi.fn(),
    animErrors: {}, setAnimErrors: vi.fn(), faces, onFaces: vi.fn(), onRemoved: vi.fn(), onFacesChanged: vi.fn(),
    onPreview: vi.fn(), audioAvailable: true, onClose: vi.fn(),
  });

  it("a textbook gets one voice card (voice + face) and no mode switch", () => {
    act(() => root.render(createElement(VoiceLab, props(textbook))));
    expect(host.textContent).not.toContain("Two voices");
    expect(host.textContent).not.toContain("One voice");
    expect(host.querySelectorAll(".voice-card")).toHaveLength(1);
    expect(radios("Narrator: face").length).toBe(2);
    expect(radios("Narrator: voice").length).toBe(6);
    expect(group("Male voice: voice")).toBeNull();
  });

  it("a novel has no Full cast and no mode select at the top: a One voice / Two voices toggle, Two voices by default (#93)", () => {
    const onCast = vi.fn();
    act(() => root.render(createElement(VoiceLab, props(novel, onCast))));
    expect(host.textContent).not.toContain("Full cast");
    expect(host.querySelector(".lab-cast, select")).toBeNull(); // no per-character voice / pitch / face rows
    expect(radios("How many voices").map((r) => r.textContent)).toEqual(["One voice", "Two voices"]);
    expect(checked("How many voices")).toBe("Two voices");
    act(() => { radios("How many voices")[0].click(); });
    expect(onCast.mock.lastCall![0].mode).toBe("one");
  });

  it("an old Full cast setting shows as Two voices", () => {
    act(() => root.render(createElement(VoiceLab, { ...props(novel), cast: { ...cast(), mode: "cast" } })));
    expect(checked("How many voices")).toBe("Two voices");
    expect(host.querySelector('.voice-card[aria-disabled="true"]')).toBeNull();
  });
});

describe("one voice mode (#93)", () => {
  it("greys out the voice that does not narrate, says why, and hides the gender list", () => {
    const { onCast } = renderTwo({ ...cast(), mode: "one" }); // the narrator is the female voice (book default)
    expect(checked("How many voices")).toBe("One voice");
    const male = host.querySelector<HTMLElement>('.voice-card[aria-label="Male voice"]')!;
    const female = host.querySelector<HTMLElement>('.voice-card[aria-label="Female voice"]')!;
    expect(male.getAttribute("aria-disabled")).toBe("true");
    expect(male.classList.contains("is-disabled")).toBe(true);
    expect(male.textContent).toContain("Not used: every line reads in the narrator's voice.");
    expect(female.getAttribute("aria-disabled")).toBeNull();
    expect(radios("Male voice: voice").every((r) => r.getAttribute("aria-disabled") === "true" && r.tabIndex === -1)).toBe(true);
    act(() => { radios("Male voice: voice")[1].click(); });
    expect(onCast).not.toHaveBeenCalled();
    expect([...male.querySelectorAll("button")].every((b) => b.disabled)).toBe(true);
    expect(group("Harry reads in")).toBeNull();
    // picking the male voice as narrator swaps which card is greyed out
    act(() => { radios("Narrator")[0].click(); });
    expect(onCast.mock.lastCall![0].narrator_gender).toBe("male");
    act(() => { radios("How many voices")[1].click(); });
    expect(onCast.mock.lastCall![0].mode).toBe("two");
  });
});
