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


# ---------- more sound effects (#73) ----------
# Each sfx goes right before onomatopoeia the book already has, at the start of a character's line (after the opening
# quote), like laughter. Sounds that are ambiguous on their own also need the adjacent speech tag to say so.

_SIGH_TAG = re.compile(r"\bsigh(?:ed|s|ing)\b", re.I)
_WEEP_TAG = re.compile(
    r"\b(?:sob(?:bed|s|bing)|wept|weep(?:s|ing)?|crying(?!\s+out)|in\s+tears|burst\s+into\s+tears"
    r"|cried\s+(?:bitterly|softly|quietly|piteously|harder|silently|herself|himself|themselves))\b", re.I)
_SCREAM_TAG = re.compile(r"\b(?:scream(?:ed|s|ing)?|shriek(?:ed|s|ing)?)\b", re.I)
_SIGH_WORD = re.compile(r"sigh|a+hh+|ah-h+|a+h+-h+|haa+h+")
_SOB_WORD = re.compile(r"sob|boo-?hoo(?:-?hoo)*")
_SCREAM_WORD = re.compile(r"a{2,}h+|a{3,}(?:r*gh)?")


def speech_tag(segments: Sequence, i: int) -> str:
    """The short speech tags next to character line segments[i] ('she sighed.' / 'Alice sobbed,'), joined; else ''."""
    out = []
    if i + 1 < len(segments) and segments[i + 1].speaker == "narrator":
        first = _SENT_END.split(_clean(segments[i + 1].text), maxsplit=1)[0]
        if len(first.split()) <= _MAX_TAG_WORDS:
            out.append(first)
    if i > 0 and segments[i - 1].speaker == "narrator":
        s = segments[i - 1].text.strip()
        if s and s[-1] in ",:—-":
            last = _SENT_END.split(_clean(s))[-1]
            if len(last.split()) <= _MAX_TAG_WORDS:
                out.append(last)
    return " | ".join(out)


def _says(rx: re.Pattern, tag: str) -> bool:
    return any(not _NEGATED.search(tag[:m.start()].replace("’", "'")) for m in rx.finditer(tag))


def sfx_at(spoken: str, tag: str = "") -> tuple[int, str] | None:
    """(offset, sfx name) when `spoken` starts with onomatopoeia for a Boson sound effect, else None.

    `tag` is the adjacent speech tag (see speech_tag). Strict: when in doubt, no sound."""
    lead = _LEAD.match(spoken)
    if not lead:
        return None
    rest = spoken[lead.end():]
    raw = rest.split()[:2]
    if not raw:
        return None
    first = re.sub(r"[^a-z-]", "", raw[0].lower()).strip("-")
    second = re.sub(r"[^a-z]", "", raw[1].lower()) if len(raw) > 1 else ""
    # punctuation right after the word: a sound, not a verb ('Sniff!' vs 'Sniff this rose')
    alone = bool(re.match(r"[A-Za-z-]+[!,.?…—*]", raw[0].lstrip("*"))) or len(raw) == 1
    at = lead.end()
    if _LAUGH.fullmatch(first.replace("-", "")) or (first == "ho" and second == "ho"):
        return at, "laughter"
    if first == "ahem":
        return at, "cough"
    if first == "cough" and (alone or second == "cough"):
        return at, "cough"
    if first == "sniff" and (alone or second == "sniff"):
        return at, "sniff"
    if _SIGH_WORD.fullmatch(first) and _says(_SIGH_TAG, tag):
        return at, "sigh"
    if (_SOB_WORD.fullmatch(first) or (first == "boo" and second == "hoo")) and (alone or second == "hoo") \
            and _says(_WEEP_TAG, tag):
        return at, "crying"
    if _SCREAM_WORD.fullmatch(first) and re.match(r"[A-Za-z-]+!", raw[0]) and not _says(_SIGH_TAG, tag) \
            and (raw[0].rstrip("!\"'”’").isupper() or _says(_SCREAM_TAG, tag)):  # 'Aaah!' alone may be delight
        return at, "screaming"
    return None


def with_sfx(spoken: str, tag: str = "") -> str:
    found = sfx_at(spoken, tag)
    if found is None:
        return spoken
    at, name = found
    return spoken[:at] + f"<|sfx:{name}|>" + spoken[at:]


# ---------- expressiveness (#73) ----------

_LOUD_TAG = re.compile(r"\b(?:shout(?:ed|s|ing)?|scream(?:ed|s|ing)?)\b", re.I)


def detect_expressive(segments: Sequence, i: int) -> str | None:
    """'expressive_high' for a clearly climactic character line, 'expressive_low' for a whispered one, else None.

    Call after apply_styles (whispered = style 'whispering')."""
    seg = segments[i]
    if seg.speaker == "narrator":
        return None
    if seg.style == "whispering":
        return "expressive_low"
    ends_bang = seg.text.strip().rstrip("\"'”’)").endswith("!")
    if _shouted_by_case(seg.text) or (ends_bang and _says(_LOUD_TAG, speech_tag(segments, i))):
        return "expressive_high"
    return None


# ---------- pauses (#73) ----------

_BREAK_MARKS = frozenset("*#~•·◆◇§")
_BREAK_CHARS = _BREAK_MARKS | frozenset("=_-")
SCENE_BREAK_MAX = 40  # a real separator is short; longer text is never one (also keeps the check cheap)


def is_scene_break(text: str) -> bool:
    """A separator paragraph such as '* * *', '***', '#' or '~ ~ ~' (no letters or digits). Linear: no regex
    backtracking on uploaded text."""
    t = text.strip()
    if not t or len(t) > SCENE_BREAK_MAX:
        return False
    return all(c in _BREAK_CHARS or c.isspace() for c in t) and any(c in _BREAK_MARKS for c in t)
