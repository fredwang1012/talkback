"""repersona: rewrite the main characters' personas from ONLY their earliest speaking lines in the series (one persona per
character, copied to every book they appear in). Same rules as cast.py SERIES_SYSTEM: manner/voice only, no plot facts.

Usage: LLM_PROVIDER=gemini uv run --project backend python preprocess/repersona.py [--model claude-sonnet-5-5]
"""
import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
from app.books import save_book  # noqa: E402
from app.schema import Book  # noqa: E402

MAIN = ["harry", "ron", "hermione", "dumbledore", "hagrid", "snape", "dobby", "voldemort"]
LINES = 14


class P(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    persona: str


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    personas: list[P]


SYSTEM = """You write roleplay personas for characters of a book series, for an actor who will speak as them.
For each character you get their EARLIEST spoken lines in the series.

Rules:
- Each persona: at most 4 sentences of direction to a roleplay actor ("Speaks ...", not "I speak"). Be specific to this character: how they refer to themselves and others (even third person), signature words, sentence shape, dialect or non-standard grammar, what happens to their speech when nervous or angry, and one or two short quoted phrases from the lines shown. No bare adjectives like "friendly" or "wise" unless tied to something audible.
- Describe them ONLY as they come across in the lines shown. Manner and voice only.
- No plot facts, no secrets, no later revelations, true allegiances, deaths, or anything from later in the series, even if you know it.
- Return one entry per character id given."""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    args = ap.parse_args()
    paths = sorted((llm.ROOT / "data/books").glob("hp*/book.json"))
    books = sorted((Book.model_validate_json(p.read_text(encoding="utf-8")) for p in paths), key=lambda b: b.series_index or 0)
    rows = []
    for cid in MAIN:
        for b in books:
            if not any(c.id == cid for c in b.characters):
                continue
            lines = []
            for p in b.paragraphs:
                for s in p.segments:
                    if s.speaker == cid and len(s.text) >= 40 and len(lines) < LINES:
                        lines.append(s.text[:300])
            if lines:
                rows.append(f"## {cid} (earliest lines, from \"{b.title}\")\n" + "\n".join(f"- {t}" for t in lines))
                break
    res = await llm.call_structured(args.model, SYSTEM, "\n\n".join(rows), Result, max_tokens=4000, effort="medium")
    persona = {x.id: x.persona.strip() for x in res.personas if x.id in MAIN}
    for path, _ in zip(paths, paths):
        b = Book.model_validate_json(path.read_text(encoding="utf-8"))
        for c in b.characters:
            if c.id in persona:
                c.persona = persona[c.id]
        save_book(path, b)
    for k, v in persona.items():
        print(f"- {k}: {v}")
    llm.print_usage()


if __name__ == "__main__":
    asyncio.run(main())
