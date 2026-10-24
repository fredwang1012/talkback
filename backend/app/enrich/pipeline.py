"""Novel enrichment for uploaded books: cast -> segment -> summaries -> index, saving the book after every stage.

The book is already readable (narrator only) when this starts; voices, recap and character talk switch on as each
stage lands. Cost controls: `estimate()` before starting, a per-book cap, and a cumulative cap tracked in
data/usage/upload_spend.json (see spend.py). Errors never take the book away: it stays readable with what was saved.
"""
import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Awaitable, Callable

from .. import books
from ..config import settings
from ..schema import Book
from ..ttschunks import chunk_paragraph
from . import cast as cast_mod
from . import minor as minor_mod
from . import segment as seg_mod
from . import spend
from . import summaries as sum_mod
from .llm import LLM, LLMRefusal, SpendLimit, effective_provider, estimate_usd, make_llm, no_llm_available

log = logging.getLogger("talkback.enrich")

STAGES = ("cast", "segment", "summaries", "index")
WEIGHTS = {"cast": 0.10, "segment": 0.55, "summaries": 0.30, "index": 0.05}
MESSAGES = {
    "cast": "Finding the main characters",
    "segment": "Splitting the dialogue by character",
    "summaries": "Writing chapter notes for the recap",
    "index": "Making the book searchable",
}
TOO_LONG = "This book is too long to process with characters; it will be read by the narrator."
BUDGET_USED = "Character processing is paused for now; this book will be read by the narrator."
FAILED = "Character processing stopped early; the book is still readable with the narrator."
FALLBACK_DONE = "Characters detected with a simple fallback (no LLM key configured). Add ANTHROPIC_API_KEY for full voices and recap."
BOOK_CAP_MARGIN = 1.25  # a running job stops when its real spend passes the per-book cap by this factor
ESTIMATE_MARGIN = 1.15

Report = Callable[..., None]  # report(status=, stage=, progress=, message=, estimate_usd=)
Indexer = Callable[[Book], Awaitable[None]]


def _offset(stage: str) -> float:
    return sum(WEIGHTS[s] for s in STAGES[:STAGES.index(stage)])


def estimate(book: Book, provider: str | None = None) -> float:
    """Estimated USD for enriching `book` with `provider`, from text length (includes a safety margin)."""
    provider = effective_provider(provider)
    total = sum(len(p.text) for p in book.paragraphs)
    # cast: one call (a long book is sampled), Sonnet-tier
    usd = estimate_usd(provider, "smart", min(total, cast_mod.CAST_MAX_CHARS) + len(cast_mod.UPLOAD_SYSTEM), 3000)
    # segmentation: batches of paragraphs that contain quotes; the model re-emits the text
    quoted = [len(p.text) for p in book.paragraphs if '"' in p.text or "“" in p.text]
    calls = math.ceil(len(quoted) / seg_mod.BATCH)
    in_chars = sum(quoted) + calls * (len(seg_mod.BATCH_SYSTEM) + 8 * 120 + 1500 + 400)
    out_tokens = sum(quoted) / 3.5 * 1.15 + 45 * len(quoted)
    usd += estimate_usd(provider, "fast", in_chars, out_tokens)
    # summaries: notes + condensations
    notes, cond, note_chars = sum_mod.estimate_calls(book)
    in_chars = note_chars + notes * len(sum_mod.NOTE_SYSTEM) + cond * (len(sum_mod.CONDENSE_SYSTEM) + min(cond, 60) * 150 * 6)
    usd += estimate_usd(provider, "fast", in_chars, notes * 220 + cond * 300)
    return round(usd * ESTIMATE_MARGIN, 4)


def check_caps(estimate_usd_: float) -> str | None:
    """None when enrichment may start; else the plain message to show the reader."""
    if estimate_usd_ > settings.upload_book_cap_usd:
        return TOO_LONG
    if spend.total_usd() + estimate_usd_ > settings.upload_total_cap_usd:
        return BUDGET_USED
    return None


class _Throttle:
    """Pass progress updates on at most every 0.5 s (and always the first and last)."""

    def __init__(self, report: Report, stage: str):
        self.report, self.stage, self.last = report, stage, 0.0

    def __call__(self, frac: float) -> None:
        now = time.monotonic()
        if frac >= 1.0 or now - self.last >= 0.5:
            self.last = now
            self.report(status="running", stage=self.stage,
                        progress=round(_offset(self.stage) + WEIGHTS[self.stage] * min(frac, 1.0), 3),
                        message=MESSAGES[self.stage])


def _book_path(book_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "book.json"


def rebuild_chunks(book: Book) -> None:
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)


async def enrich_book(book_id: str, report: Report, *, llm: LLM | None = None, indexer: Indexer | None = None) -> bool:
    """Run every stage on data/books/<id>/book.json. Returns True when all stages finished. Never raises."""
    path = _book_path(book_id)
    llm = llm or make_llm()
    cap = settings.upload_book_cap_usd * BOOK_CAP_MARGIN

    def on_usage(provider: str, model: str, n_in: int, n_out: int, usd: float) -> None:
        total = spend.add(book_id, provider, usd, flush_now=False)  # file writes happen off the loop (see flush)
        if llm.calls % 25 == 0:
            asyncio.get_running_loop().create_task(asyncio.to_thread(spend.flush))
        if total >= settings.upload_total_cap_usd:
            raise SpendLimit("upload budget used")
        if llm.usd > cap:
            raise SpendLimit("book cap")

    llm.on_usage = on_usage
    try:
        return await _run(book_id, path, report, llm, indexer)
    finally:
        await asyncio.shield(asyncio.to_thread(spend.flush))


async def run_stages(book: Book, report: Report, llm: LLM, *, state: dict, save: Callable[[Book], Awaitable[None]] | None = None,
                     indexer: Indexer | None = None, allow_fallback: bool = True) -> bool:
    """Cast, segment and summarise `book` in place; `save` runs after each stage. Returns True when degraded.

    `state["stage"]` always names the stage in progress (for the caller's error message). Raises on failure.
    With allow_fallback=False a missing LLM is an error instead of a name-based cast.
    """
    degraded = False

    async def checkpoint() -> None:
        if save is not None:
            await save(book)

    stage = state["stage"] = "cast"
    report(status="running", stage=stage, progress=0.0, message=MESSAGES[stage])
    try:
        await cast_mod.cast_novel(book, llm)
    except (asyncio.CancelledError, SpendLimit):
        raise
    except Exception as e:
        if not allow_fallback or not no_llm_available(e):
            raise
        # no working LLM at all: a simple name-based cast beats none
        log.warning("cast by LLM failed (%s); using the fallback cast", type(e).__name__)
        book.characters, book.narrator = [], None
        cast_mod.fallback_cast(book)
        degraded = True
    if book.narrator and book.narrator.is_character:
        rebuild_chunks(book)  # a first-person narrator reads in that character's voice
    await checkpoint()
    _Throttle(report, stage)(1.0)

    stage = state["stage"] = "segment"
    report(status="running", stage=stage, progress=_offset(stage), message=MESSAGES[stage])
    counts = await seg_mod.segment_book(book, llm, LLMRefusal, seg_mod.FAST, _Throttle(report, stage))
    if settings.minor_voices:  # free, offline: generic voices for uncast speakers
        counts["minor"] = len(minor_mod.assign_minor_voices(book))
    rebuild_chunks(book)
    await checkpoint()
    log.info("segmented book: %s", counts)
    if counts.get("fallback") and not (counts.get("llm") or counts.get("retry")):
        degraded = True

    stage = state["stage"] = "summaries"
    report(status="running", stage=stage, progress=_offset(stage), message=MESSAGES[stage])
    book.summaries = await sum_mod.build_summaries(book, llm, _Throttle(report, stage))
    await checkpoint()

    stage = state["stage"] = "index"
    report(status="running", stage=stage, progress=_offset(stage), message=MESSAGES[stage])
    if indexer is not None:
        await indexer(book)
    return degraded


async def _run(book_id: str, path: Path, report: Report, llm: LLM, indexer: Indexer | None) -> bool:
    state = {"stage": STAGES[0]}

    async def save(book: Book) -> None:
        await asyncio.to_thread(books.save_book, path, book)

    try:
        book = Book.model_validate_json(path.read_text(encoding="utf-8"))
        degraded = await run_stages(book, report, llm, state=state, save=save, indexer=indexer)
        report(status="done", stage="done", progress=1.0, degraded=degraded or None,
               message=FALLBACK_DONE if degraded else "Characters, recap and search are ready.")
        log.info("enriched %s: $%.4f in %d calls", book_id, llm.usd, llm.calls)
        return True
    except asyncio.CancelledError:
        raise
    except SpendLimit as e:
        log.warning("enrichment of %s stopped in %s: %s", book_id, state["stage"], e)
        report(status="error", stage=state["stage"], message=BUDGET_USED if "budget" in str(e) else TOO_LONG)
    except Exception as e:
        log.warning("enrichment of %s failed in %s: %s", book_id, state["stage"], type(e).__name__)
        report(status="error", stage=state["stage"], message=FAILED)
    return False
