"""Per-book cast settings: who the main characters are and which voice/face narrator and characters use.

Stored in data/books/<id>/cast_settings.json. A null voice means "the book's own voice" (character.voice /
narrator.voice); a null face means no face. One shared setting per book (single-user app, gated by the access code).
"""
import json
import uuid

from pydantic import BaseModel

from . import faces, voices
from .config import settings
from .schema import Book


class CastError(ValueError):
    pass


class Assignment(BaseModel):
    voice: str | None = None
    face: str | None = None


class CastSettings(BaseModel):
    main: list[str] | None = None
    narrator: Assignment = Assignment()
    characters: dict[str, Assignment] = {}


def _path(book_id: str):
    return settings.data_dir / "books" / book_id / "cast_settings.json"


def _stored(book_id: str) -> CastSettings:
    p = _path(book_id)
    return CastSettings.model_validate_json(p.read_text(encoding="utf-8")) if p.exists() else CastSettings()


def _default_main(book: Book) -> list[str]:
    return [c.id for c in book.characters if c.main is not False]


def load(book: Book) -> dict:
    """Stored settings merged with defaults: every book character gets an entry; `main` falls back to the
    characters not marked main=False."""
    stored = _stored(book.id)
    ids = [c.id for c in book.characters]
    main = [i for i in (stored.main if stored.main is not None else _default_main(book)) if i in ids]
    return {"main": main, "narrator": stored.narrator.model_dump(),
            "characters": {i: stored.characters.get(i, Assignment()).model_dump() for i in ids}}


def _check(a: Assignment, who: str) -> None:
    if a.voice is not None and not voices.is_known_voice(a.voice):
        raise CastError(f"Unknown voice '{a.voice}' for {who}.")
    if a.face is not None and not faces.face_exists(a.face):
        raise CastError(f"Unknown face '{a.face}' for {who}.")


def save(book: Book, new: CastSettings) -> dict:
    ids = {c.id for c in book.characters}
    for i in [*(new.main or []), *new.characters]:
        if i not in ids:
            raise CastError(f"'{i}' is not a character of this book.")
    _check(new.narrator, "the narrator")
    for i, a in new.characters.items():
        _check(a, i)
    main = list(dict.fromkeys(new.main if new.main is not None else _default_main(book)))
    out = CastSettings(main=main, narrator=new.narrator, characters=new.characters)
    p = _path(book.id)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")  # unique: concurrent PUTs never share a temp file
    tmp.write_text(json.dumps(out.model_dump(), indent=1), encoding="utf-8")
    tmp.replace(p)
    return load(book)


def voice_for(book: Book, speaker: str) -> str | None:
    """The cast-chosen voice for a speaker (character id or "narrator"), or None to keep the book's voice."""
    stored = _stored(book.id)
    a = stored.narrator if speaker == "narrator" else stored.characters.get(speaker)
    return a.voice if a and a.voice and voices.is_known_voice(a.voice) else None
