"""Library reconciliation of series (#134): the first volume of a series often does not say it is one, so its cast
call answers series=None and the volumes never stack. After an upload is enriched (and once at startup for the books
already there) one tiny cheap call per same-author upload asks whether it belongs to the known series.

Only the owner's uploads (books.same_library), never built-in books or textbooks. At most MAX_CHECKS calls per upload;
each (book, series) pair is asked once (Book.series_checked). A failed call is not marked, so it is retried later.
"""
import logging
import re
from typing import Protocol

from pydantic import BaseModel, Field

from .. import books
from ..config import settings
from ..schema import Book
from . import spend
from .llm import SpendLimit, estimate_usd

log = logging.getLogger("talkback.enrich")

MAX_CHECKS = 5            # calls per upload
STARTUP_MAX_CHECKS = 20   # calls in one startup pass, over the whole library
OUT_TOKENS = 20
SERIES_MAX_INDEX = 99


class SeriesGuess(BaseModel):
    member: bool = Field(description="true if the book is a volume of that series")
    index: int | None = Field(default=None, description="its volume number in reading order (1 = first), else null")


SYSTEM = ("You answer questions about the order of books in a series. Answer from general knowledge of the book. "
          "member is true only if you are confident the book is a volume of that series; index is its volume number "
          "in reading order (1 = first) or null when unsure.")


def question(title: str, author: str, series: str) -> str:
    clean = lambda s, n: re.sub(r"\s+", " ", s.replace("'", "’")).strip()[:n]  # noqa: E731
    return (f"Is '{clean(title, 150)}' by {clean(author, 100)} a volume of the '{clean(series, 60)}' series? "
            f"If yes, which volume number in reading order?")


def check_estimate_usd(provider: str) -> float:
    return estimate_usd(provider, "fast", len(SYSTEM) + 300, OUT_TOKENS)


def author_key(author: str | None) -> str | None:
    """'Herbert, Frank' and 'FRANK  HERBERT.' -> the same key (sorted lowercase words); None when there is no author."""
    words = sorted(re.findall(r"[^\W_]+", (author or "").lower()))
    return " ".join(words) if words and words != ["unknown"] else None


class _LLM(Protocol):
    async def complete_json(self, system: str, user: str, schema_model, tier: str = "fast", **kw): ...


def _uploads() -> list[Book]:
    out = []
    for path in sorted(books._books_dir().glob("u-*/book.json")):
        try:
            b = books.load_book(path.parent.name)
        except Exception:
            continue
        if b is not None and b.kind == "novel":
            out.append(b)
    return out


def _same_library(a: str, b: str) -> bool:
    try:
        return books.same_library(a, b)
    except Exception:
        return False


def _record(book_id: str, series: str, member: bool, index: int | None) -> None:
    def change(b: Book) -> None:
        if series not in b.series_checked:
            b.series_checked.append(series)
        if member and not b.series:
            b.series = series
            b.series_index = index if isinstance(index, int) and 1 <= index <= SERIES_MAX_INDEX else None
    books.update_book(books._books_dir() / book_id / "book.json", change)


async def reconcile(book_id: str, llm: _LLM, *, max_checks: int = MAX_CHECKS) -> int:
    """Run the checks for one upload; returns the number of calls made. Never raises."""
    done = 0
    try:
        if not book_id.startswith("u-") or max_checks <= 0:
            return 0
        book = books.load_book(book_id)
        key = author_key(book.author) if book else None
        if book is None or book.kind != "novel" or key is None:
            return 0
        others = [b for b in _uploads() if b.id != book_id and author_key(b.author) == key and _same_library(book_id, b.id)]
        provider = "gemini" if settings.upload_llm != "claude" else "claude"
        # (book to ask about, the book that gets the answer, series to ask about)
        if book.series:
            jobs = [(o, o.id, book.series) for o in others if not o.series and book.series not in o.series_checked]
        else:
            seen: set[str] = set()
            jobs = []
            for o in others:
                if o.series and o.series not in seen and o.series not in book.series_checked:
                    seen.add(o.series)
                    jobs.append((book, book_id, o.series))
        for subject, target, series in jobs:
            if done >= max_checks:
                break
            if spend.total_usd() + check_estimate_usd(provider) > settings.upload_total_cap_usd:
                break
            if target == book_id and (books.load_book(book_id).series):   # an earlier answer already set it
                break
            try:
                guess = await llm.complete_json(SYSTEM, question(subject.title, subject.author, series), SeriesGuess,
                                                "fast", max_tokens=200)
            except SpendLimit:
                done += 1
                break
            except Exception as e:  # not marked: asked again next time
                log.warning("series check for %s failed: %s", subject.id, type(e).__name__)
                break
            done += 1
            await _save(target, series, bool(guess.member), guess.index)
            if guess.member and target == book_id:
                break
    except Exception as e:
        log.warning("series reconciliation for %s failed: %s", book_id, type(e).__name__)
    return done


async def _save(target: str, series: str, member: bool, index: int | None) -> None:
    import asyncio
    await asyncio.to_thread(_record, target, series, member, index)


async def startup_pass(llm: _LLM, max_checks: int = STARTUP_MAX_CHECKS) -> int:
    """Once per (book, series): uploads with no series that share an author with a series upload. Bounded."""
    done = 0
    for b in _uploads():
        if done >= max_checks:
            break
        if b.series:
            continue
        done += await reconcile(b.id, llm, max_checks=min(MAX_CHECKS, max_checks - done))
    if done:
        log.info("series reconciliation at startup: %d checks", done)
    return done


async def startup_default() -> int:
    """The startup pass with whatever LLM this machine has (its own metered one, or the public service); 0 with none."""
    from .. import public_client
    from .llm import make_llm
    if not settings.upload_enrich:
        return 0
    if public_client.active():
        return await startup_pass(public_client.remote_llm("series"))
    if not public_client.local_llm_configured():
        return 0
    llm = make_llm()
    llm.on_usage = lambda provider, model, n_in, n_out, usd: spend.add("series-startup", provider, usd)
    return await startup_pass(llm)
