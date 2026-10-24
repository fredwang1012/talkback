"""Pydantic schema for data/books/<book_id>/book.json (brief section 4)."""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Emotion = Literal[
    "elation", "amusement", "enthusiasm", "determination", "pride", "contentment",
    "affection", "relief", "contemplation", "confusion", "surprise", "awe", "longing",
    "anger", "fear", "disgust", "bitterness", "sadness", "shame", "helplessness",
]
EMOTIONS: tuple[str, ...] = Emotion.__args__  # type: ignore[attr-defined]


class ChatTurn(BaseModel):
    """One earlier turn of the Talkback chat, sent back so the character remembers it (history, never new facts)."""
    role: Literal["user", "character"]
    text: str = Field(max_length=600)


class Section(BaseModel):
    id: str
    title: str
    start_para: int
    level: int = 1  # nesting depth in the chapter list: 1 = chapter, 2 = 2.1, 3 = 2.1.1 (old book.json files: 1)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:  # old books keep stray glyphs / page numbers in book.json; never show them
        from .parsing import clean_title
        return clean_title(v)


class Segment(BaseModel):
    speaker: str  # character id or "narrator"
    text: str
    emotion: Emotion | None = None
    style: Literal["whispering", "shouting"] | None = None  # Boson style tag; rule-detected (app/delivery.py) or LLM


class TtsChunk(BaseModel):
    id: str
    speaker: str
    voice: str
    tts_text: str
    char_start: int
    char_end: int


class Paragraph(BaseModel):
    idx: int
    section_id: str
    text: str
    segments: list[Segment] = []
    tts_chunks: list[TtsChunk] = []
    kind: Literal["code"] | None = None  # "code": a program / pseudocode listing, shown but not read aloud


class Character(BaseModel):
    id: str
    name: str
    aliases: list[str] = []
    first_para: int
    voice: str
    voice_tags: str = ""
    base_voice: str | None = None  # preset this character was cast with, kept when `voice` becomes a baked custom voice
    base_voice_tags: str | None = None  # the prosody tags that were baked into that custom voice (see voice_bake.py)
    persona: str
    portrait: str | None = None
    main: bool | None = None  # None = main character; uploaded novels mark minor speakers False


class Narrator(BaseModel):
    id: str = "narrator"
    voice: str
    is_character: str | None = None
    portrait: str | None = None


class Summary(BaseModel):
    upto_para: int
    text: str


class KindDetected(BaseModel):
    kind: Literal["novel", "textbook"]
    confidence: float
    method: str


class Book(BaseModel):
    id: str
    title: str
    author: str
    kind: Literal["novel", "textbook"]
    kind_detected: KindDetected | None = None  # set for uploads whose kind was auto-detected
    license: str
    attribution: str | None = None
    sections: list[Section]
    paragraphs: list[Paragraph]
    characters: list[Character] = []
    narrator: Narrator | None = None
    summaries: list[Summary] = []
    # Books of one series (same `series`, ordered by `series_index`): a reader of book N already knows books 1..N-1.
    series: str | None = None
    series_index: int | None = None
