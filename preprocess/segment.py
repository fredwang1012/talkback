"""segment (Haiku 5.5): split each paragraph into verbatim segments by speaker, each with at most one emotion.

Usage: uv run --project backend python preprocess/segment.py --book hp1-sorcerers-stone [--book ...] [--yes]
Needs the cast (run cast.py first). Paragraphs without quotation marks, and every paragraph of a textbook, skip the
LLM and become a single narrator segment. Every LLM answer is validated: the segment texts must tile the paragraph
exactly (whitespace aside) and use known speakers. Novels are sent BATCH paragraphs per call (one shared cast list and
context); a paragraph whose batch answer fails validation is retried on its own once, then falls back to the narrator.
Rerun chunk_tts.py afterwards to turn segments into TTS chunks.
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.schema import Book, Paragraph, Segment  # noqa: E402
from app.books import save_book  # noqa: E402

MODEL = "claude-haiku-5-5"
from app.enrich import segment as _seg  # noqa: E402  (the shared logic lives in the app)
from app.enrich.segment import (  # noqa: E402,F401
    BATCH, BATCH_SYSTEM, SYSTEM, BatchPara, BatchResult, CallFn, Emotion, SegItem, SegResult, batches,
    build_batch_user, build_user, narrator_only, needs_llm, validate_segments,
)


async def segment_paragraph(book: Book, p: Paragraph, call: CallFn, refusal_exc: type[Exception] = Exception):
    return await _seg.segment_paragraph(book, p, call, refusal_exc, MODEL)


async def segment_batch(book: Book, paras: list[Paragraph], call: CallFn, refusal_exc: type[Exception] = Exception):
    return await _seg.segment_batch(book, paras, call, refusal_exc, MODEL)


# ---------- textbooks: single narrator segment per paragraph, plus the occasional awe/enthusiasm sentence ----------

class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid")
    para: int
    sentence: str
    emotion: Literal["awe", "enthusiasm"]


class PickResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    picks: list[Pick]


EMPHASIS_SYSTEM = """You help a tutor read a science textbook aloud with natural expression.
From the numbered paragraphs, pick the few sentences that are genuinely striking (a surprising scale, a key insight,
an elegant mechanism). At most one sentence per paragraph and at most one pick per five paragraphs.
Copy each sentence exactly as written. emotion is "awe" or "enthusiasm"."""


def apply_emphasis(text: str, sentence: str, emotion: str) -> list[Segment] | None:
    """Split `text` around `sentence` (narrator throughout) so the picked sentence carries the emotion. None if absent."""
    sentence = sentence.strip()
    at = text.find(sentence) if sentence else -1
    if at < 0:
        return None
    parts = [(text[:at].strip(), None), (sentence, emotion), (text[at + len(sentence):].strip(), None)]
    return [Segment(speaker="narrator", text=t, emotion=e) for t, e in parts if t]  # type: ignore[arg-type]


async def emphasize_textbook(book: Book, call: Callable[..., Awaitable[PickResult]]) -> int:
    """Fill segments for every paragraph; returns how many sentences received an emotion."""
    for p in book.paragraphs:
        p.segments = narrator_only(p.text)
    applied = 0
    for section in book.sections:
        paras = [p for p in book.paragraphs if p.section_id == section.id]
        if not paras:
            continue
        user = "\n\n".join(f"[{p.idx}] {p.text}" for p in paras)
        result = await call(MODEL, EMPHASIS_SYSTEM, user, PickResult, max_tokens=1500, effort="low")
        taken: set[int] = set()
        for pick in result.picks[: max(1, len(paras) // 5)]:
            target = next((p for p in paras if p.idx == pick.para), None)
            if target is None or pick.para in taken:
                continue
            segs = apply_emphasis(target.text, pick.sentence, pick.emotion)
            if segs:
                target.segments, applied = segs, applied + 1
                taken.add(pick.para)
    return applied


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, action="append")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--emphasis", action="store_true", help="textbooks: let Haiku pick a few awe/enthusiasm sentences")
    args = ap.parse_args()
    if len(args.book) > 1:
        await segment_novels(llm, args.book, args.yes)
        return
    path = llm.ROOT / "data/books" / args.book[0] / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    if book.kind == "textbook":
        if args.emphasis:
            est = llm.estimate_cost(MODEL, sum(len(p.text) for p in book.paragraphs), 150 * len(book.sections), calls=len(book.sections))
            print(f"segment (textbook emphasis): {len(book.sections)} calls to {MODEL}, estimated ${est:.3f}")
            n = await emphasize_textbook(book, llm.call_structured)
            print(f"{n} sentences got an emotion")
            llm.print_usage()
        else:
            for p in book.paragraphs:
                p.segments = narrator_only(p.text)
            print("textbook: one narrator segment per paragraph (use --emphasis for awe/enthusiasm picks)")
        save_book(path, book)
        print("Next: rerun chunk_tts.py.")
        return
    await segment_novels(llm, args.book, args.yes)


async def segment_novels(llm, book_ids: list[str], yes: bool) -> None:
    paths = [llm.ROOT / "data/books" / b / "book.json" for b in book_ids]
    books = [Book.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    for book in books:
        if book.kind != "novel":
            sys.exit(f"{book.id}: segment textbooks one at a time")
        if not book.characters:
            sys.exit(f"{book.id}: no cast yet, run cast.py first.")
    total = 0.0
    for book in books:
        bs = batches(book)
        chars = sum(len(build_batch_user(book, b)) + len(BATCH_SYSTEM) for b in bs)
        out_tokens = sum(len(p.text) for b in bs for p in b) // 3 + 80 * sum(len(b) for b in bs)
        est = llm.estimate_cost(MODEL, chars, out_tokens)
        total += est
        print(f"segment {book.id}: {sum(len(b) for b in bs)} of {len(book.paragraphs)} paragraphs in {len(bs)} calls, estimated ${est:.2f}")
    print(f"total estimated ${total:.2f}")
    if total > 5 and not yes:
        sys.exit("estimate > $5; rerun with --yes")

    async def one(book: Book, path) -> None:
        results = await asyncio.gather(*(segment_batch(book, b, llm.call_structured, llm.LLMRefusal) for b in batches(book)))
        done = {p.idx: r for b, rs in zip(batches(book), results) for p, r in zip(b, rs)}
        counts: dict[str, int] = {}
        for p in book.paragraphs:
            segs, how = done.get(p.idx) or (narrator_only(p.text), "skipped")
            p.segments = segs
            counts[how] = counts.get(how, 0) + 1
        save_book(path, book)
        llm_total = sum(counts.get(k, 0) for k in ("llm", "retry", "fallback"))
        ok_rate = (counts.get("llm", 0) + counts.get("retry", 0)) / llm_total if llm_total else 1.0
        print(f"{book.id}: {counts}; verbatim check passed for {ok_rate:.1%} of LLM paragraphs (target >= 95%)")

    for book, path in zip(books, paths):  # one book at a time keeps memory and rate limits sane
        await one(book, path)
    llm.print_usage()
    print("Next: rerun chunk_tts.py to rebuild TTS chunks from the segments.")


if __name__ == "__main__":
    asyncio.run(main())
