"""summaries (Sonnet 5.5): rolling "story so far" notes, each written ONLY from the text up to its `upto_para`.

Usage: uv run --project backend python preprocess/summaries.py --book scandal-in-bohemia [--yes]
Spoiler safety is structural: the model is only ever shown paragraphs 0..upto_para, so a summary cannot contain what
comes later. A summary is written every ~10 paragraphs and at the end of every section. Novels only.
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

MODEL = "claude-sonnet-5-5"
EVERY = 10
MAX_WORDS = 120


class SummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


SYSTEM = f"""You write a "story so far" note for a reader who has read the text below and nothing more.
Summarise only events, facts and character relationships established in the text provided, in at most {MAX_WORDS} words of plain prose.
Do not speculate about what happens next, and do not use outside knowledge of the story. Past tense, no lists."""

CallFn = Callable[..., Awaitable[SummaryResult]]


def summary_points(book: Book) -> list[int]:
    """Paragraph indexes (inclusive) at which a summary is written: every EVERY paragraphs and every section end."""
    last = len(book.paragraphs) - 1
    points = {i for i in range(EVERY - 1, last + 1, EVERY)}
    starts = sorted(s.start_para for s in book.sections)
    points |= {s - 1 for s in starts[1:] if s > 0}  # the paragraph before each section begins
    points.add(last)
    return sorted(p for p in points if 0 <= p <= last)


def prefix_text(book: Book, upto: int) -> str:
    """The ONLY text the model sees for a summary at `upto`: paragraphs 0..upto, never anything later."""
    return "\n\n".join(p.text for p in book.paragraphs[: upto + 1])


def trim_words(text: str, limit: int = MAX_WORDS + 30) -> str:
    words = text.split()
    return text.strip() if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "."


async def build_summaries(book: Book, call: CallFn) -> list[Summary]:
    points = summary_points(book)

    async def one(upto: int) -> Summary:
        user = f'Text of "{book.title}" up to this point:\n\n{prefix_text(book, upto)}'
        result = await call(MODEL, SYSTEM, user, SummaryResult, max_tokens=500, effort="low")
        return Summary(upto_para=upto, text=trim_words(result.summary))

    return list(await asyncio.gather(*(one(u) for u in points)))


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    path = llm.ROOT / "data/books" / args.book / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    if book.kind != "novel":
        sys.exit("Summaries are for novels; textbooks use the whole chapter.")

    points = summary_points(book)
    chars = sum(len(prefix_text(book, u)) + len(SYSTEM) for u in points)
    pin, pout = llm.PRICES[MODEL]
    est = (chars / 4 * pin + 200 * len(points) * pout) / 1e6
    print(f"summaries: {len(points)} summaries from {MODEL}, ~{chars // 4} input tokens, estimated ${est:.2f}")
    if est > 5 and not args.yes:
        sys.exit("estimate > $5; rerun with --yes")

    book.summaries = await build_summaries(book, llm.call_structured)
    path.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    print(f"wrote {len(book.summaries)} summaries (upto_para: {[s.upto_para for s in book.summaries]})")
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
