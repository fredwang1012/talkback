"""Dialogue segmentation: split paragraphs into verbatim segments by speaker, each with at most one emotion.

Shared by preprocess/segment.py (offline) and the upload pipeline. Every LLM answer is validated: the segment texts
must tile the paragraph exactly (whitespace aside) and use known speakers. Only main characters are speakers
(`Character.main` is not False); any other speaker maps to the narrator. Novels are sent BATCH paragraphs per call;
a paragraph whose batch answer fails validation is retried on its own once, then falls back to the narrator.
`call(model, system, user, schema, max_tokens=, effort=)` is injected; `model` is passed through unchanged.
"""
import asyncio
import re
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict

from ..schema import EMOTIONS, Book, Paragraph, Segment
from .llm import SpendLimit
from .tasks import gather_or_cancel

FAST = "fast"
Emotion = Literal[
    "elation", "amusement", "enthusiasm", "determination", "pride", "contentment", "affection", "relief",
    "contemplation", "confusion", "surprise", "awe", "longing", "anger", "fear", "disgust", "bitterness",
    "sadness", "shame", "helplessness",
]
Style = Literal["whispering", "shouting"]
assert set(Emotion.__args__) == set(EMOTIONS)  # type: ignore[attr-defined]  # keep in step with the app schema



class SegItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    text: str
    emotion: Emotion | None
    style: Style | None = None


class SegResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[SegItem]


SYSTEM = """You split one paragraph of a story into consecutive segments for a full-cast audiobook.

Rules:
- Cover the paragraph exactly: the segments, concatenated in order, must reproduce the paragraph text word for word. Never add, drop, reorder or reword anything.
- A character's spoken words (inside quotation marks, including the quotation marks) belong to that character's id from the cast list.
- Everything else belongs to "narrator": description, thoughts, and speech tags such as "said Hagrid" or "I answered".
- Start a new segment whenever the speaker changes. If a quote is interrupted by a speech tag and then continues, that is three segments.
- emotion: choose at most one of the allowed values per segment, only when the text clearly expresses it; otherwise null. Narration is usually null.
- style: for a character's speech only, "whispering" if the text says it is whispered, hissed, murmured or muttered, "shouting" if it is shouted, yelled, bellowed, roared or screamed (or written in capitals); otherwise null. Narration is always null.
- Use only speaker ids from the cast list, plus "narrator"."""



def main_characters(book: Book) -> list:
    """Characters that may be speakers: everyone except those explicitly marked minor (main=False)."""
    return [c for c in book.characters if c.main is not False]


def speaker_ids(book: Book) -> set[str]:
    return {c.id for c in main_characters(book)} | {"narrator"}

CallFn = Callable[..., Awaitable[SegResult]]


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_QUOTES = set("\"“”‘’'«»")


def _is_quotes(text: str) -> bool:
    return bool(text) and set(text) <= _QUOTES


def validate_segments(paragraph: str, items: list[SegItem], speakers: set[str]) -> list[Segment] | None:
    """Return Segment objects if `items` tile `paragraph` exactly and use known speakers, else None.

    Quotation marks the model left out (Gemini often drops the quote marks around speech) are put back from the
    paragraph itself, so the repaired segments still tile it verbatim: an opening mark goes with the quote that
    follows it, a closing mark with the quote it ends. Every segment's text is a slice of the paragraph (spaces next
    to a restored mark included), and the result is checked the way ttschunks.chunk_paragraph reads it.
    """
    if not items:
        return None
    cursor = 0
    spans: list[list] = []  # [start, end, item] in paragraph offsets
    for it in items:
        text = it.text.strip()
        if not text:
            return None
        at = paragraph.find(text, cursor)
        if at < 0:
            return None
        raw_gap = paragraph[cursor:at]
        gap = raw_gap.strip()
        start, end = at, at + len(text)
        opened = False
        if gap:
            if not _is_quotes(gap):  # text missing, reordered, or something skipped between segments
                return None
            g0 = cursor + len(raw_gap) - len(raw_gap.lstrip())   # the gap's first and last mark
            g1 = cursor + len(raw_gap.rstrip())
            prev = spans[-1] if spans else None
            prev_text = paragraph[prev[0]:prev[1]] if prev else ""
            if prev is not None and prev_text[:1] in _QUOTES and prev_text[-1:] not in _QUOTES:
                prev[1] = g1  # closing mark
            else:
                start, opened = g0, True  # opening mark
        cursor = end
        if opened:  # an opening mark was added: take the closing mark that follows too
            tail = paragraph[cursor:cursor + 1]
            if tail and tail in _QUOTES:
                end = cursor = cursor + 1
        spans.append([start, end, it])
    rest = paragraph[cursor:]
    if rest.strip():
        if _is_quotes(rest.strip()):
            spans[-1][1] = cursor + len(rest.rstrip())
        else:
            return None
    out: list[Segment] = []
    for start, end, it in spans:
        speaker = it.speaker if it.speaker in speakers else "narrator"
        out.append(Segment(speaker=speaker, text=paragraph[start:end].strip(), emotion=it.emotion,
                           style=it.style if speaker != "narrator" else None))
    return out if tiles(paragraph, out) else None


def tiles(paragraph: str, segments: list[Segment]) -> bool:
    """True when the segment texts are found in `paragraph` in order (as ttschunks.chunk_paragraph looks them up)
    and cover all of it but whitespace."""
    cursor, left = 0, []
    for s in segments:
        t = s.text.strip()
        if not t:
            continue
        at = paragraph.find(t, cursor)
        if at < 0:
            return False
        left.append(paragraph[cursor:at])
        cursor = at + len(t)
    left.append(paragraph[cursor:])
    return not "".join(left).strip()


def narrator_only(paragraph: str) -> list[Segment]:
    return [Segment(speaker="narrator", text=paragraph.strip(), emotion=None)]


def needs_llm(book: Book, p: Paragraph) -> bool:
    return book.kind == "novel" and bool(main_characters(book)) and '"' in p.text


def build_user(book: Book, p: Paragraph) -> str:
    cast = "\n".join(f'- id "{c.id}": {c.name} (also called: {", ".join(c.aliases)})' for c in main_characters(book))
    before = "\n\n".join(q.text for q in book.paragraphs[max(0, p.idx - 3):p.idx]) or "(start of the story)"
    narrator = book.narrator.is_character if book.narrator else None
    note = f' The first-person narrator is the character "{narrator}"; their narration stays "narrator".' if narrator else ""
    return f"Cast:\n{cast}{note}\n\nPrevious paragraphs (context only):\n{before}\n\nParagraph to split:\n{p.text}"


async def segment_paragraph(book: Book, p: Paragraph, call: CallFn, refusal_exc: type[Exception] = Exception,
                            model: str = FAST) -> tuple[list[Segment], str]:
    """Returns (segments, how) where how is 'skipped', 'llm', 'retry' or 'fallback'."""
    if not needs_llm(book, p):
        return narrator_only(p.text), "skipped"
    speakers = speaker_ids(book)
    user = build_user(book, p)
    for attempt in range(2):
        try:
            result = await call(model, SYSTEM, user, SegResult, max_tokens=max(3000, 2 * len(p.text)), effort="low")
        except (refusal_exc, RuntimeError):  # refused, or ran out of tokens: the narrator reads this paragraph
            break  # refused: fall back to the narrator for this paragraph
        ok = validate_segments(p.text, result.segments, speakers)
        if ok:
            return ok, "llm" if attempt == 0 else "retry"
    return narrator_only(p.text), "fallback"


# ---------- batches: several paragraphs per call (one cast list and context for all of them) ----------

BATCH = 10


class BatchPara(BaseModel):
    model_config = ConfigDict(extra="forbid")
    para: int
    segments: list[SegItem]


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paragraphs: list[BatchPara]


BATCH_SYSTEM = SYSTEM.replace("You split one paragraph", "You split each numbered paragraph") + """
- Return one entry per numbered paragraph, with its number in "para"."""


def build_batch_user(book: Book, paras: list[Paragraph]) -> str:
    cast = "\n".join(f'- id "{c.id}": {c.name} (also called: {", ".join(c.aliases)})' for c in main_characters(book))
    first = paras[0].idx
    before = "\n\n".join(q.text for q in book.paragraphs[max(0, first - 2):first]) or "(start of the story)"
    narrator = book.narrator.is_character if book.narrator else None
    note = f' The first-person narrator is the character "{narrator}"; their narration stays "narrator".' if narrator else ""
    todo = "\n\n".join(f"[{p.idx}] {p.text}" for p in paras)
    return f"Cast:\n{cast}{note}\n\nPrevious paragraphs (context only):\n{before}\n\nParagraphs to split:\n{todo}"


async def segment_batch(book: Book, paras: list[Paragraph], call: CallFn,
                        refusal_exc: type[Exception] = Exception, model: str = FAST) -> list[tuple[list[Segment], str]]:
    """Segments for each paragraph in `paras` (all need the LLM), in order. Failed ones go through segment_paragraph."""
    speakers = speaker_ids(book)
    answers: dict[int, list[SegItem]] = {}
    try:
        result = await call(model, BATCH_SYSTEM, build_batch_user(book, paras), BatchResult,
                            max_tokens=4000 + 600 * len(paras), effort="low")
        answers = {bp.para: bp.segments for bp in result.paragraphs}
    except (refusal_exc, RuntimeError):  # refused or out of tokens: each paragraph is retried on its own below
        pass
    out = []
    for p in paras:
        ok = validate_segments(p.text, answers.get(p.idx, []), speakers)
        out.append((ok, "llm") if ok else await segment_paragraph(book, p, call, refusal_exc, model))
    return out


def batches(book: Book) -> list[list[Paragraph]]:
    """Consecutive runs of paragraphs that need the LLM, BATCH at a time."""
    todo = [p for p in book.paragraphs if needs_llm(book, p)]
    return [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]


async def segment_book(book: Book, call: CallFn, refusal_exc: type[Exception] = Exception, model: str = FAST,
                       on_progress: Callable[[float], None] | None = None) -> dict[str, int]:
    """Fill `segments` of every paragraph. All batches run concurrently (the caller's `call` limits parallelism).

    Returns counts by how: skipped / llm / retry / fallback.
    """
    todo = batches(book)
    done: dict[int, tuple[list[Segment], str]] = {}
    finished = 0

    async def one(b: list[Paragraph]) -> None:
        nonlocal finished
        try:
            results = await segment_batch(book, b, call, refusal_exc, model)
        except SpendLimit:
            raise  # stops the whole stage: siblings are cancelled
        except Exception:  # any other provider failure: the narrator reads this batch
            results = [(narrator_only(p.text), "fallback") for p in b]
        for p, r in zip(b, results):
            done[p.idx] = r
        finished += 1
        if on_progress:
            on_progress(finished / len(todo))

    await gather_or_cancel(one(b) for b in todo)
    counts: dict[str, int] = {}
    for p in book.paragraphs:
        segs, how = done.get(p.idx) or (narrator_only(p.text), "skipped")
        p.segments = segs
        counts[how] = counts.get(how, 0) + 1
    return counts
