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
from app.schema import EMOTIONS, Book, Paragraph, Segment  # noqa: E402
from app.books import save_book  # noqa: E402

MODEL = "claude-haiku-5-5"
Emotion = Literal[
    "elation", "amusement", "enthusiasm", "determination", "pride", "contentment", "affection", "relief",
    "contemplation", "confusion", "surprise", "awe", "longing", "anger", "fear", "disgust", "bitterness",
    "sadness", "shame", "helplessness",
]
assert set(Emotion.__args__) == set(EMOTIONS)  # type: ignore[attr-defined]  # keep in step with the app schema


class SegItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speaker: str
    text: str
    emotion: Emotion | None


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
- Use only speaker ids from the cast list, plus "narrator"."""

CallFn = Callable[..., Awaitable[SegResult]]


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_segments(paragraph: str, items: list[SegItem], speakers: set[str]) -> list[Segment] | None:
    """Return Segment objects if `items` tile `paragraph` exactly and use known speakers, else None."""
    if not items:
        return None
    cursor = 0
    out: list[Segment] = []
    for it in items:
        text = it.text.strip()
        if not text:
            return None
        at = paragraph.find(text, cursor)
        if at < 0 or paragraph[cursor:at].strip():  # text missing, reordered, or something skipped between segments
            return None
        cursor = at + len(text)
        speaker = it.speaker if it.speaker in speakers else "narrator"
        out.append(Segment(speaker=speaker, text=text, emotion=it.emotion))
    if paragraph[cursor:].strip():
        return None
    return out


def narrator_only(paragraph: str) -> list[Segment]:
    return [Segment(speaker="narrator", text=paragraph.strip(), emotion=None)]


def needs_llm(book: Book, p: Paragraph) -> bool:
    return book.kind == "novel" and bool(book.characters) and '"' in p.text


def build_user(book: Book, p: Paragraph) -> str:
    cast = "\n".join(f'- id "{c.id}": {c.name} (also called: {", ".join(c.aliases)})' for c in book.characters)
    before = "\n\n".join(q.text for q in book.paragraphs[max(0, p.idx - 3):p.idx]) or "(start of the story)"
    narrator = book.narrator.is_character if book.narrator else None
    note = f' The first-person narrator is the character "{narrator}"; their narration stays "narrator".' if narrator else ""
    return f"Cast:\n{cast}{note}\n\nPrevious paragraphs (context only):\n{before}\n\nParagraph to split:\n{p.text}"


async def segment_paragraph(book: Book, p: Paragraph, call: CallFn, refusal_exc: type[Exception] = Exception) -> tuple[list[Segment], str]:
    """Returns (segments, how) where how is 'skipped', 'llm', 'retry' or 'fallback'."""
    if not needs_llm(book, p):
        return narrator_only(p.text), "skipped"
    speakers = {c.id for c in book.characters} | {"narrator"}
    user = build_user(book, p)
    for attempt in range(2):
        try:
            result = await call(MODEL, SYSTEM, user, SegResult, max_tokens=3000, effort="low")
        except refusal_exc:
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
    cast = "\n".join(f'- id "{c.id}": {c.name} (also called: {", ".join(c.aliases)})' for c in book.characters)
    first = paras[0].idx
    before = "\n\n".join(q.text for q in book.paragraphs[max(0, first - 2):first]) or "(start of the story)"
    narrator = book.narrator.is_character if book.narrator else None
    note = f' The first-person narrator is the character "{narrator}"; their narration stays "narrator".' if narrator else ""
    todo = "\n\n".join(f"[{p.idx}] {p.text}" for p in paras)
    return f"Cast:\n{cast}{note}\n\nPrevious paragraphs (context only):\n{before}\n\nParagraphs to split:\n{todo}"


async def segment_batch(book: Book, paras: list[Paragraph], call: CallFn,
                        refusal_exc: type[Exception] = Exception) -> list[tuple[list[Segment], str]]:
    """Segments for each paragraph in `paras` (all need the LLM), in order. Failed ones go through segment_paragraph."""
    speakers = {c.id for c in book.characters} | {"narrator"}
    answers: dict[int, list[SegItem]] = {}
    try:
        result = await call(MODEL, BATCH_SYSTEM, build_batch_user(book, paras), BatchResult,
                            max_tokens=4000 + 600 * len(paras), effort="low")
        answers = {bp.para: bp.segments for bp in result.paragraphs}
    except refusal_exc:
        pass
    out = []
    for p in paras:
        ok = validate_segments(p.text, answers.get(p.idx, []), speakers)
        out.append((ok, "llm") if ok else await segment_paragraph(book, p, call, refusal_exc))
    return out


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


def batches(book: Book) -> list[list[Paragraph]]:
    """Consecutive runs of paragraphs that need the LLM, BATCH at a time."""
    todo = [p for p in book.paragraphs if needs_llm(book, p)]
    return [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]


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
