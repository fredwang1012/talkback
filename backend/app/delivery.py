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
_HUM_TAG = re.compile(r"\b(?:hum(?:med|s|ming)|humm?ing)\b", re.I)
_HUM_WORD = re.compile(r"(?:h?m{2,}|hm)(?:-h?m+)*")  # 'Hmm-hmm-hmm', 'Mmm': only when the speech tag says hummed
_SNEEZE_WORD = re.compile(r"a[h-]*t?-?choo|atishoo|ahtishoo|kerchoo")  # 'Achoo', 'Ah-choo', 'Atishoo'
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
    if _SNEEZE_WORD.fullmatch(first):
        return at, "sneeze"
    if _HUM_WORD.fullmatch(first) and _says(_HUM_TAG, tag):
        return at, "humming"
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


# ---------- singing (#74) ----------
# A song is verse the story says is SUNG: a lead-in ('...began to sing this:—', 'singing a sort of lullaby ... every
# line:', 'CHORUS.') followed by quoted or verse-like paragraphs. Verse that is repeated or recited is never sung, nor is
# talk about singing. A false 'singing' on prose sounds terrible, so every check errs towards no tag.

_SING = re.compile(
    r"(?<![-\w])(?:sing(?:s|ing)?|sang|sung|songs?|chorus|lullaby|hum(?:s|med|ming)|carol(?:s|led|ling)?"
    r"|croon(?:s|ed|ing)?)(?![-\w])(?!\s+out\b)", re.I)  # 'sang out' = called out; 'sing-song' is a tone of voice
_RECITE = re.compile(r"\b(?:repeat\w*|recit\w*|read(?:s|ing)?)\b", re.I)
_SING_NEG = re.compile(r"n't\b|\b(?:not|never|no|without)\b", re.I)
# ends on ':' / ':—', or on the verb; never on a bare dash ('...booing and singing — "— dodges Warrington' is commentary)
_LEAD_END = re.compile(r"(?::\s*[—–-]*|\b(?:sing|sang))\s*$", re.I)
_HEADING = re.compile(r"[\W_]*(?:chorus|refrain)[\W_]*", re.I)  # a 'CHORUS.' line before a quoted chorus
_OPEN_Q = "\"“'‘"
_VERSE_WORDS = frozenset(
    "and but or nor for yet so the a an how who what where when why while with without like to in on of at by from up "
    "down if as all that this these those his her its your my our their they he she you we it there then than though "
    "till until through because oh o no now will would can could shall should may might must let some each every upon "
    "into over under before after not".split())
_LINE_START = re.compile(r"(?<=[a-z,;:—–)])[ \t]+([A-Z][a-z']*)(?![\w'-])")
SONG_MAX_CHARS = 700  # a longer paragraph is not sung (each chunk is <= 280 chars anyway)
SONG_MAX_PARAS = 8  # a song is cut off after this many paragraphs
_LEAD_TAIL = 400  # the lead-in is the last sentence; only the end of a long paragraph is looked at


def is_sing_lead_in(text: str) -> bool:
    """Narration that introduces a song right after it: its last sentence mentions singing and ends on ':' or ':—'
    (or on 'sing' / 'sang'), without 'repeat' / 'recite' and without a negation before the singing word."""
    t = text.strip()
    if _HEADING.fullmatch(t):
        return True
    t = t[-_LEAD_TAIL:]
    clause = t if len(t) <= 80 else _SENT_END.split(t)[-1]
    if not _LEAD_END.search(clause) or _RECITE.search(clause):
        return False
    return any(not _SING_NEG.search(clause[:m.start()].replace("’", "'")) for m in _SING.finditer(clause))


def verse_score(text: str) -> int:
    """How many verse lines seem to start mid-sentence: a capitalised small word ('And', 'Where', 'How') right after a
    lowercase word or , ; : — (lines run together when verse is flattened into one paragraph). Names don't count, a
    capital after an opening quote is dialogue, and hits closer than 3 words count once."""
    hits, last = 0, -10
    for m in _LINE_START.finditer(text):
        if m.group(1).lower() not in _VERSE_WORDS:
            continue
        pos = text.count(" ", 0, m.start(1))
        if pos - last >= 3:
            hits += 1
        last = pos
    return hits


def _last_dq(text: str) -> tuple[int, bool] | None:
    """(index, is_opening) of the last double quote, or None."""
    k = max(text.rfind('"'), text.rfind("“"), text.rfind("”"))
    if k < 0:
        return None
    ch = text[k]
    opening = ch == "“" or (ch == '"' and (k == 0 or text[k - 1].isspace() or text[k - 1] in "([—–-"))
    return k, opening


def _quote_open(text: str) -> bool:
    """The paragraph ends inside a quotation (a multi-paragraph quote reopens on the next paragraph). Counted by parity,
    so quotes nested inside a song ('But the snail replied "Too far, too far!" and...') don't close it."""
    if "“" in text or "”" in text:
        return text.count("“") > text.count("”")
    return text.count('"') % 2 == 1


def _no_narration_after_quote(text: str) -> bool:
    """False when a quote closes and narration follows ('"La la," she said, and...')."""
    if _quote_open(text):
        return True
    last = _last_dq(text)
    return last is None or last[1] or not any(c.isalpha() for c in text[last[0] + 1:])


def _song_body(text: str, *, quoted: bool) -> bool:
    t = text.strip()
    return (0 < len(t) <= SONG_MAX_CHARS and _no_narration_after_quote(t)
            and (t[0] in _OPEN_Q if quoted else (t[0] in _OPEN_Q or verse_score(t) >= 2)))


def _inline_song(text: str) -> int | None:
    """Offset of a song that starts inside the paragraph after its lead-in ('the hat began to sing: "Oh, you...')."""
    for m in re.finditer(r":\s*[—–-]*\s*([\"“])", text[-(SONG_MAX_CHARS + _LEAD_TAIL):]):
        q = len(text) - len(text[-(SONG_MAX_CHARS + _LEAD_TAIL):]) + m.start(1)
        if text[q + 1:q + 2].isspace() or text.count('"', 0, q) % 2 or text.count("“", 0, q) > text.count("”", 0, q):
            continue  # a closing quote ('"Twinkle, twinkle—" and went on'), not one that opens the song
        if is_sing_lead_in(text[:q]) and _song_body(text[q:], quoted=True):
            return q
    return None


def _continues(prev: str, text: str) -> bool:
    """`text` goes on with the song sung in `prev`: the song's quote was left open (each paragraph of a multi-paragraph
    quotation reopens with a quote mark and only the last one closes), or `text` reads as verse."""
    t = text.strip()
    if not t or len(t) > SONG_MAX_CHARS:
        return False
    if t[0] in "\"“" and prev.rstrip()[-1:] not in "\"”'’":
        return True
    return verse_score(t) >= 2 and _no_narration_after_quote(t)


def sung_from(prev: Sequence[str], text: str) -> int | None:
    """Offset in `text` from which the paragraph is sung (0 = all of it), or None. `prev` are the paragraphs before it,
    oldest first (only the last SONG_MAX_PARAS + 1 matter; pass only those of the same chapter)."""
    window = list(prev[-(SONG_MAX_PARAS + 1):]) + [text]
    state: int | None = None  # sung offset of the previous paragraph
    run = 0  # paragraphs of the current song so far
    for k, t in enumerate(window):
        before = window[k - 1] if k else ""
        if state is None and k and is_sing_lead_in(before)                 and _song_body(t, quoted=not before.rstrip().endswith((":", "—", "–", "-"))):
            state, run = 0, 1
        elif state is not None and run < SONG_MAX_PARAS and _continues(before, t):
            state, run = 0, run + 1
        else:
            state = _inline_song(t)
            run = 1 if state is not None else 0
    return state


# ---------- natural reading speed (#78) ----------

SPEEDS = ("very_slow", "slow", "fast", "very_fast")  # Boson prosody:speed_* values (~0.65x, 0.85x, 1.2x, 1.4x)
_SPEED_TAG = re.compile(r"<\|prosody:speed_[a-z_]+\|>")


def apply_speed(tts_text: str, speed: str | None) -> str:
    """tts_text with a Boson speed tag first (a delivery token: at the start it covers the whole chunk). None: unchanged.

    A speed tag already in the text (the cast's voice tag) is replaced, since a second one would fight the first."""
    if speed is None:
        return tts_text
    if speed not in SPEEDS:
        raise ValueError(f"unknown speed '{speed}'")
    return f"<|prosody:speed_{speed}|>" + _SPEED_TAG.sub("", tts_text)
