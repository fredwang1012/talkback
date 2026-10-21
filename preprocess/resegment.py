"""resegment (Sonnet 5.5): redo segmentation for quoted paragraphs that are still a single narrator segment.

Usage: uv run --project backend python preprocess/resegment.py --book hp1-sorcerers-stone [--budget 1.5] [--dry]
Candidates: paragraphs with quotation marks and exactly one segment, spoken by the narrator (a failed verbatim check or
an error fell back to the narrator). The stronger model gets every cast id as a possible speaker (minor characters have
voices too). Answers go through the same verbatim validation and quote restoring as segment.py; a paragraph is only
changed if the result has a non-narrator speaker. Stops when --budget dollars have been spent. Rerun chunk_tts.py after.
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
from app.books import save_book  # noqa: E402
from app.enrich.segment import BATCH, BATCH_SYSTEM, build_batch_user, segment_batch  # noqa: E402
from app.schema import Book  # noqa: E402

MODEL = "claude-sonnet-5-5"


_VERBS = (r"(?:said|asked|shouted|whispered|snapped|called|muttered|cried|yelled|added|replied|roared|bellowed|hissed|growled|"
          r"sneered|squeaked|gasped|began|answered|says|continued|told|exclaimed|repeated|breathed|sobbed|laughed|interrupted|"
          r"moaned|barked|screamed|stammered|demanded|suggested|whined|sighed|snarled|croaked|spat)")


def candidates(book: Book, tagged_only: bool = True):
    """Quoted single-narrator paragraphs that name a cast member (others are speech by uncast characters: narrator is right).

    tagged_only: also require a speech tag next to a cast name ("said Hagrid", "Ron muttered"); that is where fallbacks
    hide, and it avoids paying for the many quoted paragraphs that are rightly all narrator.
    """
    names = sorted({a for c in book.characters for a in {c.name, *c.aliases} if len(a) > 2}, key=len, reverse=True)
    alt = "(?:" + "|".join(re.escape(n) for n in names) + ")"
    tag = re.compile(rf"\b{_VERBS}\b[ ,]+(?:\w+ ){{0,2}}{alt}\b|\b{alt}\b[ ,]+(?:\w+ ){{0,3}}{_VERBS}\b")
    name = re.compile(r"\b" + alt + r"\b")
    return [p for p in book.paragraphs
            if any(q in p.text for q in '"“”') and len(p.segments) == 1 and p.segments[0].speaker == "narrator"
            and (tag if tagged_only else name).search(p.text)]


class Budget(Exception):
    pass


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--budget", type=float, default=1.5)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--all-named", action="store_true", help="also paragraphs naming a cast member without a speech tag")
    args = ap.parse_args()
    path = llm.ROOT / "data/books" / args.book / "book.json"
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    work = book.model_copy(deep=True)
    for c in work.characters:
        c.main = True  # every cast member may speak
    todo = candidates(work, not args.all_named)
    todo_idx = {p.idx for p in todo}
    bs = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    chars = sum(len(build_batch_user(work, b)) + len(BATCH_SYSTEM) for b in bs)
    out_tokens = int(sum(len(p.text) for p in todo) / 3.5 * 1.4)
    est = llm.estimate_cost(MODEL, chars, out_tokens)
    print(f"{book.id}: {len(todo)} candidates in {len(bs)} calls, estimated ${est:.2f}")
    if args.dry:
        return
    start = llm.usage["usd"]

    async def call(*a, **kw):
        if llm.usage["usd"] - start > args.budget:
            raise Budget()
        res = await llm.call_structured(*a, **kw)
        for bp in getattr(res, "paragraphs", []):  # whitespace-only segments (a common slip) would fail the tiling check
            bp.segments = [i for i in bp.segments if i.text.strip()]
        if hasattr(res, "segments"):
            res.segments = [i for i in res.segments if i.text.strip()]
        return res

    stats = {"fixed": 0, "kept": 0, "stopped": 0}
    fixed: dict[int, list] = {}

    async def one(b):
        try:
            res = await segment_batch(work, b, call, llm.LLMRefusal, MODEL)
        except Budget:
            stats["stopped"] += len(b)
            return
        for p, (segs, how) in zip(b, res):
            if any(s.speaker != "narrator" for s in segs):
                fixed[p.idx] = segs
            else:
                stats["kept"] += 1

    await asyncio.gather(*(one(b) for b in bs))
    for p in book.paragraphs:
        if p.idx in fixed and p.idx in todo_idx:
            p.segments = fixed[p.idx]
    stats["fixed"] = len(fixed)
    save_book(path, book)
    print(f"{book.id}: {stats}")
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
