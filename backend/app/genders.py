"""Inferred gender of each speaker of a novel, for the two-voice reading mode (issues #85, #86).

Free and offline. For a character, in order: a title or noun in its name ("Mrs. Weasley", "The Queen"), a first-name
list, the pronouns of the tags after its attributed lines ("..." she said, at least 3 and 85% one way), then the gender
of the preset voice it was cast with. None when nothing is known; cast.py then reads it in the narrator's voice.
"""
from .enrich import attribute as at
from .enrich import minor
from .schema import Book, Character

PRESET_GENDER = {"chloe": "female", "eleanor": "female", "nora": "female",
                 "jake": "male", "marcus": "male", "oliver": "male"}
_EXTRA_FEMALE = {"duchess", "niece", "countess", "empress", "goddess", "widow", "landlady"}
_EXTRA_MALE = {"duke", "nephew", "count", "emperor", "landlord", "mister"}
_CACHE_SIZE = 16
_cache: dict[tuple, dict[str, str | None]] = {}


def from_name(name: str) -> str | None:
    """Gender a character's name reveals: a title ("Mrs.", "Uncle"), a noun ("Queen", "barge woman"), a first name."""
    words = [w.strip(".,'’").lower().removesuffix("'s") for w in name.split()]
    words = [w for w in words if w]
    if not words:
        return None
    if words[0] in minor.FEMALE_TITLES:
        return "female"
    if words[0] in minor.MALE_TITLES:
        return "male"
    for w in words:
        if w in minor.FEMALE_NOUNS or w in _EXTRA_FEMALE:
            return "female"
        if w in minor.MALE_NOUNS or w in _EXTRA_MALE:
            return "male"
    for w in words:
        if w in minor.FEMALE_NAMES:
            return "female"
        if w in minor.MALE_NAMES:
            return "male"
    return None


def from_voice(c: Character) -> str | None:
    """The gender of the preset the casting step picked (it was asked to match the character's gender)."""
    return PRESET_GENDER.get(c.base_voice or "") or PRESET_GENDER.get(c.voice)


def character_genders(book: Book) -> dict[str, str | None]:
    """Character id -> "male" / "female" / None. Cached per loaded book (books.load_book reuses the object)."""
    key = (book.id, id(book), len(book.paragraphs), len(book.characters))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    pronouns = at.infer_genders(book)
    out: dict[str, str | None] = {}
    for c in book.characters:
        stand_in = next((g for g, v in minor.MINOR_VOICES.items() if v.id == c.id), None)
        out[c.id] = stand_in or from_name(c.name) or {"he": "male", "she": "female"}.get(pronouns.get(c.id, "")) \
            or from_voice(c)
    if len(_cache) >= _CACHE_SIZE:
        _cache.pop(next(iter(_cache)))
    _cache[key] = out
    return out
