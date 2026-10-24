"""Generic voices for speakers outside the cast (issue #70).

A quote whose tag names someone outside the cast ("said Mrs. Dursley", "squealed the gnome") used to be read by the
narrator. Instead it gets one of two stand-in voices, chosen by the speaker's inferred gender: a lower, male-ish one
and a higher, female-ish one. Change MINOR_VOICES below to retune them; nothing else needs to know.

Stand-ins are Character entries with main=False, so they never show in the cast or as people to talk to.
Gender comes from, in order: a pronoun right at the tag ("she said"), a title or noun in the tag ("Mrs.", "the old
woman"), then what the whole book reveals about that name (the pronouns that follow it elsewhere), then a short
first-name list; UNKNOWN_GENDER decides the rest. Free and offline: no model is called.
"""
import re
from dataclasses import dataclass

from ..schema import Book, Character
from ..ttschunks import rechunk_book
from . import attribute as at


@dataclass(frozen=True)
class MinorVoice:
    id: str
    name: str
    voice: str  # one of voices.PRESET_VOICES
    tags: str   # prosody tags (enrich.cast.VOICE_TAGS)


MINOR_VOICES = {
    "male": MinorVoice("minor_m", "Other speaker (lower voice)", "oliver", "<|prosody:pitch_low|>"),
    "female": MinorVoice("minor_f", "Other speaker (higher voice)", "chloe", "<|prosody:pitch_high|>"),
}
UNKNOWN_GENDER = "male"
MINOR_IDS = {v.id for v in MINOR_VOICES.values()}

_TITLE = r"(?:Mr|Mrs|Miss|Ms|Madam|Madame|Professor|Aunt|Uncle|Lord|Lady|Sir|Dr|Cousin|Auntie|Headmaster|Headmistress)"
_NAME = r"[A-Z][\w'-]+"
_SUBJ = rf"(?i:the|a|an)\s+(?:[\w'-]+\s+){{0,2}}[\w'-]+|(?:{_TITLE}\.?\s+)?{_NAME}(?:\s+{_NAME})?"
_AFTER_VERB = re.compile(rf"[,.\s]*{at.VERBS}\s+(?:\w+ly\s+)?({_SUBJ})")
_BEFORE_VERB = re.compile(rf"[,.\s]*({_SUBJ})\s+(?:\w+\s+){{0,2}}{at.VERBS}")
_LAST_SUBJ = re.compile(rf"({_SUBJ})\s+(?:\w+\s+)?{at.VERBS}\W*$|{at.VERBS}\s+(?:\w+ly\s+)?({_SUBJ})\W*$")

FEMALE_TITLES = {"mrs", "miss", "ms", "madam", "madame", "aunt", "auntie", "lady", "headmistress"}
MALE_TITLES = {"mr", "uncle", "sir", "lord", "headmaster"}
FEMALE_NOUNS = {"woman", "girl", "witch", "lady", "mother", "mum", "mom", "sister", "wife", "queen", "maid", "nurse",
                "daughter", "grandmother", "aunt", "waitress", "princess", "hag", "lass"}
MALE_NOUNS = {"man", "boy", "wizard", "father", "dad", "brother", "husband", "king", "gentleman", "son", "uncle",
              "goblin", "giant", "prince", "dwarf", "lad", "fellow", "soldier", "guard"}
FEMALE_NAMES = {"ginny", "luna", "molly", "petunia", "minerva", "pomona", "sybill", "trelawney", "narcissa", "bellatrix",
                "fleur", "cho", "lavender", "parvati", "padma", "tonks", "dolores", "umbridge", "rita", "myrtle",
                "sally", "mary", "anne", "emma", "alice", "lucy", "susan", "jane", "kate", "dora", "ella"}
MALE_NAMES = {"neville", "fred", "george", "percy", "arthur", "vernon", "dudley", "draco", "lucius", "sirius", "remus",
              "lupin", "cedric", "viktor", "krum", "gilderoy", "lockhart", "quirrell", "filch", "peeves", "ollivander",
              "bill", "charlie", "seamus", "dean", "oliver", "john", "tom", "james", "peter", "david", "ted", "marcus"}


def _gender_of_subject(subj: str) -> str | None:
    words = [w.strip(".,'").lower() for w in subj.split()]
    if not words:
        return None
    if words[0] in FEMALE_TITLES:
        return "female"
    if words[0] in MALE_TITLES:
        return "male"
    if words[0] in ("the", "a", "an"):
        noun = words[-1]
        return "female" if noun in FEMALE_NOUNS else "male" if noun in MALE_NOUNS else None
    for w in words:
        if w in FEMALE_NAMES:
            return "female"
        if w in MALE_NAMES:
            return "male"
    return None


def _subject(before: str, after: str) -> str | None:
    last = [s for s in at.SENT_END.split(before.strip()) if s][-1:] or [""]
    m = _LAST_SUBJ.search(last[0])
    if m:
        return m.group(1) or m.group(2)
    head = at.SENT_END.split(after.strip())[0][:160] if after.strip() else ""
    m = _AFTER_VERB.match(head) or _BEFORE_VERB.match(head)
    return m.group(1) if m else None


def _norm(subj: str) -> str:
    return re.sub(r"\s+", " ", subj.strip().lower().replace(".", ""))


def _pronoun_gender(before: str, after: str) -> str | None:
    p = at._pronoun_of(after, before)
    return {"he": "male", "she": "female"}.get(p or "")


def infer_gender(before: str, after: str, known: dict[str, str] | None = None) -> str:
    """"male" / "female" for the speaker of a quote with this narration before and after it."""
    g = _pronoun_gender(before, after)
    if g:
        return g
    subj = _subject(before, after)
    if subj:
        g = _gender_of_subject(subj) or (known or {}).get(_norm(subj))
        if g:
            return g
    return UNKNOWN_GENDER


def _evidence(book: Book) -> dict[str, str]:
    """Subject (normalised) -> gender: the majority of pronoun / title evidence seen for that subject in the book."""
    votes: dict[str, dict[str, int]] = {}

    def collect(before: str, quote: str, after: str) -> None:
        subj = _subject(before, after)
        g = _pronoun_gender(before, after) or (_gender_of_subject(subj) if subj else None)
        if subj and g:
            v = votes.setdefault(_norm(subj), {"male": 0, "female": 0})
            v[g] += 1
        return None

    at.attribute_book(book.model_copy(deep=True), apply=False, uncast_speaker=collect)
    return {k: max(v, key=v.get) for k, v in votes.items() if max(v.values()) > min(v.values())}


def ensure_stand_ins(book: Book, used: set[str]) -> None:
    """Add the stand-in characters that some line uses (an unused one would only clutter the book)."""
    have = {c.id for c in book.characters}
    for v in MINOR_VOICES.values():
        if v.id in used and v.id not in have:
            book.characters.append(Character(id=v.id, name=v.name, first_para=0, voice=v.voice, voice_tags=v.tags,
                                             persona="A minor speaker outside the cast.", main=False))


def assign_minor_voices(book: Book, apply: bool = True) -> list[at.Decision]:
    """Give every uncast speaker's line a generic voice by inferred gender; returns the lines that were (or would be)
    reassigned. With apply=True the book's segments, stand-in characters and TTS chunks are updated in place."""
    known = _evidence(book)
    stand_in = {g: v.id for g, v in MINOR_VOICES.items()}
    ds = at.attribute_book(book, apply=apply, uncast_speaker=lambda b, q, a: stand_in[infer_gender(b, a, known)])
    lines = [d for d in ds if d.rule == "minor"]
    if apply and lines:
        ensure_stand_ins(book, {d.speaker for d in lines})
        rechunk_book(book)  # stamps Book.chunk_rules
    return lines
