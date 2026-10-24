"""Cast pass: one LLM call reads the book (a sample when it is long) and proposes the main characters with voices.

Shared by preprocess/cast.py (offline) and the upload pipeline. `first_para` is computed from the text (alias search)
because the spoiler rule depends on it being exact; the model's own guess is only a fallback.
"""
import re
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schema import Book, Character, Narrator
from .llm import SpendLimit
from .tasks import gather_or_cancel

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
    persona: str = Field(description="at most 4 sentences: concrete speaking style, self-reference, signature words and verbal tics")
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
- Personas are at most 4 sentences of direction to a roleplay actor: concrete speech habits (how they refer to themselves and others, signature words, dialect, quoted example phrases), not generic adjectives. Use only facts from the text. Do not reveal plot twists or the ending in any persona.
- Paragraphs are numbered [0], [1], ... Use those numbers for first_para."""

class MainCharacter(BaseModel):
    """A main character as identified by the upload cast call (no persona: personas are written separately)."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description="lowercase slug, e.g. 'hagrid'")
    name: str
    aliases: list[str] = Field(description="every way the text refers to this character")
    first_para: int = Field(description="index of the first paragraph where this character appears (speaks or is mentioned)")
    gender: Literal["male", "female", "unknown"]
    voice: Voice
    voice_tags: VoiceTag


class MainCast(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[MainCharacter]
    narrator_voice: Voice
    narrator_is_character: str | None = Field(description="character id if the narrator is a character in the story (first-person), else null")
    series: str | None = Field(default=None, description="name of the book series this is a volume of, e.g. 'Harry Potter', else null")
    series_index: int | None = Field(default=None, description="this book's number in that series in reading order (1 = first), else null")


SERIES_OUT_TOKENS = 30  # the two series fields of the cast answer (for estimates)
SERIES_MAX_INDEX = 99
_SERIES_EDGE = re.compile(r"^(?:the)\s+|\s+(?:series|saga|books|novels|trilogy|tetralogy|quartet|quintet|sequence|cycle|chronicles|chronicle|universe)$", re.I)


def series_slug(name: str | None) -> str | None:
    """'The Harry Potter series' -> 'harry-potter' (the Library groups by this, the built-in books use the same form)."""
    text = (name or "").strip()
    for _ in range(3):
        text = _SERIES_EDGE.sub("", text).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or None


def apply_series(book: Book, result: MainCast) -> None:
    """Series and volume number from the cast answer; a series already set (by hand or a re-run) is kept."""
    if book.series:
        return
    book.series = series_slug(result.series)
    idx = result.series_index
    book.series_index = idx if book.series and isinstance(idx, int) and 1 <= idx <= SERIES_MAX_INDEX else None


UPLOAD_SYSTEM = f"""You are casting an audiobook with a text-to-speech system. Read the story and name its main characters.

Rules:
- Include only characters who speak or matter to the plot (at most {MAIN_MAX}), listed by importance: speaking lines first, then plot role. Minor speakers are not characters; the narrator reads them. Include a first-person narrator as a character too, and set narrator_is_character to that character's id.
- The narrator's voice must equal that character's voice.
- Available voices: chloe (female, friendly, clear), eleanor (female, calm, professional), jake (male, energetic, dramatic), marcus (male, enthusiastic, professorial), nora (female, calm, narrative), oliver (male, calm, thoughtful, reflective).
- Give each character the voice that fits their gender, age and temperament. Voices may be shared only when there are more characters than voices; then differentiate with voice_tags (pitch_low, pitch_high, speed_slow, speed_fast). Use "" when no tag is needed.
- aliases: every name the text uses for them, as written. Never use a bare common word, or a name shared with another character.
- Paragraphs are numbered [0], [1], ... Use those numbers for first_para.
- series and series_index: give them when this book is a volume of a series, whether or not the text says so: the title, title page or text shows it, or it is a well-known volume of one. The first book of a well-known series counts too ("Dune" is volume 1 of the Dune series; "Harry Potter and the Philosopher's Stone" is volume 1 of Harry Potter). Name the series by its common short name ("Dune", "Harry Potter", not "The Dune Chronicles series") and give the volume number in reading order (1 = first). Otherwise null for both."""

PERSONA_GUIDE = """Make it specific to THIS character, never a generic type:
- Name concrete speech habits: how they refer to themselves and to others (even if odd, such as third person, or a title for everyone), signature words and sentence shape, dialect or non-standard grammar, and what their speech does when they are nervous, angry or excited.
- Quote one or two short phrases that appear in the text given, as models of the voice.
- Do not lean on bare adjectives such as friendly, wise, kind, nervous or mysterious: tie every trait to something audible. Keep canon speech habits as they are in the text.
- Write it as direction to the actor ("Speaks ...", "Drops ..."), not as "I speak"."""

PERSONA_PARAS = 40  # a persona is written from the paragraphs up to first_para + this many (earliest lines only)


class PersonaResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: str


PERSONA_SYSTEM = """You describe how one character comes across in their earliest lines of a story, for a roleplay actor who will speak as them.
Write at most 4 sentences about MANNER only: temperament, diction, how they address others, verbal tics. Describe them ONLY as they come across in the text given.
{guide}
Do not state plot facts, secrets, relationships, events or outcomes, and do not use outside knowledge of the book or anything that comes later.""".replace("{guide}", PERSONA_GUIDE)


def persona_cutoff(book: Book, first_para: int) -> int:
    return min(len(book.paragraphs) - 1, first_para + PERSONA_PARAS)


def persona_user(book: Book, name: str, aliases: list[str], first_para: int) -> str:
    """The ONLY text a persona sees: paragraphs from just before first_para up to first_para + PERSONA_PARAS."""
    end = persona_cutoff(book, first_para)
    text = "\n\n".join(book.paragraphs[i].text for i in range(max(0, first_para - 5), end + 1))
    return f'Character: {name} (also called: {", ".join(aliases)})\n\nText:\n\n{text}'


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


def apply_cast(book: Book, result: CastResult | MainCast, max_main: int | None = None, mark_main: bool = False) -> None:
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
                               voice=c.voice, voice_tags=c.voice_tags, persona=getattr(c, "persona", ""),
                               main=True if mark_main else None))
    chars.sort(key=lambda c: c.first_para)
    is_char = result.narrator_is_character if result.narrator_is_character in seen else None
    narrator_voice = next((c.voice for c in chars if c.id == is_char), result.narrator_voice)
    book.characters = chars
    book.narrator = Narrator(voice=narrator_voice, is_character=is_char)


async def cast_novel(book: Book, call: Callable[..., Awaitable], tier: str = "smart",
                     persona_tier: str = "fast") -> None:
    """Cast call (who, voices, first_para), then one cheap persona call per character from their earliest text only."""
    user = f'Title: "{book.title}" by {book.author}\n\n{numbered_sample(book)}'
    result = await call(tier, UPLOAD_SYSTEM, user, MainCast, max_tokens=8000, effort="medium")   # Flash-Lite is wordier
    apply_cast(book, result, MAIN_MAX, mark_main=True)
    apply_series(book, result)

    async def persona(c: Character) -> None:
        try:
            r = await call(persona_tier, PERSONA_SYSTEM, persona_user(book, c.name, c.aliases, c.first_para),
                           PersonaResult, max_tokens=600, effort="low")
            c.persona = r.persona.strip()
        except SpendLimit:
            raise
        except Exception:  # a plain fallback beats a failed upload
            c.persona = ""
        if not c.persona:
            c.persona = f"{c.name} speaks in their own way."

    await gather_or_cancel(persona(c) for c in book.characters)


_SPEECH = r"(?:said|asked|replied|cried|shouted|whispered|answered|exclaimed|called|added|muttered|laughed|growled)"
_NOT_NAMES = {"The", "And", "But", "Then", "She", "He", "They", "It", "There", "This", "That", "What", "When", "Who",
              "Why", "How", "Where", "Her", "His", "Their", "You", "We", "Yes", "No", "Oh", "Well", "Now", "One", "Said"}


def fallback_cast(book: Book) -> None:
    """No working LLM: take the names that most often sit next to a speech verb ("said Alice", "Alice replied").

    Deliberately simple: no gender, no real personas, voices handed out in turn. Better than an empty cast.
    """
    after = re.compile(rf"\b{_SPEECH}\s+(?:the\s+)?([A-Z][a-z]{{2,}})\b")
    before = re.compile(rf"\b([A-Z][a-z]{{2,}})\s+{_SPEECH}\b")
    counts: dict[str, int] = {}
    for p in book.paragraphs:
        for m in (*after.finditer(p.text), *before.finditer(p.text)):
            if m.group(1) not in _NOT_NAMES:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    names = [n for n, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c >= 2][:MAIN_MAX]
    result = MainCast(characters=[
        MainCharacter(id=re.sub(r"[^a-z]", "", n.lower()), name=n, aliases=[n], first_para=0, gender="unknown",
                      voice=VOICES[i % len(VOICES)], voice_tags="")
        for i, n in enumerate(names)], narrator_voice="nora", narrator_is_character=None)
    apply_cast(book, result, MAIN_MAX, mark_main=True)
    for c in book.characters:
        c.persona = f"{c.name} speaks in their own way."
