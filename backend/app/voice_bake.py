"""Bake a character's voice: render a short neutral sample with the preset voice + prosody tags, register it as a
custom Boson voice, and use that voice id for the character in read mode AND Talkback.

Why: TTS tags (pitch/speed) work only in /v1/audio/speech. Realtime (Talkback) takes a voice id and cannot apply
tags, so "jake + pitch_high" sounded like plain jake there. A custom voice cloned from a tagged sample carries the
tone into both modes.

Cost: the sample is TTS ($0.015 per 1K characters); voice creation itself is unpriced. Never called implicitly:
scripts/bake_voices.py prints the estimate and needs --run.
"""
import base64
import hashlib
from dataclasses import dataclass
from typing import Callable

from . import voices
from .schema import Book, Character
from .ttschunks import chunk_paragraph

# ~10 s of calm, neutral speech; tags go in front (the same prefix the reader puts on this character's lines).
SAMPLE_TEXT = ("The morning was quiet, and the road ran on ahead of us, past the old stone wall and down toward "
               "the river. Nobody was in any hurry, so we stopped for a while and talked about nothing in particular.")
TTS_USD_PER_1K_CHARS = 0.015


def sample_text(char: Character) -> str:
    return (char.base_voice_tags if char.base_voice else char.voice_tags) + SAMPLE_TEXT


def preset_of(char: Character) -> tuple[str, str]:
    """(preset voice, tags) the character was cast with, whether or not it has been baked yet."""
    if char.base_voice:
        return char.base_voice, char.base_voice_tags or ""
    return char.voice, char.voice_tags


def voice_key(book_id: str, char_id: str, preset: str, tags: str) -> str:
    """Deterministic: the same book/character/preset/tags always map to the same name, so a re-run reuses the voice."""
    return hashlib.sha1(f"{book_id}|{char_id}|{preset}|{tags}".encode()).hexdigest()[:10]


def voice_name(book: Book, char: Character) -> str:
    preset, tags = preset_of(char)
    return f"tb-{book.id[:24]}-{char.id[:24]}-{voice_key(book.id, char.id, preset, tags)}"


def is_bakeable(char: Character) -> bool:
    """A main character still on a preset voice (not already baked, not a user-cloned voice)."""
    return char.main is not False and not char.base_voice and char.voice in voices.PRESET_VOICES


@dataclass
class Estimate:
    book_id: str
    characters: list[str]
    chars: int
    usd: float


def estimate(books: list[Book]) -> list[Estimate]:
    out = []
    for b in books:
        todo = [c for c in b.characters if is_bakeable(c)]
        n = sum(len(sample_text(c)) for c in todo)
        out.append(Estimate(b.id, [c.id for c in todo], n, round(n / 1000 * TTS_USD_PER_1K_CHARS, 4)))
    return out


def apply_voice(book: Book, char: Character, voice_id: str) -> None:
    """Make `voice_id` the character's voice (tags are now inside it), record the original, rebuild TTS chunks."""
    if not char.base_voice:
        char.base_voice, char.base_voice_tags = char.voice, char.voice_tags
    char.voice, char.voice_tags = voice_id, ""
    if book.narrator and book.narrator.is_character == char.id:
        book.narrator.voice = voice_id
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)


async def bake_character(client, book: Book, char: Character, *,
                         load: Callable[[], dict[str, str]] = voices.load_voices,
                         save: Callable[[str, str], None] = voices.save_voice) -> str:
    """Voice id for this character. Reuses a voice already registered under the deterministic name (free)."""
    name = voice_name(book, char)
    existing = load().get(name)
    if existing:
        return existing
    preset, _ = preset_of(char)
    audio = (await client.create_speech(sample_text(char), preset, response_format="wav")).content
    voice_id = await client.create_voice(base64.b64encode(audio).decode(), SAMPLE_TEXT, name)
    save(name, voice_id)
    return voice_id


async def bake_book(client, book: Book, only: set[str] | None = None, **kw) -> dict[str, str]:
    """Bake every bakeable main character (or those in `only`), mutating `book`. Returns character id -> voice id."""
    done: dict[str, str] = {}
    for c in book.characters:
        if (only is not None and c.id not in only) or not is_bakeable(c):
            continue
        voice_id = await bake_character(client, book, c, **kw)
        apply_voice(book, c, voice_id)
        done[c.id] = voice_id
    return done
