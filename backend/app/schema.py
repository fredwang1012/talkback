"""Pydantic schema for data/books/<book_id>/book.json (brief section 4)."""
from typing import Literal

from pydantic import BaseModel

Emotion = Literal[
    "elation", "amusement", "enthusiasm", "determination", "pride", "contentment",
    "affection", "relief", "contemplation", "confusion", "surprise", "awe", "longing",
    "anger", "fear", "disgust", "bitterness", "sadness", "shame", "helplessness",
]
EMOTIONS: tuple[str, ...] = Emotion.__args__  # type: ignore[attr-defined]


class Section(BaseModel):
    id: str
    title: str
    start_para: int
    level: int = 1  # nesting depth in the chapter list: 1 = chapter, 2 = 2.1, 3 = 2.1.1 (old book.json files: 1)


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
