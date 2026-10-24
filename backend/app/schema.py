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


class Segment(BaseModel):
    speaker: str  # character id or "narrator"
    text: str
    emotion: Emotion | None = None


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


class Character(BaseModel):
    id: str
    name: str
    aliases: list[str] = []
    first_para: int
    voice: str
    voice_tags: str = ""
    persona: str
    portrait: str | None = None


class Narrator(BaseModel):
    id: str = "narrator"
    voice: str
    is_character: str | None = None
    portrait: str | None = None


class Summary(BaseModel):
    upto_para: int
    text: str


class Book(BaseModel):
    id: str
    title: str
    author: str
    kind: Literal["novel", "textbook"]
    license: str
    attribution: str | None = None
    sections: list[Section]
    paragraphs: list[Paragraph]
    characters: list[Character] = []
    narrator: Narrator | None = None
    summaries: list[Summary] = []
