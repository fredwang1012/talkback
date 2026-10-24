"""cast (Sonnet 5.5): read the whole book, propose characters, personas and voice assignments -> book.json.

Usage: uv run --project backend python preprocess/cast.py --book scandal-in-bohemia [--yes]
Writes `characters` and `narrator` into data/books/<id>/book.json. Review/edit the casting by hand afterwards.
`first_para` is computed from the text (alias search) because the spoiler rule depends on it being exact;
the model's own guess is only a fallback.
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
from app.schema import Book, Character, Narrator  # noqa: E402

MODEL = "claude-sonnet-5-5"
VOICES = ("chloe", "eleanor", "jake", "marcus", "nora", "oliver")
VOICE_TAGS = ("", "<|prosody:pitch_low|>", "<|prosody:pitch_high|>", "<|prosody:speed_slow|>", "<|prosody:speed_fast|>")
Voice = Literal["chloe", "eleanor", "jake", "marcus", "nora", "oliver"]
VoiceTag = Literal["", "<|prosody:pitch_low|>", "<|prosody:pitch_high|>", "<|prosody:speed_slow|>", "<|prosody:speed_fast|>"]


class CastCharacter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="lowercase slug, e.g. 'holmes'")
    name: str
    aliases: list[str] = Field(description="every way the text refers to this character, e.g. 'Holmes', 'Mr. Sherlock Holmes'")
    first_para: int = Field(description="index of the first paragraph where this character appears (speaks or is mentioned)")
    gender: Literal["male", "female", "unknown"]
    persona: str = Field(description="at most 3 sentences: personality, speaking style, verbal tics")
    voice: Voice
    voice_tags: VoiceTag


class CastResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[CastCharacter]
    narrator_voice: Voice
    narrator_is_character: str | None = Field(description="character id if the narrator is a character in the story (first-person), else null")


SYSTEM = """You are casting an audiobook with a text-to-speech system. Read the story and propose a cast.

Rules:
- Include only characters who speak or matter to the plot (at most 8). Include a first-person narrator as a character too, and set narrator_is_character to that character's id.
- The narrator's voice must equal that character's voice.
- Available voices: chloe (female, friendly, clear), eleanor (female, calm, professional), jake (male, energetic, dramatic), marcus (male, enthusiastic, professorial), nora (female, calm, narrative), oliver (male, calm, thoughtful, reflective).
- Give each character the voice that fits their gender, age and temperament. Voices may be shared only when there are more characters than voices; then differentiate with voice_tags (pitch_low, pitch_high, speed_slow, speed_fast). Use "" when no tag is needed.
- Personas are at most 3 sentences, written to be spoken in first person by a roleplay actor: temperament, how they talk, verbal tics. Use only facts from the text. Do not reveal plot twists or the ending in any persona.
- Paragraphs are numbered [0], [1], ... Use those numbers for first_para."""


def numbered(book: Book) -> str:
    return "\n\n".join(f"[{p.idx}] {p.text}" for p in book.paragraphs)


def locate_first_para(book: Book, aliases: list[str], fallback: int) -> int:
    pats = [re.compile(rf"\b{re.escape(a)}\b") for a in aliases if len(a) > 2]
    for p in book.paragraphs:
        if any(pat.search(p.text) for pat in pats):
            return p.idx
    return max(0, min(fallback, len(book.paragraphs) - 1))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    path = llm.ROOT / "data/books" / args.book / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))

    user = f'Title: "{book.title}" by {book.author}\n\n{numbered(book)}'
    est = llm.estimate_cost(MODEL, len(SYSTEM) + len(user), 3000)
    print(f"cast: {MODEL}, ~{(len(SYSTEM) + len(user)) // 4} input tokens, estimated ${est:.3f}")
    if est > 5 and not args.yes:
        sys.exit("estimate > $5; rerun with --yes")

    result = await llm.call_structured(MODEL, SYSTEM, user, CastResult, max_tokens=6000, effort="medium")
    chars: list[Character] = []
    seen: set[str] = set()
    for c in result.characters:
        if c.id in seen:
            continue
        seen.add(c.id)
        aliases = sorted({c.name, *c.aliases}, key=len, reverse=True)
        chars.append(Character(id=c.id, name=c.name, aliases=aliases,
                               first_para=locate_first_para(book, aliases, c.first_para),
                               voice=c.voice, voice_tags=c.voice_tags, persona=c.persona))
    chars.sort(key=lambda c: c.first_para)
    is_char = result.narrator_is_character if result.narrator_is_character in seen else None
    narrator_voice = next((c.voice for c in chars if c.id == is_char), result.narrator_voice)
    book.characters = chars
    book.narrator = Narrator(voice=narrator_voice, is_character=is_char)
    path.write_text(book.model_dump_json(indent=1), encoding="utf-8")

    print(f"\nNarrator: voice={book.narrator.voice}, is_character={book.narrator.is_character}")
    for c in chars:
        print(f"- {c.name} (id={c.id}, first seen ¶{c.first_para}) voice={c.voice} {c.voice_tags or ''}\n    aliases: {c.aliases}\n    {c.persona}")
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
