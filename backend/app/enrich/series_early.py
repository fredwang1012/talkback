"""Early series detection: right after an upload is published, ONE cheap call from title, author and the opening
text sets Book.series (+ series_index) so volumes stack in the Library at once, without waiting for the enrichment
queue. The later cast call keeps a series that is already set (cast.apply_series); the same-author reconciliation
(series_link) then runs for it.

Only the owner's novel uploads that have no series yet. Metered like other enrichment calls (spend caps); with no LLM
configured nothing happens. A clone uses the public service (/api/public/series-early). Never raises.
"""
import asyncio
import logging
import re
from typing import Protocol

from pydantic import BaseModel, Field

from .. import books
from ..config import settings
from ..schema import Book
from . import series_link, spend
from .cast import SERIES_MAX_INDEX, series_slug
from .llm import SpendLimit, estimate_usd

log = logging.getLogger("talkback.enrich")

EXCERPT_CHARS = 1500
OUT_TOKENS = 40
MAX_REQUEST_CHARS = 2400   # public route bound: title, author and the excerpt


class EarlyGuess(BaseModel):
    series: str | None = Field(default=None, description="short name of the series this book is a volume of, e.g. 'Harry Potter', else null")
    series_index: int | None = Field(default=None, description="its volume number in reading order (1 = first), else null")


SYSTEM = ("You decide from a book's title, author and opening text whether it is a volume of a book series. Give "
          "series and series_index when it is, whether or not the text says so: the title, title page or text shows it, "
          "or it is a well-known volume of one. The first book of a well-known series counts too (\"Dune\" is volume 1 "
          "of the Dune series; \"Harry Potter and the Philosopher's Stone\" is volume 1 of Harry Potter). Name the series "
          "by its common short name (\"Dune\", \"Harry Potter\", not \"The Dune Chronicles series\") and give the volume "
          "number in reading order (1 = first). Otherwise null for both. Never guess.")


def question(title: str, author: str, excerpt: str) -> str:
    clean = lambda s, n: re.sub(r"\s+", " ", s).strip()[:n]  # noqa: E731
    return f"Title: {clean(title, 150)}\nAuthor: {clean(author, 100)}\nOpening text: {clean(excerpt, EXCERPT_CHARS)}"


def estimate_usd_for(provider: str) -> float:
    return estimate_usd(provider, "fast", len(SYSTEM) + 300 + EXCERPT_CHARS, OUT_TOKENS)


class _LLM(Protocol):
    async def complete_json(self, system: str, user: str, schema_model, tier: str = "fast", **kw): ...


def _excerpt(book: Book) -> str:
    out, n = [], 0
    for p in book.paragraphs:
        out.append(p.text)
        n += len(p.text)
        if n >= EXCERPT_CHARS:
            break
    return " ".join(out)


def _apply(book_id: str, guess: EarlyGuess) -> bool:
    """Set the series under the write lock, only while the book still has none. True when it was set."""
    slug = series_slug(guess.series)
    if not slug:
        return False
    idx = guess.series_index
    done = []

    def change(b: Book) -> None:
        if b.series:
            return
        b.series = slug
        b.series_index = idx if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= SERIES_MAX_INDEX else None
        done.append(True)

    books.update_book(books._books_dir() / book_id / "book.json", change)
    return bool(done)


async def detect(book_id: str, llm: _LLM, *, provider: str | None = None) -> bool:
    """One call for one upload, then the same-author reconciliation. True when a series was set. Never raises."""
    try:
        if not book_id.startswith("u-"):
            return False
        book = await asyncio.to_thread(books.load_book, book_id)
        if book is None or book.kind != "novel" or book.series:
            return False
        provider = provider or ("gemini" if settings.upload_llm != "claude" else "claude")
        if spend.total_usd() + estimate_usd_for(provider) > settings.upload_total_cap_usd:
            return False
        try:
            guess = await llm.complete_json(SYSTEM, question(book.title, book.author, _excerpt(book)), EarlyGuess,
                                            "fast", max_tokens=OUT_TOKENS * 5)
        except SpendLimit:
            return False
        except Exception as e:
            log.warning("early series detection for %s failed: %s", book_id, type(e).__name__)
            return False
        found = await asyncio.to_thread(_apply, book_id, guess)
        await series_link.reconcile(book_id, llm_for_link(llm))
        return found
    except Exception as e:
        log.warning("early series detection for %s failed: %s", book_id, type(e).__name__)
        return False


def llm_for_link(llm):
    """The reconcile questions use another prompt: a public clone needs its own remote task, a local LLM is reused."""
    from .. import public_client
    if isinstance(llm, public_client.RemoteLLM):
        return public_client.remote_llm("series")
    return llm


async def detect_default(book_id: str) -> bool:
    """With whatever LLM this machine has (its own metered one, or the public service); False with none."""
    from .. import public_client
    from .llm import make_llm
    try:
        if not settings.upload_enrich:
            return False
        if public_client.active():
            return await detect(book_id, public_client.remote_llm("series-early"))
        if not public_client.local_llm_configured():
            return False
        llm = make_llm()
        llm.on_usage = lambda provider, model, n_in, n_out, usd: spend.add(book_id, provider, usd)
        return await detect(book_id, llm)
    except Exception as e:
        log.warning("early series detection for %s failed: %s", book_id, type(e).__name__)
        return False
