"""Rule-based Boson delivery control for read-aloud: style tags (whispering/shouting) and laughter sound effects.

No LLM. Everything is conservative: when the text is ambiguous, no tag is added. The book text is never changed;
the laughter sfx tag is inserted in front of onomatopoeia the book already contains.
"""
import re
from typing import Literal, Sequence

Style = Literal["whispering", "shouting"]
STYLES = ("whispering", "shouting")

# Full Boson tag vocabulary (docs.boson.ai/models/higgs-tts/tags). Used to strip tag residue from timestamp words.
TAG_VALUES = {
    "style": ("singing", "shouting", "whispering"),
    "sfx": ("cough", "laughter", "crying", "screaming", "burping", "humming", "sigh", "sniff", "sneeze"),
    "emotion": ("elation", "amusement", "enthusiasm", "determination", "pride", "contentment", "affection", "relief",
                "contemplation", "confusion", "surprise", "awe", "longing", "arousal", "anger", "fear", "disgust",
                "bitterness", "sadness", "shame", "helplessness"),
    "prosody": ("speed_very_slow", "speed_slow", "speed_fast", "speed_very_fast", "pitch_low", "pitch_high", "pause",
                "long_pause", "expressive_high", "expressive_low"),
}
# Boson sometimes returns a tag as plain letters glued to the next word: 'emotioncontemplationprosodypitchlowTo'.
TAG_RESIDUE = re.compile("|".join(
    kind + "(?:" + "|".join(sorted((v.replace("_", "") for v in vals), key=len, reverse=True)) + ")"
    for kind, vals in TAG_VALUES.items()))


def strip_tag_residue(word: str) -> str:
    """Remove glued-on lowercase tag names (case-sensitive: spoken words that start a sentence are capitalised)."""
    return TAG_RESIDUE.sub("", re.sub(r"[|<>:_]", "", word))


# ---------- style ----------

_V = r"(?:ed|s|ing)?"
_WHISPER = re.compile(rf"\b(?:whisper{_V}|hiss(?:ed|es|ing)?|breathed|mutter{_V}|murmur{_V})\b", re.I)
_SHOUT = re.compile(
    rf"\b(?:shout{_V}|yell{_V}|bellow{_V}|roar{_V}|scream{_V}|thunder{_V}|bawl{_V})\b(?!\s+with\s+(?:laughter|delight|approval))",
    re.I)
_NEGATED = re.compile(r"\b(?:not|never|n't|didn't|wasn't|without)\s+(?:\w+\s+)?$", re.I)
_SENT_END = re.compile(r"[.!?]+[\"'”’)]*\s+")
_MAX_TAG_WORDS = 9  # a speech tag is short: 'Hermione hissed,' / 'said Ron in a low voice.'


def _verb_style(clause: str) -> Style | None:
    found = set()
    for style, rx in (("whispering", _WHISPER), ("shouting", _SHOUT)):
        for m in rx.finditer(clause):
            if not _NEGATED.search(clause[:m.start()].replace("’", "'")):
                found.add(style)
    return found.pop() if len(found) == 1 else None  # both or neither: ambiguous


def _clean(s: str) -> str:
    return s.strip().lstrip("\"'“”‘’,;:— ").strip()


def _tag_after(narr: str) -> Style | None:
    """Speech tag right after a quote: the first sentence of the following narration ('Ron whispered.')."""
    s = _clean(narr)
    first = _SENT_END.split(s, maxsplit=1)[0]
    if len(first.split()) > _MAX_TAG_WORDS:
        return None
    return _verb_style(first)


def _tag_before(narr: str) -> Style | None:
    """Speech tag introducing a quote: the last sentence of the preceding narration, ending in , or :"""
    s = narr.strip()
    if not s or s[-1] not in ",:—-":
        return None
    last = _SENT_END.split(_clean(s))[-1]
    if len(last.split()) > _MAX_TAG_WORDS:
        return None
    return _verb_style(last)


def _shouted_by_case(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return (len(letters) >= 4 and "!" in text
            and sum(c.isupper() for c in letters) / len(letters) >= 0.9)


def detect_style(segments: Sequence, i: int) -> Style | None:
    """Style for segments[i] (objects with .speaker and .text), or None. Only character dialogue gets a style."""
    seg = segments[i]
    if seg.speaker == "narrator":
        return None
    verb = None
    if i + 1 < len(segments) and segments[i + 1].speaker == "narrator":
        verb = _tag_after(segments[i + 1].text)
    if verb is None and i > 0 and segments[i - 1].speaker == "narrator":
        verb = _tag_before(segments[i - 1].text)
    if _shouted_by_case(seg.text):
        return None if verb == "whispering" else "shouting"
    return verb


def apply_styles(segments: Sequence, *, keep_existing: bool = True) -> None:
    """Set `.style` on character segments. A rule hit overrides; otherwise an existing (LLM) style is kept if asked."""
    for i, seg in enumerate(segments):
        found = detect_style(segments, i)
        if found:
            seg.style = found
        elif not keep_existing or seg.speaker == "narrator" or seg.style not in STYLES:
            seg.style = None


# ---------- sound effects ----------

_LAUGH = re.compile(r"ha(?:ha)*h?|hee(?:hee)*|he(?:he)+h?|heh|ho(?:ho)+")
_LEAD = re.compile(r"""^[\s"'“”‘’«»—-]*(?=[A-Za-z])""")


def laughter_at(spoken: str) -> int | None:
    """Offset where laughter onomatopoeia begins at the very start of `spoken` (after any opening quote), else None."""
    lead = _LEAD.match(spoken)
    if not lead:
        return None
    words = [re.sub(r"[^a-z]", "", w.lower()) for w in spoken[lead.end():].split()[:2]]
    if not words:
        return None
    if _LAUGH.fullmatch(words[0]) or (words[0] == "ho" and len(words) > 1 and words[1] == "ho"):
        return lead.end()
    return None


def with_laughter(spoken: str) -> str:
    at = laughter_at(spoken)
    return spoken if at is None else spoken[:at] + "<|sfx:laughter|>" + spoken[at:]
