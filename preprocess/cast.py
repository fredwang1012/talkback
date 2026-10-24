"""cast (Sonnet 5.5): read the whole book, propose characters, personas and voice assignments -> book.json.

Usage: uv run --project backend python preprocess/cast.py --book <id> [--yes]
       uv run --project backend python preprocess/cast.py --series harry-potter [--yes]
Writes `characters` and `narrator` into data/books/<id>/book.json. Review/edit the casting by hand afterwards.
--series casts every book of a series with ONE call that sees only speech-tag statistics and each speaker's earliest
lines (a whole series does not fit one prompt, and earliest lines keep personas free of later revelations).
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
    id: str = Field(description="lowercase slug, e.g. 'hagrid'")
    name: str
    aliases: list[str] = Field(description="every way the text refers to this character, e.g. 'Hagrid', 'Rubeus Hagrid'")
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


TUTOR_VOICE = "marcus"  # enthusiastic, professorial: the brief's choice for textbook narration


def cast_textbook(book: Book) -> None:
    """Textbooks have no characters: one narrator/tutor voice, no LLM call (the tutor persona lives in personas.py)."""
    book.characters = []
    book.narrator = Narrator(voice=TUTOR_VOICE, is_character=None)


def numbered(book: Book) -> str:
    return "\n\n".join(f"[{p.idx}] {p.text}" for p in book.paragraphs)


def locate_first_para(book: Book, aliases: list[str], fallback: int) -> int:
    pats = [re.compile(rf"\b{re.escape(a)}\b") for a in aliases if len(a) > 2]
    for p in book.paragraphs:
        if any(pat.search(p.text) for pat in pats):
            return p.idx
    return max(0, min(fallback, len(book.paragraphs) - 1))


# ---------- series casting: one cast for every book, from speech-tag statistics and earliest lines only ----------

SERIES_MAX = 24
_TAG_VERBS = r"(?:said|asked|shouted|whispered|snapped|called|muttered|cried|yelled|added|replied|roared|bellowed|hissed|growled|sneered|squeaked|gasped|began)"
_W = r"[A-Z][A-Za-z'-]*[a-z]"  # a capitalised word: "Hagrid", "McGonagall", "O'Brien"
_NAME = rf"((?:Professor|Mr\.|Mrs\.|Madam|Uncle|Aunt|Mad-Eye|Dr\.) {_W}|{_W}(?: {_W})?)"
_SAID_X = re.compile(rf"\b{_TAG_VERBS} {_NAME}")
_X_SAID = re.compile(rf"{_NAME} {_TAG_VERBS}\b")
_NOT_NAMES = {"He", "She", "They", "It", "I", "We", "You", "Then", "There", "But", "And", "The", "A", "This", "That",
              "Everyone", "Nobody", "Somebody", "Someone", "Something", "Nothing", "What", "Well", "Yes", "No", "Oh"}

SERIES_SYSTEM = f"""You are casting a full-cast audiobook of a book series with a text-to-speech system.
You get the most frequent speakers (from speech tags such as "said X") with counts, where they first speak, and their EARLIEST lines.

Rules:
- Pick at most {SERIES_MAX} characters who speak often or matter most. Merge different names for the same person into one character (put the other names in aliases), and drop names that are not people who speak.
- id: a short lowercase slug of the name most used for them, e.g. "harry", "mcgonagall".
- aliases: every name the text uses for them, as written (e.g. "Professor McGonagall", "McGonagall"). Never use a bare common word, or a name shared with another character, as an alias.
- Available voices: chloe (female, friendly, clear), eleanor (female, calm, professional), jake (male, energetic, dramatic), marcus (male, enthusiastic, professorial), nora (female, calm, narrative), oliver (male, calm, thoughtful, reflective). Give each character the voice that fits their gender, age and temperament; share voices where needed and tell sharers apart with voice_tags (pitch_low, pitch_high, speed_slow, speed_fast), "" when not needed.
- Personas: at most 3 sentences, written to be spoken by a roleplay actor: temperament, how they talk, verbal tics. Describe them ONLY as they come across in their earliest lines shown here. Never mention later revelations, true allegiances, deaths, or anything from later in the series, even if you know it.
- first_para: use 0 (it is recomputed from the text).
- The story is told by a third-person narrator: set narrator_is_character to null and pick a calm narrator_voice."""


def speaker_stats(books: list[Book], max_names: int = 60, samples: int = 3) -> list[dict]:
    """Most frequent speech-tag names across the series, each with where it first speaks and its earliest lines."""
    stats: dict[str, dict] = {}
    for b in books:
        for p in b.paragraphs:
            if '"' not in p.text:
                continue
            for m in (*_SAID_X.finditer(p.text), *_X_SAID.finditer(p.text)):
                name = m.group(1)
                if name.split()[0] in _NOT_NAMES:
                    continue
                s = stats.setdefault(name, {"name": name, "count": 0, "first": (b.series_index or 0, p.idx, b.title),
                                            "lines": []})
                s["count"] += 1
                if len(s["lines"]) < samples and p.text[:300] not in s["lines"]:
                    s["lines"].append(p.text[:300])
    return sorted(stats.values(), key=lambda s: -s["count"])[:max_names]


def series_user(series_title: str, stats: list[dict]) -> str:
    rows = []
    for s in stats:
        lines = "\n".join(f"    - {t}" for t in s["lines"])
        rows.append(f'- {s["name"]}: {s["count"]} speech tags, first speaks in "{s["first"][2]}" ¶{s["first"][1]}\n{lines}')
    return f"Series: {series_title}\n\nSpeakers:\n" + "\n".join(rows)


def apply_series_cast(books: list[Book], result: CastResult) -> None:
    """Give every book the characters who have appeared by its end; first_para is 0 when met in an earlier book."""
    seen: set[str] = set()
    cast = []
    for c in result.characters:
        if c.id not in seen:
            seen.add(c.id)
            cast.append((c, sorted({c.name, *c.aliases}, key=len, reverse=True)))
    met_before: set[str] = set()
    for book in sorted(books, key=lambda b: b.series_index or 0):
        chars = []
        for c, aliases in cast:
            if c.id in met_before:
                first = 0
            else:
                first = locate_first_para(book, aliases, -1) if _appears(book, aliases) else None
                if first is None:
                    continue  # not met yet in the series: never offered, never named
            chars.append(Character(id=c.id, name=c.name, aliases=aliases, first_para=first,
                                   voice=c.voice, voice_tags=c.voice_tags, persona=c.persona))
        chars.sort(key=lambda ch: ch.first_para)
        book.characters = chars
        book.narrator = Narrator(voice=book.narrator.voice if book.narrator else result.narrator_voice, is_character=None)
        met_before |= {ch.id for ch in chars}


def _appears(book: Book, aliases: list[str]) -> bool:
    pats = [re.compile(rf"\b{re.escape(a)}\b") for a in aliases if len(a) > 2]
    return any(pat.search(p.text) for p in book.paragraphs for pat in pats)


async def cast_series(series: str, yes: bool) -> None:
    paths = sorted((llm.ROOT / "data/books").glob("*/book.json"))
    books = [b for b in (Book.model_validate_json(p.read_text(encoding="utf-8")) for p in paths) if b.series == series]
    if not books:
        sys.exit(f"no books with series={series!r}")
    books.sort(key=lambda b: b.series_index or 0)
    user = series_user(series, speaker_stats(books))
    est = llm.estimate_cost(MODEL, len(SERIES_SYSTEM) + len(user), 6000)
    print(f"cast (series {series}, {len(books)} books): {MODEL}, ~{(len(SERIES_SYSTEM) + len(user)) // 4} input tokens, estimated ${est:.3f}")
    if est > 5 and not yes:
        sys.exit("estimate > $5; rerun with --yes")
    result = await llm.call_structured(MODEL, SERIES_SYSTEM, user, CastResult, max_tokens=12000, effort="medium")
    apply_series_cast(books, result)
    for b in books:
        (llm.ROOT / "data/books" / b.id / "book.json").write_text(b.model_dump_json(indent=1), encoding="utf-8")
        print(f"{b.id}: {len(b.characters)} characters, new this book: "
              f"{[c.id for c in b.characters if c.first_para > 0]}")
    for c in result.characters:
        print(f"- {c.name} (id={c.id}) voice={c.voice} {c.voice_tags}\n    aliases: {c.aliases}\n    {c.persona}")
    llm.print_usage()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book")
    ap.add_argument("--series", help="cast every book of a series at once (e.g. harry-potter)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.series:
        await cast_series(args.series, args.yes)
        return
    if not args.book:
        sys.exit("give --book or --series")
    path = llm.ROOT / "data/books" / args.book / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    if book.kind == "textbook":
        cast_textbook(book)
        path.write_text(book.model_dump_json(indent=1), encoding="utf-8")
        print(f"Textbook: no characters; narrator/tutor voice={TUTOR_VOICE}. No LLM call made.")
        return

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
