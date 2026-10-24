"""summaries (Sonnet 5.5): rolling "story so far" notes, each written ONLY from the text up to its `upto_para`.

Usage: uv run --project backend python preprocess/summaries.py --book hp1-sorcerers-stone [--book ...] [--yes]
Incremental, so a long novel costs about one read of its text: the note at each point is written from the previous
note plus only the paragraphs since it. Spoiler safety stays structural: the model never sees a paragraph after
`upto_para` (the previous note was itself written from earlier text only). A note is written every EVERY paragraphs
(LONG_EVERY for long books) and at the end of every section. Novels only. Several --book values run in parallel.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.schema import Book, Summary  # noqa: E402
from app.books import save_book  # noqa: E402

MODEL = "claude-sonnet-5-5"
EVERY = 10
LONG_EVERY = 40      # books with more than LONG_BOOK paragraphs
LONG_BOOK = 1000
MAX_WORDS = 200


class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


SYSTEM = f"""You keep a "story so far" note for a reader who has read the text up to a point and nothing more.
You are given the previous note (covering everything before the new text) and the next stretch of the text.
Write the updated note: summarise only events, facts and character relationships established in the text so far, in at most {MAX_WORDS} words of plain prose.
Keep what still matters from the previous note, compress older events, and give the most recent events the most detail.
Do not speculate about what happens next, and do not use outside knowledge of the story or its sequels. Past tense, no lists."""

CallFn = Callable[..., Awaitable[SummaryResult]]


def every_for(book: Book) -> int:
    return LONG_EVERY if len(book.paragraphs) > LONG_BOOK else EVERY


def summary_points(book: Book, every: int | None = None) -> list[int]:
    """Paragraph indexes (inclusive) at which a summary is written: every `every` paragraphs and every section end."""
    every = every or every_for(book)
    last = len(book.paragraphs) - 1
    points = {i for i in range(every - 1, last + 1, every)}
    starts = sorted(s.start_para for s in book.sections)
    points |= {s - 1 for s in starts[1:] if s > 0}  # the paragraph before each section begins
    points.add(last)
    return sorted(p for p in points if 0 <= p <= last)


def prefix_text(book: Book, upto: int, start: int = 0) -> str:
    """The ONLY new text the model sees for a summary at `upto`: paragraphs start..upto, never anything later."""
    return "\n\n".join(p.text for p in book.paragraphs[start: upto + 1])


def section_title(book: Book, para: int) -> str:
    sid = book.paragraphs[para].section_id
    return next((s.title for s in book.sections if s.id == sid), "")


def build_user(book: Book, previous: str, start: int, upto: int) -> str:
    return (f'Book: "{book.title}"\n\nPrevious note:\n{previous or "(the story has just begun)"}\n\n'
            f"New text ({section_title(book, upto)}):\n\n{prefix_text(book, upto, start)}")


def trim_words(text: str, limit: int = MAX_WORDS + 30) -> str:
    words = text.split()
    return text.strip() if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "."


async def build_summaries(book: Book, call: CallFn, every: int | None = None) -> list[Summary]:
    """Sequential chain: each note = f(previous note, paragraphs since the previous point)."""
    out: list[Summary] = []
    previous, start = "", 0
    for upto in summary_points(book, every):
        result = await call(MODEL, SYSTEM, build_user(book, previous, start, upto), SummaryResult,
                            max_tokens=800, effort="low")
        previous = trim_words(result.summary)
        out.append(Summary(upto_para=upto, text=previous))
        start = upto + 1
    return out


def estimate(book: Book) -> float:
    import llm

    points = summary_points(book)
    chars = sum(len(p.text) for p in book.paragraphs) + len(points) * (len(SYSTEM) + 6 * MAX_WORDS)
    pin, pout = llm.PRICES[MODEL]
    return (chars / 3 * pin + 500 * len(points) * pout) / 1e6  # measured on book 1: ~3 chars/token, ~500 out/note


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    paths = [llm.ROOT / "data/books" / b / "book.json" for b in args.book]
    books = [Book.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    if any(b.kind != "novel" for b in books):
        sys.exit("Summaries are for novels; textbooks use the whole chapter.")
    total = 0.0
    for b in books:
        est = estimate(b)
        total += est
        print(f"summaries {b.id}: {len(summary_points(b))} notes from {MODEL}, estimated ${est:.2f}")
    print(f"total estimated ${total:.2f}")
    if total > 5 and not args.yes:
        sys.exit("estimate > $5; rerun with --yes")

    async def one(book: Book, path: Path) -> None:
        book.summaries = await build_summaries(book, llm.call_structured)
        save_book(path, book)
        print(f"wrote {len(book.summaries)} summaries for {book.id}")

    await asyncio.gather(*(one(b, p) for b, p in zip(books, paths)))
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
