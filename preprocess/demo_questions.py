"""Demo questions (Sonnet 5.5): per Harry Potter book, two reader positions (~40% and ~75%, snapped to a chapter start)
with already-read / earlier-book / future (should decline) questions and one character to ask.

Usage: uv run --project backend python preprocess/demo_questions.py [--book hp3-...] [--yes]
Input to the model is ONLY the latest summary at or before the position plus the earlier books' closing summaries
(books.series_recap), so "already read" answers stay consistent with what the reader has seen. For future questions
the model writes the question only, never the spoiler answer (the schema has no answer field for them).
Writes data/demo/questions.json and data/demo/DEMO.md.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.books import series_recap  # noqa: E402
from app.retrieval import latest_summary  # noqa: E402
from app.schema import Book  # noqa: E402

MODEL = "claude-sonnet-5-5"
FRACTIONS = (0.40, 0.75)
EXPECTED_OUT_TOKENS = 900


class QA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    answer: str


class FutureQ(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str


class CharacterQ(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: str
    question: str


class DemoSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    already_read: list[QA]
    earlier_book: list[QA]
    future: list[FutureQ]
    character: CharacterQ


SYSTEM = """You write demo questions for a read-aloud app where listeners ask questions by voice. The app only answers from
what the reader has already read, and declines questions whose answers would spoil what comes later.
You are given a "story so far" note for the reader's current point and, if any, closing notes of the earlier books.
Use ONLY those notes; do not use outside knowledge of the books for answers.
- already_read: exactly 3 natural spoken-style questions, each with a one-line answer stated in the current note.
- earlier_book: exactly 1 question with a one-line answer stated in an earlier book's note; an empty list if there are no earlier books.
- future: exactly 2 questions a curious reader might ask whose true answers lie AFTER this point in the story (for example
  "Who is really behind X?" or "Does Y survive?"). Write only the question. Never reveal or hint at the answer anywhere.
- character: pick one character id from the allowed list who features in the note, and write one fun, in-character question
  for them (about their own life, opinions or the scene), answerable without spoilers."""


def pick_positions(book: Book, fractions=FRACTIONS) -> list[dict]:
    """For each fraction pick the chapter whose start is nearest to fraction*len(paragraphs); never the first chapter."""
    starts = sorted((s for s in book.sections if s.start_para > 0), key=lambda s: s.start_para)
    n = len(book.paragraphs)
    out, used = [], set()
    for f in fractions:
        target = f * n
        sec = min((s for s in starts if s.id not in used), key=lambda s: abs(s.start_para - target), default=None)
        if sec is None:
            break
        used.add(sec.id)
        out.append({"fraction": f, "section_id": sec.id, "chapter": sec.title, "reader_para": sec.start_para})
    return out


def available_characters(book: Book, reader_para: int) -> list:
    """Cast unlock rule (routes/talk.py _resolve_talk): a character is askable once first_para <= reader_para."""
    return [c for c in book.characters if c.first_para <= reader_para]


def build_user(book: Book, reader_para: int, recap: str, characters: list) -> str:
    cast = "\n".join(f"- {c.id}: {c.name}" for c in characters)
    return (f'Book: "{book.title}"\n\nEarlier books (closing notes):\n{recap or "(none: this is the first book)"}\n\n'
            f"Story so far at the reader's position:\n{latest_summary(book, reader_para) or '(nothing read yet)'}\n\n"
            f"Allowed character ids:\n{cast}")


def validate(result: DemoSet, book: Book, characters: list) -> DemoSet:
    ids = {c.id for c in characters}
    if result.character.character_id not in ids:
        raise ValueError(f"{book.id}: character {result.character.character_id!r} not unlocked at this position")
    if book.series_index == 1:
        result.earlier_book = []
    return result


def estimate(books: list[Book]) -> float:
    import llm

    total = 0.0
    for b in books:
        for pos in pick_positions(b):
            chars = available_characters(b, pos["reader_para"])
            user = build_user(b, pos["reader_para"], series_recap(b), chars)
            total += llm.estimate_cost(MODEL, len(SYSTEM) + len(user), EXPECTED_OUT_TOKENS)
    return total


def render_markdown(data: list[dict]) -> str:
    lines = ["## Questions per book", "",
             "Reader position = chapter start; jump there in the reader (paragraph index shown). "
             "Future questions should be declined by the app.", ""]
    for b in data:
        lines += [f"### {b['title']}", ""]
        for p in b["positions"]:
            lines += [f"#### {p['chapter']} (paragraph {p['reader_para']}, about {round(p['fraction'] * 100)}% in)", ""]
            lines.append("Already read:")
            lines += [f"- {q['question']}  \n  Expected: {q['answer']}" for q in p["already_read"]]
            if p["earlier_book"]:
                lines += ["", "Earlier book:"]
                lines += [f"- {q['question']}  \n  Expected: {q['answer']}" for q in p["earlier_book"]]
            lines += ["", "Future (should decline):"]
            lines += [f"- {q['question']}" for q in p["future"]]
            c = p["character"]
            lines += ["", f"Ask {c['name']}: {c['question']}", ""]
    return "\n".join(lines)


DEMO_HEAD = """# Talkback demo

## 90-second script

1. (0:00) Open the library. One line: "Talkback reads books aloud and answers questions by voice, without spoilers."
2. (0:10) Pick Alice's Adventures in Wonderland (ships with the repo; the Harry Potter books need your own PDF). Jump to a chapter listed for it below.
3. (0:20) Press play and let a few sentences read aloud; point out the sentence highlighting following the voice.
4. (0:35) Push-to-talk one "already read" question from the list below. The answer comes from the text so far.
5. (0:50) Push-to-talk one "future" question. The app declines rather than spoil the story.
6. (1:05) Open the character picker, switch to the suggested character and ask their in-character question; the answer is in their voice.
7. (1:20) Optional: switch to the cloned narrator voice with face for a closing line.

"""


def build_docs(data: list[dict]) -> str:
    return DEMO_HEAD + render_markdown(data) + "\n"


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", action="append", help="book id; default all hp* books")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    paths = sorted((llm.ROOT / "data/books").glob("hp*/book.json"))
    if args.book:
        paths = [p for p in paths if p.parent.name in args.book]
    books = sorted((Book.model_validate_json(p.read_text(encoding="utf-8")) for p in paths),
                   key=lambda b: b.series_index or 0)
    est = estimate(books)
    print(f"demo questions: {len(books)} books, {sum(len(pick_positions(b)) for b in books)} calls to {MODEL}, estimated ${est:.2f}")
    if not args.yes:
        sys.exit("rerun with --yes to spend")

    async def one(book: Book, pos: dict) -> dict:
        chars = available_characters(book, pos["reader_para"])
        user = build_user(book, pos["reader_para"], series_recap(book), chars)
        res = validate(await llm.call_structured(MODEL, SYSTEM, user, DemoSet, max_tokens=2000, effort="low"), book, chars)
        char = next(c for c in chars if c.id == res.character.character_id)
        return {**pos, "already_read": [q.model_dump() for q in res.already_read],
                "earlier_book": [q.model_dump() for q in res.earlier_book],
                "future": [{"question": q.question, "expected": "should decline"} for q in res.future],
                "character": {"id": char.id, "name": char.name, "question": res.character.question}}

    out = []
    for book in books:
        positions = await asyncio.gather(*(one(book, p) for p in pick_positions(book)))
        out.append({"book_id": book.id, "title": book.title, "positions": list(positions)})
    (llm.ROOT / "data/demo").mkdir(parents=True, exist_ok=True)
    existing = llm.ROOT / "data/demo/questions.json"
    if existing.exists():  # keep hand-written entries (e.g. the shipped Alice sample); only hp* books are generated
        generated = {e["book_id"] for e in out}
        out = [e for e in json.loads(existing.read_text(encoding="utf-8"))
               if not e["book_id"].startswith("hp") and e["book_id"] not in generated] + out
    (llm.ROOT / "data/demo/questions.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    (llm.ROOT / "data/demo/DEMO.md").write_text(build_docs(out), encoding="utf-8")
    print("wrote data/demo/questions.json and data/demo/DEMO.md")
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
