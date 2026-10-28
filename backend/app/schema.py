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


class MathBlock(BaseModel):
    """A display equation of an uploaded PDF textbook: where it sits on the page, its picture, and (when Gemini read
    the picture) its LaTeX and how to say it. The paragraph's `text` keeps the extracted glyphs as the last fallback."""
    id: str                     # "e12": names the cropped picture
    page: int                   # 0-based PDF page
    bbox: list[float]           # x0, y0, x1, y1 in PDF points
    number: str | None = None   # "3.4" when the equation ends in "(3.4)"
    garbled: bool = False       # the extracted text is no faithful reading (stacked fractions, big operators, accents)
    image: str | None = None    # URL of the cropped picture
    latex: str | None = None
    spoken: str | None = None


class InlineMath(BaseModel):
    """A formula inside a prose paragraph whose extracted text is garbled: text[start:end] shows that garble."""
    id: str
    start: int
    end: int
    page: int
    bbox: list[float]
    image: str | None = None
    latex: str | None = None
    spoken: str | None = None


class Paragraph(BaseModel):
    idx: int
    section_id: str
    text: str
    segments: list[Segment] = []
    tts_chunks: list[TtsChunk] = []
    # "code": a program / pseudocode listing, shown but not read aloud; "math": a display equation (see `math`);
    # "figure": a textbook figure (#40), `text` is its caption (read aloud) and `figure` the image file name
    kind: Literal["code", "math", "figure"] | None = None
    math: MathBlock | None = None
    inline_math: list[InlineMath] = []
    figure: str | None = None  # under data/books/<id>/figures/, served by /api/books/<id>/figures/<name>


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
    # Textbooks: the section (chapter or numbered subsection) this note summarises; upto_para is that section's last
    # paragraph. Novel "story so far" notes leave it unset.
    section_id: str | None = None


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
    # ISO 639-3 code of the book's language (#117, app/booklang.py): detected at upload. Older books are English.
    language: str = "eng"
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
    # series slugs the library reconciliation (enrich/series_link.py) already asked about for this book: once each
    series_checked: list[str] = []
    # ttschunks.CHUNK_RULES_VERSION the tts_chunks were built with (0 = before the version existed). The server
    # re-chunks an older book once after loading it (books.load_book), keeping segment styles.
    chunk_rules: int = 0

    @field_validator("language")
    @classmethod
    def _known_language(cls, v: str) -> str:  # a code Boson does not know would fail every TTS request
        from .tts_lang import valid
        return v if valid(v) else "eng"
