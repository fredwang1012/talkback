"""Per-book cast settings: who the main characters are and which voice/face narrator and characters use.

Stored per user (users.cast_json). A null voice means "the book's own voice" (character.voice / narrator.voice);
a null face means no face.

Novels read in one of two modes, both built on the same male and female voice (each with a face):
- "two" (the default, issues #85/#86): the whole book uses exactly two voices, a male and a female one, each with
  a face. Every speaker, main cast and stand-ins alike, gets the voice of its inferred gender (genders.py, or the
  user's override in `genders`); the narrator uses whichever of the two the user picks (`narrator_gender`, by
  default the gender of the book's own narrator voice). A speaker of unknown gender reads in the narrator's voice.
  Pitch/speed tags baked into the book are dropped, so the two voices stay two voices. Nothing in book.json changes:
  the mapping is applied when a chunk's voice is resolved, so it works for every book and is reversible.
- "one" (#93): every line, dialogue included, reads in the narrator's voice (the one `narrator_gender` picks).
The old "cast" mode (per-character voices) is gone: a stored or sent "cast" is read as "two". The `characters`
assignments stay in the file (and are validated) so an old cast loses nothing, but novels no longer read from them.
Textbooks have one voice: the `narrator` assignment.
"""
import json
import uuid
from typing import Literal

from pydantic import BaseModel, field_validator

from . import faces, genders, voices
from . import users
from .enrich.cast import VOICE_TAGS
from .schema import Book
from .ttschunks import DEFAULT_VOICE

Gender = Literal["male", "female"]
DEFAULT_SLOT_VOICES = {"male": "jake", "female": "chloe"}  # when the book's narrator voice is not of that gender


class CastError(ValueError):
    pass


class Assignment(BaseModel):
    voice: str | None = None
    tags: str | None = None  # prosody tags (pitch / speed) replacing the character's own; None = the book's
    face: str | None = None


class CastSettings(BaseModel):
    main: list[str] | None = None
    narrator: Assignment = Assignment()
    characters: dict[str, Assignment] = {}
    mode: Literal["two", "one"] = "two"
    male: Assignment = Assignment()      # the male voice + face of two-voice mode (tags are ignored)
    female: Assignment = Assignment()
    narrator_gender: Gender | None = None  # None = the gender of the book's narrator voice
    genders: dict[str, Gender] = {}        # the user's per-character overrides of the inferred gender

    @field_validator("mode", mode="before")
    @classmethod
    def _no_full_cast(cls, v):
        """Full cast was removed (#93): settings saved with it, or sent by an old client, read in two voices."""
        return "two" if v == "cast" else v


def _path(book_id: str):
    return users.cast_json(book_id)


def _stored(book_id: str) -> CastSettings:
    p = _path(book_id)
    return CastSettings.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else CastSettings()


def _default_main(book: Book) -> list[str]:
    return [c.id for c in book.characters if c.main is not False]


def load(book: Book) -> dict:
    """Stored settings merged with defaults: every book character gets an entry; `main` falls back to the
    characters not marked main=False. `auto_genders` and `defaults` are read-only (ignored when sent back)."""
    stored = _stored(book.id)
    ids = [c.id for c in book.characters]
    main = [i for i in (stored.main if stored.main is not None else _default_main(book)) if i in ids]
    return {"main": main, "narrator": _live(stored.narrator),
            "characters": {i: _live(stored.characters.get(i, Assignment())) for i in ids},
            "mode": stored.mode, "male": {**_live(stored.male), "tags": None},
            "female": {**_live(stored.female), "tags": None},
            "narrator_gender": stored.narrator_gender,
            "genders": {i: g for i, g in stored.genders.items() if i in ids},
            "auto_genders": genders.character_genders(book),
            "defaults": {"male": default_slot_voice(book, "male"), "female": default_slot_voice(book, "female"),
                         "narrator_gender": default_narrator_gender(book)}}


def default_narrator_gender(book: Book) -> str:
    """The gender of the book's own narrator voice; "male" when it is a custom voice of unknown gender."""
    return genders.PRESET_GENDER.get(book.narrator.voice if book.narrator else "", "male")


def default_slot_voice(book: Book, gender: str) -> str:
    """The narrator's own voice for its gender (narration keeps sounding as before), else a preset."""
    if gender == default_narrator_gender(book):
        return book.narrator.voice if book.narrator else DEFAULT_VOICE
    return DEFAULT_SLOT_VOICES[gender]


def two_voices(book: Book, stored: CastSettings) -> bool:
    """Novels read from the male / female voices (in "two" or "one" mode); textbooks from the narrator assignment."""
    return book.kind == "novel"


def gender_of(book: Book, stored: CastSettings, speaker: str) -> str:
    """The gender whose voice reads `speaker` (unknown: the narrator's; in one-voice mode everyone's the narrator's)."""
    narrator = stored.narrator_gender or default_narrator_gender(book)
    if speaker == "narrator" or stored.mode == "one":
        return narrator
    return stored.genders.get(speaker) or genders.character_genders(book).get(speaker) or narrator


def slot_voice(book: Book, stored: CastSettings, gender: str) -> str:
    a = stored.male if gender == "male" else stored.female
    return a.voice if a.voice and voices.is_known_voice(a.voice) else default_slot_voice(book, gender)


def _live(a: Assignment) -> dict:
    """A voice or face that was deleted since the cast was saved falls back to Auto (None)."""
    return {"voice": a.voice if a.voice and voices.is_known_voice(a.voice) else None,
            "tags": a.tags if a.tags in VOICE_TAGS else None,
            "face": a.face if a.face and faces.face_exists(a.face) else None}


def _check(a: Assignment, who: str) -> None:
    if a.voice is not None and not voices.is_known_voice(a.voice):
        raise CastError(f"Unknown voice '{a.voice}' for {who}.")
    if a.face is not None and not faces.face_exists(a.face):
        raise CastError(f"Unknown face '{a.face}' for {who}.")
    if a.tags is not None and a.tags not in VOICE_TAGS:
        raise CastError(f"Unknown pitch/speed tag for {who}.")


def save(book: Book, new: CastSettings) -> dict:
    ids = {c.id for c in book.characters}
    for i in [*(new.main or []), *new.characters]:
        if i not in ids:
            raise CastError(f"'{i}' is not a character of this book.")
    for i in new.genders:
        if i not in ids:
            raise CastError(f"'{i}' is not a character of this book.")
    _check(new.narrator, "the narrator")
    _check(new.male, "the male voice")
    _check(new.female, "the female voice")
    for i, a in new.characters.items():
        _check(a, i)
    main = list(dict.fromkeys(new.main if new.main is not None else _default_main(book)))
    out = CastSettings(main=main, narrator=new.narrator, characters=new.characters, mode=new.mode,
                       male=new.male.model_copy(update={"tags": None}),
                       female=new.female.model_copy(update={"tags": None}),
                       narrator_gender=new.narrator_gender, genders=new.genders)
    p = _path(book.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")  # unique: concurrent PUTs never share a temp file
    tmp.write_text(json.dumps(out.model_dump(), indent=1), encoding="utf-8")
    tmp.replace(p)
    return load(book)


def voice_for(book: Book, speaker: str) -> str | None:
    """The cast-chosen voice for a speaker (character id or "narrator"), or None to keep the book's voice."""
    stored = _stored(book.id)
    if two_voices(book, stored):
        return slot_voice(book, stored, gender_of(book, stored, speaker))
    a = stored.narrator if speaker == "narrator" else stored.characters.get(speaker)
    return a.voice if a and a.voice and voices.is_known_voice(a.voice) else None


def tags_for(book: Book, speaker: str) -> str | None:
    """The cast-chosen pitch/speed tag for a character ("" = none), or None to keep the book's own.

    Two-voice mode drops every character's tags: two voices, not two voices in a dozen pitches."""
    stored = _stored(book.id)
    if two_voices(book, stored):
        return ""
    a = stored.characters.get(speaker)
    return a.tags if a and a.tags in VOICE_TAGS else None
