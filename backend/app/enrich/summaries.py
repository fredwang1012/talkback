"""Parallel "story so far" notes for uploaded novels (spoiler-safe by construction).

The offline chain in preprocess/summaries.py is sequential and too slow for an upload. Here every chapter ("unit") is
written independently and in parallel: a checkpoint note every CHECKPOINT_EVERY paragraphs and one at the chapter end,
each written ONLY from that chapter's text up to the checkpoint. A chapter longer than UNIT_MAX_PARAS paragraphs (or
UNIT_MAX_CHARS characters) is cut into several units, which bounds prompt size.

The stored summary at paragraph P (a checkpoint) = condensed end-notes of the units completed before P's unit
+ the latest checkpoint note of P's unit (the one at P). Spoiler invariant: no summary text derives from a paragraph
after its `upto_para`. A condensation of completed units uses only their end notes, which came from text before P.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from ..schema import Book, Summary
from .llm import LLMRefusal

log = logging.getLogger("talkback.enrich")

CHECKPOINT_EVERY = 40
UNIT_MAX_PARAS = 200
UNIT_MAX_CHARS = 60_000
NOTE_WORDS = 150
STORY_WORDS = 220
TIER = "fast"


class NoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


NOTE_SYSTEM = f"""You write a short note about a story for a reader who has read only the text you are given: the opening of a chapter, up to a point.
Summarise only events, facts and character relationships established in that text, in at most {NOTE_WORDS} words of plain prose.
Do not speculate about what happens next, and do not use outside knowledge of the book or its sequels. Past tense, no lists."""

CONDENSE_SYSTEM = f"""You are given notes about consecutive chapters of a story, oldest first. They are everything the reader has read before the current chapter.
Condense them into one "story so far" of at most {STORY_WORDS} words of plain prose: keep what still matters, compress older events, and give the most recent chapters the most detail.
Use only what the notes say. Do not speculate about what happens next, and do not use outside knowledge of the book or its sequels. Past tense, no lists."""

CallFn = Callable[..., Awaitable[NoteResult]]


@dataclass(frozen=True)
class Unit:
    start: int
    end: int  # inclusive paragraph index
    title: str

    @property
    def checkpoints(self) -> list[int]:
        pts = list(range(self.start + CHECKPOINT_EVERY - 1, self.end, CHECKPOINT_EVERY))
        return pts + [self.end]


def units(book: Book) -> list[Unit]:
    """Chapters as (start, end) ranges, long ones cut at UNIT_MAX_PARAS / UNIT_MAX_CHARS."""
    n = len(book.paragraphs)
    if n == 0:
        return []
    starts = sorted({s.start_para for s in book.sections if 0 <= s.start_para < n} | {0})
    titles = {s.start_para: s.title for s in book.sections}
    out: list[Unit] = []
    for i, a in enumerate(starts):
        b = (starts[i + 1] if i + 1 < len(starts) else n) - 1
        cur, size = a, 0
        for idx in range(a, b + 1):
            size += len(book.paragraphs[idx].text) + 2
            if idx - cur + 1 >= UNIT_MAX_PARAS or (size >= UNIT_MAX_CHARS and idx > cur):
                out.append(Unit(cur, idx, titles.get(a, "")))
                cur, size = idx + 1, 0
        if cur <= b:
            out.append(Unit(cur, b, titles.get(a, "")))
    return out


def unit_text(book: Book, unit: Unit, upto: int) -> str:
    """The ONLY text a note at `upto` may see: the unit's paragraphs from its start to `upto` (never later)."""
    assert unit.start <= upto <= unit.end
    return "\n\n".join(p.text for p in book.paragraphs[unit.start: upto + 1])


def note_user(book: Book, unit: Unit, upto: int) -> str:
    head = f'Book: "{book.title}"' + (f"\nChapter: {unit.title}" if unit.title else "")
    return f"{head}\n\nText so far in this chapter:\n\n{unit_text(book, unit, upto)}"


def trim_words(text: str, limit: int) -> str:
    words = text.split()
    return text.strip() if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "."


def fallback_condense(notes: list[str], limit: int = STORY_WORDS) -> str:
    """No LLM: the most recent notes in full while the budget allows, older ones cut to their first sentence."""
    budget, kept = limit, []
    for note in reversed(notes):
        words = note.split()
        if len(words) <= budget:
            kept.append(note.strip())
            budget -= len(words)
            continue
        first = note.split(". ")[0].strip()
        short = trim_words(first, max(0, min(budget, 25)))
        if short and budget > 0:
            kept.append(short)
            budget -= len(short.split())
    return " ".join(reversed(kept))


def estimate_calls(book: Book) -> tuple[int, int, int]:
    """(note calls, condense calls, total input characters of the note calls) for cost estimates."""
    us = units(book)
    notes = chars = 0
    for u in us:
        for c in u.checkpoints:
            notes += 1
            chars += sum(len(p.text) + 2 for p in book.paragraphs[u.start: c + 1]) + 120
    return notes, max(0, len(us) - 1), chars


async def build_summaries(book: Book, call: CallFn, on_progress: Callable[[float], None] | None = None) -> list[Summary]:
    """All notes run concurrently (the caller's `call` bounds the parallelism), then the condensations, also concurrently."""
    us = units(book)
    total = sum(len(u.checkpoints) for u in us) + max(0, len(us) - 1)
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if on_progress and total:
            on_progress(done / total)

    notes: dict[tuple[int, int], str] = {}  # (unit index, checkpoint para) -> note

    async def note(ui: int, u: Unit, c: int) -> None:
        try:
            r = await call(TIER, NOTE_SYSTEM, note_user(book, u, c), NoteResult, max_tokens=1000, effort="low")
            text = trim_words(r.summary, NOTE_WORDS + 30)
            if text:
                notes[(ui, c)] = text
        except (RuntimeError, ValueError, LLMRefusal) as e:  # refusal/out of tokens/bad JSON: checkpoint skipped
            log.warning("summary note skipped (%s)", type(e).__name__)  # SpendLimit is not caught: it stops the job
        tick()

    await asyncio.gather(*(note(ui, u, c) for ui, u in enumerate(us) for c in u.checkpoints))

    def end_note(ui: int) -> str | None:
        """The unit's end note; if that call failed, its latest earlier checkpoint (still only earlier text)."""
        for c in reversed(us[ui].checkpoints):
            if (ui, c) in notes:
                return notes[(ui, c)]
        return None

    prior: list[list[str]] = [[n for n in (end_note(j) for j in range(ui)) if n] for ui in range(len(us))]
    condensed: dict[int, str] = {}

    async def condense(ui: int) -> None:
        ns = prior[ui]
        if not ns:
            condensed[ui] = ""
        elif sum(len(n.split()) for n in ns) <= STORY_WORDS:
            condensed[ui] = " ".join(ns)
        else:
            try:
                r = await call(TIER, CONDENSE_SYSTEM, "\n\n".join(f"Chapter {i + 1}: {n}" for i, n in enumerate(ns)),
                               NoteResult, max_tokens=800, effort="low")
                condensed[ui] = trim_words(r.summary, STORY_WORDS + 30) or fallback_condense(ns)
            except (RuntimeError, ValueError, LLMRefusal):
                condensed[ui] = fallback_condense(ns)
            tick()

    await asyncio.gather(*(condense(ui) for ui in range(len(us))))
    if on_progress:
        on_progress(1.0)

    out: list[Summary] = []
    for ui, u in enumerate(us):
        for c in u.checkpoints:
            if (ui, c) not in notes:
                continue
            before = condensed.get(ui, "")
            text = f"{before}\n\nThe current chapter so far: {notes[(ui, c)]}" if before else notes[(ui, c)]
            out.append(Summary(upto_para=c, text=text))
    return sorted(out, key=lambda s: s.upto_para)
