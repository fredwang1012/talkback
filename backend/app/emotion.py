"""Emotion in spoken answers (#14).

The answer model starts each sentence with a feeling in square brackets ("[sadness] I miss him."). `split_marker`
removes it (and any stray marker) so it never reaches the screen or the TTS as words, and keeps it only when it is one
of the emotions the books already use (schema.EMOTIONS). The TTS then gets it as a Boson delivery tag.
"""
import re
from typing import AsyncIterator

from .schema import EMOTIONS

_OPEN, _CLOSE = re.escape("[［【"), re.escape("]］】")
# a leading marker: [sadness], [Emotion: fear], ［surprise］, or the Boson form <|emotion:fear|>
_LEAD = re.compile(rf"^\s*(?:<\|\s*emotion\s*:\s*([^|>]*?)\s*\|>|[{_OPEN}]\s*(?:emotion\s*:\s*)?([^{_OPEN}{_CLOSE}\n]{{0,30}}?)\s*[{_CLOSE}])\s*",
                   re.I)
_UNCLOSED = re.compile(rf"^\s*[{_OPEN}]\s*[^\W\d]+\s+")          # "[sadness I miss him." (no closing bracket)
# a stray bracket marker inside the text; (?<!\s) starts a match only at the first space of a run (linear, no backtrack)
_INNER = re.compile(rf"(?<!\s)\s*[{_OPEN}]\s*(?:emotion\s*:\s*)?[^\W\d]{{1,24}}\s*[{_CLOSE}]", re.I)
_TAG = re.compile(r"<\|[^|<>]*\|>")   # one Boson tag (any length)
SENTENCE_MAX = 2000   # longer "sentences" (e.g. untrusted service text) are cut before any parsing
_TAG_PASSES = 4


def strip_tags(text: str) -> str:
    """`text` with no Boson <|...|> tag left in it: tags are removed until none is left (a removal can join a new one,
    "<|<|x|>sfx:laughter|>"), and after a few passes any remaining "|" goes, so no "<|...|>" can remain. Linear."""
    for _ in range(_TAG_PASSES):
        if "<|" not in text or "|>" not in text:
            break
        text = _TAG.sub("", text)
    if "<|" in text or "|>" in text:
        text = text.replace("|", "")
    return re.sub(r"\s{2,}", " ", text).strip()


def split_marker(sentence: str) -> tuple[str, str | None]:
    """(clean text, emotion or None). Unknown, translated, malformed or 'neutral' markers give None."""
    s, found = sentence[:SENTENCE_MAX].strip(), None
    m = _LEAD.match(s)
    if m:
        value = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip().lower()
        found = value if value in EMOTIONS else None
        s = s[m.end():]
    else:
        s = _UNCLOSED.sub("", s, count=1)
    return strip_tags(_INNER.sub("", s)), found


def tts_input(text: str, emotion: str | None) -> str:
    """Boson input for one answer sentence: the emotion tag first (tag order: voice, style, emotion, ...), else the text."""
    return f"<|emotion:{emotion}|>{text}" if emotion in EMOTIONS else text


def valid(emotion) -> str | None:
    return emotion if isinstance(emotion, str) and emotion in EMOTIONS else None


async def tag_sentences(sentences: AsyncIterator[str]) -> AsyncIterator[tuple[str, str | None]]:
    """(text, emotion) per streamed sentence; a sentence that was only a marker is dropped.

    Closing this stream closes `sentences` too, so the answerer's own finally (token usage) still runs."""
    try:
        async for s in sentences:
            text, found = split_marker(s)
            if text:
                yield text, found
    finally:
        aclose = getattr(sentences, "aclose", None)
        if aclose is not None:
            await aclose()


def sentence_line(text: str, emotion: str | None) -> dict:
    """The NDJSON `sentence` message; neutral sentences carry no emotion field."""
    return {"type": "sentence", "text": text, **({"emotion": emotion} if emotion else {})}
