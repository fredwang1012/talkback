"""Cast pass: one LLM call reads the book (a sample when it is long) and proposes the main characters with voices.

Shared by preprocess/cast.py (offline) and the upload pipeline. `first_para` is computed from the text (alias search)
because the spoiler rule depends on it being exact; the model's own guess is only a fallback.
"""
import re
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Book, Character, Narrator

VOICES = ("chloe", "eleanor", "jake", "marcus", "nora", "oliver")
VOICE_TAGS = ("", "<|prosody:pitch_low|>", "<|prosody:pitch_high|>", "<|prosody:speed_slow|>", "<|prosody:speed_fast|>")
Voice = Literal["chloe", "eleanor", "jake", "marcus", "nora", "oliver"]
VoiceTag = Literal["", "<|prosody:pitch_low|>", "<|prosody:pitch_high|>", "<|prosody:speed_slow|>", "<|prosody:speed_fast|>"]
MAIN_MAX = 8
CAST_MAX_CHARS = 300_000  # ~85K tokens: stays in the cheap (<100K token) price tier; longer books are sampled


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

UPLOAD_SYSTEM = SYSTEM.replace(
    "(at most 8).", f"(at most {MAIN_MAX}), listed by importance: speaking lines first, then plot role. "
    "Minor speakers are not characters; the narrator reads them.")


def numbered(book: Book) -> str:
    return "\n\n".join(f"[{p.idx}] {p.text}" for p in book.paragraphs)


def numbered_sample(book: Book, max_chars: int = CAST_MAX_CHARS, block: int = 12) -> str:
    """Numbered paragraphs; a long book is cut to evenly spread blocks of `block` paragraphs that fit `max_chars`."""
    full = numbered(book)
    if len(full) <= max_chars:
        return full
    paras = book.paragraphs
    blocks = [paras[i:i + block] for i in range(0, len(paras), block)]
    keep_n = max(1, int(len(blocks) * max_chars / len(full)))
    step = len(blocks) / keep_n
    chosen = [blocks[int(i * step)] for i in range(keep_n)]
    parts, size = [], 0
    for b in chosen:
        text = "\n\n".join(f"[{p.idx}] {p.text}" for p in b)
        if size + len(text) > max_chars and parts:
            break
        parts.append(text)
        size += len(text) + 2
    return "\n\n[...]\n\n".join(parts)


def locate_first_para(book: Book, aliases: list[str], fallback: int) -> int:
    pats = [re.compile(rf"\b{re.escape(a)}\b") for a in aliases if len(a) > 2]
    for p in book.paragraphs:
        if any(pat.search(p.text) for pat in pats):
            return p.idx
    return max(0, min(fallback, len(book.paragraphs) - 1))


def apply_cast(book: Book, result: CastResult, max_main: int | None = None, mark_main: bool = False) -> None:
    """Write characters and narrator into `book`; only the first `max_main` characters are kept (importance order)."""
    chars: list[Character] = []
    seen: set[str] = set()
    for c in result.characters:
        if c.id in seen or (max_main is not None and len(chars) >= max_main):
            continue
        seen.add(c.id)
        aliases = sorted({c.name, *c.aliases}, key=len, reverse=True)
        chars.append(Character(id=c.id, name=c.name, aliases=aliases,
                               first_para=locate_first_para(book, aliases, c.first_para),
                               voice=c.voice, voice_tags=c.voice_tags, persona=c.persona,
                               main=True if mark_main else None))
    chars.sort(key=lambda c: c.first_para)
    is_char = result.narrator_is_character if result.narrator_is_character in seen else None
    narrator_voice = next((c.voice for c in chars if c.id == is_char), result.narrator_voice)
    book.characters = chars
    book.narrator = Narrator(voice=narrator_voice, is_character=is_char)


async def cast_novel(book: Book, call: Callable[..., Awaitable[CastResult]], tier: str = "smart") -> None:
    """One call, then apply: at most MAIN_MAX main characters, all marked main."""
    user = f'Title: "{book.title}" by {book.author}\n\n{numbered_sample(book)}'
    result = await call(tier, UPLOAD_SYSTEM, user, CastResult, max_tokens=6000, effort="medium")
    apply_cast(book, result, MAIN_MAX, mark_main=True)
