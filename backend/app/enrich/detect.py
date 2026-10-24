"""Novel or textbook? One cheap LLM call on a sample of the text, with a heuristic fallback.

`detect_kind(text)` never raises: when enrichment or the LLM is off, the spend cap is used up, or the call fails, the
heuristic decides. The call's cost is counted in the cumulative upload spend (it is tiny, but the caps stay honest).
"""
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from . import spend
from .llm import LLM, SpendLimit, make_llm

log = logging.getLogger("talkback.enrich")

SAMPLE_CHARS = 3000
SYSTEM = """You classify a document as a novel (fiction: a story with characters, scenes and dialogue) or a textbook (non-fiction teaching material: chapters with concepts, definitions, figures, tables, exercises, review questions).
You see three excerpts (start, middle, end). Answer with the kind and your confidence from 0 to 1."""


class KindGuess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["novel", "textbook"]
    confidence: float = Field(ge=0, le=1, description="0 to 1")


def sample(text: str, total: int = SAMPLE_CHARS) -> str:
    """About `total` chars from the start, middle and end (the whole text when it is short)."""
    text = text.strip()
    if len(text) <= total:
        return text
    part = total // 3
    mid = len(text) // 2 - part // 2
    return "\n[...]\n".join((text[:part], text[mid:mid + part], text[-part:]))


_QUOTED = re.compile(r'["“][^"”\n]{2,400}["”]')
_MARKERS = re.compile(
    r"\b(figure|fig\.|table|exercises?|review questions?|learning objectives?|chapter summary|key terms?|"
    r"summary|problems?|definition|theorem|example|glossary|references|bibliography)\s*\d*", re.I)
_NUMBERED_HEADING = re.compile(r"^\s*\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+[A-Z]", re.M)


def heuristic(text: str) -> tuple[str, float]:
    """(kind, confidence) from dialogue density, textbook markers and numbered section headings."""
    s = sample(text, 30_000)
    n = max(len(s), 1)
    quoted = sum(len(m.group(0)) for m in _QUOTED.finditer(s)) / n
    per_k = 1000 / n
    markers = len(_MARKERS.findall(s)) * per_k
    headings = len(_NUMBERED_HEADING.findall(s)) * per_k
    score = 0.0  # positive: textbook
    score += min(markers, 6) * 0.5 + min(headings, 4) * 0.8
    score -= min(quoted * 100, 30) / 3.0  # 30% of the text in quotes counts as -10
    kind = "textbook" if score > 0.5 else "novel"  # ties go to novel: the richer, reversible experience
    confidence = min(0.9, 0.5 + abs(score - 0.5) / 12)
    return kind, round(confidence, 2)


async def detect_kind(text: str, *, llm: LLM | None = None, book_id: str = "detect") -> tuple[str, float, str]:
    """(kind, confidence, method) with method "llm" or "heuristic"."""
    if settings.upload_enrich or llm is not None:
        try:
            if llm is None:
                if spend.total_usd() >= settings.upload_total_cap_usd:
                    raise SpendLimit("upload budget used")
                llm = make_llm()
                llm.on_usage = lambda provider, model, n_in, n_out, usd: spend.add(book_id, provider, usd)
            guess = await llm.complete_json(SYSTEM, sample(text), KindGuess, "fast", max_tokens=200)
            return guess.kind, round(guess.confidence, 2), "llm"
        except Exception as e:  # any failure: the heuristic decides
            log.warning("kind detection by LLM failed (%s); using the heuristic", type(e).__name__)
    kind, conf = heuristic(text)
    return kind, conf, "heuristic"
