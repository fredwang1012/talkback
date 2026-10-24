"""Study-guide recap notes for textbooks: one short summary per chapter and (when the estimate allows) per numbered
subsection, stored as `Summary(upto_para=<section's last paragraph>, section_id=<section>)`.

Textbooks have no spoilers, so recap.py may show any section's note wherever the reader is. Each note is still written
only from its own section's text (a chapter with subsections: its opening text plus its subsections' notes, so the
book is read about once), which keeps `upto_para` honest if the book is ever moved to Novels.

Cost: about one read of the book's text in the "fast" tier plus a short answer per section; `estimate()` before any
call, and `plan()` drops the subsections when only the chapters fit the per-book cap.
"""
import logging
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

from ..config import settings
from ..schema import Book, Paragraph, Summary
from .llm import SpendLimit, effective_provider, estimate_usd
from .tasks import gather_or_cancel

log = logging.getLogger("talkback.enrich")

TIER = "fast"
SECTION_MAX_CHARS = 120_000   # one call reads at most this much of a section (~34K tokens); longer ones are cut
SUBSECTION_LEVEL = 2          # "4.2"; deeper headings ("4.2.1") are folded into their subsection
MAX_SUBSECTIONS = 600         # more than this: chapters only (a mis-parsed heading list)
OUT_TOKENS = 450              # per call, for the estimate (a 3-6 sentence answer plus JSON, with room to spare)
NOTE_CHARS = 900              # length of a subsection note when a chapter call reads it back
ESTIMATE_MARGIN = 1.15


class SectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", title="SectionSummary")
    summary: str


SECTION_SYSTEM = """You write a study-guide summary of one section of a textbook, for a student revising it.
Use only the text you are given. In 3 to 6 sentences of plain prose, say what the section is about: its key ideas, each definition it gives, and every formula, algorithm or method it introduces, with formulas named and explained in words (no LaTeX, no symbols the reader would have to see).
Do not add facts, examples, opinions or outside knowledge that are not in the text, and do not mention page or figure numbers. No lists, no headings, no Markdown."""

CHAPTER_SYSTEM = """You write a study-guide summary of one chapter of a textbook, for a student revising it.
You are given the chapter's opening text and short summaries of each of its subsections, in order. Use only those.
In 3 to 6 sentences of plain prose, give an overview of the chapter: its main ideas, the key definitions, and the formulas, algorithms or methods it introduces, named and explained in words (no LaTeX).
Do not add facts, examples, opinions or outside knowledge that are not given, and do not mention page or figure numbers. No lists, no headings, no Markdown."""

CallFn = Callable[..., Awaitable[SectionSummary]]


@dataclass(frozen=True)
class Target:
    section_id: str
    title: str
    level: int
    start: int
    end: int                       # inclusive
    children: tuple[str, ...] = ()  # subsection targets inside a chapter (summarised first)


def targets(book: Book, include_subsections: bool = True) -> list[Target]:
    """Chapters (level 1) and, optionally, level-2 subsections, each with its paragraph range (up to the next heading of
    the same or a higher level). Subsections come before their chapter in the list."""
    n = len(book.paragraphs)
    secs = sorted((s for s in book.sections if 0 <= s.start_para < n), key=lambda s: s.start_para)
    subs = include_subsections and sum(1 for s in secs if s.level == SUBSECTION_LEVEL) <= MAX_SUBSECTIONS

    def end_of(i: int) -> int:
        lvl = max(1, secs[i].level)
        nxt = next((s.start_para for s in secs[i + 1:] if max(1, s.level) <= lvl), n)
        return max(secs[i].start_para, nxt - 1)

    out: list[Target] = []
    for i, s in enumerate(secs):
        if s.level > 1:
            continue
        end = end_of(i)
        kids = []
        if subs:
            for j in range(i + 1, len(secs)):
                if secs[j].level <= 1:
                    break
                if secs[j].level == SUBSECTION_LEVEL:
                    kids.append(Target(secs[j].id, secs[j].title, SUBSECTION_LEVEL, secs[j].start_para, min(end_of(j), end)))
        out += kids
        out.append(Target(s.id, s.title, 1, s.start_para, end, tuple(k.section_id for k in kids)))
    return out


def para_text(p: Paragraph) -> str:
    """What a reader would hear: a display equation as its spoken reading (or LaTeX), else the text."""
    if p.kind == "math" and p.math is not None and (p.math.spoken or p.math.latex):
        return p.math.spoken or p.math.latex or p.text
    return p.text


def section_text(book: Book, start: int, end: int) -> str:
    """Paragraphs start..end (inclusive), cut at SECTION_MAX_CHARS."""
    out, size = [], 0
    for p in book.paragraphs[start: end + 1]:
        t = para_text(p)
        if size + len(t) > SECTION_MAX_CHARS:
            out.append(t[: max(0, SECTION_MAX_CHARS - size)] + " [the section continues; the rest is not shown]")
            break
        out.append(t)
        size += len(t) + 2
    return "\n\n".join(out)


def _intro_end(t: Target, by_id: dict[str, Target]) -> int:
    """Last paragraph of a chapter's opening text (before its first subsection)."""
    return by_id[t.children[0]].start - 1 if t.children else t.end


def section_user(book: Book, t: Target) -> str:
    return f'Book: "{book.title}"\nSection: {t.title}\n\nText:\n\n{section_text(book, t.start, t.end)}'


def chapter_user(book: Book, t: Target, by_id: dict[str, Target], notes: dict[str, str]) -> str:
    end = _intro_end(t, by_id)
    intro = section_text(book, t.start, end) if end >= t.start else "(none)"
    parts = [f"Subsection {by_id[c].title}: {notes[c]}" for c in t.children if c in notes]
    return f'Book: "{book.title}"\nChapter: {t.title}\n\nOpening text:\n\n{intro}\n\nSubsection summaries:\n\n' + "\n\n".join(parts)


def _chars(book: Book, start: int, end: int) -> int:
    return min(SECTION_MAX_CHARS, sum(len(para_text(p)) + 2 for p in book.paragraphs[start: end + 1]))


def estimate(book: Book, provider: str | None = None, include_subsections: bool = True) -> float:
    """Estimated USD (with a safety margin) for the notes of `book`."""
    provider = effective_provider(provider)
    ts = targets(book, include_subsections)
    by_id = {t.section_id: t for t in ts}
    in_chars = 0
    for t in ts:
        if t.children:
            end = _intro_end(t, by_id)
            in_chars += len(CHAPTER_SYSTEM) + (_chars(book, t.start, end) if end >= t.start else 0) + len(t.children) * NOTE_CHARS
        else:
            in_chars += len(SECTION_SYSTEM) + _chars(book, t.start, t.end)
        in_chars += 200
    return round(estimate_usd(provider, TIER, in_chars, len(ts) * OUT_TOKENS) * ESTIMATE_MARGIN, 4)


def plan(book: Book, provider: str | None = None, cap: float | None = None) -> tuple[float, bool]:
    """(estimate, include_subsections): subsections too when that still fits `cap` (default: the per-book upload cap)."""
    cap = settings.upload_book_cap_usd if cap is None else cap
    full = estimate(book, provider, True)
    if full <= cap:
        return full, True
    return estimate(book, provider, False), False


def has_notes(book: Book) -> bool:
    return any(s.section_id for s in book.summaries)


async def build(book: Book, call: CallFn, on_progress: Callable[[float], None] | None = None,
                include_subsections: bool = True, out: list[Summary] | None = None) -> list[Summary]:
    """All subsection notes concurrently (the caller's `call` bounds parallelism), then the chapters. A failed call
    skips that section; SpendLimit stops the job (siblings cancelled). `out` collects notes as they land, so a caller
    can keep what was written before a stop."""
    ts = targets(book, include_subsections)
    by_id = {t.section_id: t for t in ts}
    out = out if out is not None else []
    notes: dict[str, str] = {}
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if on_progress and ts:
            on_progress(done / len(ts))

    async def one(t: Target) -> None:
        if t.children and any(c in notes for c in t.children):
            system, user = CHAPTER_SYSTEM, chapter_user(book, t, by_id, notes)
        else:  # no subsections (or none of their notes landed): read the chapter itself
            system, user = SECTION_SYSTEM, section_user(book, t)
        try:
            r = await call(TIER, system, user, SectionSummary, max_tokens=1000, effort="low")
            text = " ".join(r.summary.split())
            if text:
                notes[t.section_id] = text
                out.append(Summary(upto_para=t.end, text=text, section_id=t.section_id))
        except SpendLimit:
            raise
        except Exception as e:  # refusal / bad JSON / provider failure: this section has no note
            log.warning("textbook note skipped (%s)", type(e).__name__)
        tick()

    await gather_or_cancel(one(t) for t in ts if t.level != 1)
    await gather_or_cancel(one(t) for t in ts if t.level == 1)
    if on_progress:
        on_progress(1.0)
    return sorted(out, key=lambda s: (s.upto_para, s.section_id or ""))


_save_lock = threading.Lock()


def save_summaries(book_id: str, summaries: list[Summary]) -> bool:
    """Re-read book.json and set only its summaries (the equations job may have saved it meanwhile). False when the
    book is gone or is no longer a textbook."""
    from .. import books, ingest_upload
    path = settings.data_dir / "books" / book_id / "book.json"
    with ingest_upload._eq_lock, _save_lock:
        try:
            book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if book.kind != "textbook":
            return False
        book.summaries = sorted(summaries, key=lambda s: (s.upto_para, s.section_id or ""))
        books.save_book(path, book)
    return True
