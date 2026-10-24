"""ingest: data/raw/<name>.txt + <name>.meta.yaml -> data/books/<book_id>/book.json (paragraphs + sections only).

Usage: uv run --project backend python preprocess/ingest.py --book scandal-in-bohemia
Idempotent. Running later stages (cast, segment, ...) fills in the rest of book.json; re-running ingest
keeps existing characters/narrator/summaries/segments only if paragraph text is unchanged.

Meta keys: book_id, title, author, kind (novel|textbook), license, attribution, and optionally
start_marker / end_marker (text boundaries), heading_regex (a block that matches starts a new section; default is a
roman numeral line) and skip_regex (blocks that match are dropped: figure credits, link lists, exercises).
"""
import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.parsing import BLANK_LINE, CURLY, ROMAN, normalize, parse  # noqa: E402,F401
from app.schema import Book  # noqa: E402

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    args = ap.parse_args()

    metas = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in (ROOT / "data/raw").glob("*.meta.yaml")]
    meta = next((m for m in metas if m["book_id"] == args.book), None)
    if meta is None:
        sys.exit(f"No data/raw/*.meta.yaml with book_id={args.book}")
    txt = next((p for p in (ROOT / "data/raw").glob("*.txt") if (p.with_suffix(".meta.yaml").exists()
                and yaml.safe_load(p.with_suffix(".meta.yaml").read_text(encoding="utf-8"))["book_id"] == args.book)), None)
    if txt is None:
        sys.exit(f"No text file for {args.book}: put the text in data/raw/<name>.txt next to its .meta.yaml")
    sections, paragraphs = parse(txt.read_text(encoding="utf-8"), meta.get("start_marker"), meta.get("end_marker"),
                                 meta.get("heading_regex"), meta.get("skip_regex"))

    out = ROOT / "data/books" / args.book / "book.json"
    old = Book.model_validate_json(out.read_text(encoding="utf-8")) if out.exists() else None
    book = Book(id=meta["book_id"], title=meta["title"], author=meta["author"], kind=meta["kind"],
                license=meta["license"], attribution=meta.get("attribution"),
                sections=sections, paragraphs=paragraphs)
    if old and [p.text for p in old.paragraphs] == [p.text for p in paragraphs]:
        # text unchanged: keep downstream work (cast, segments, summaries) but always refresh metadata
        book = old.model_copy(update={
            "sections": sections, "title": book.title, "author": book.author, "kind": book.kind,
            "license": book.license, "attribution": book.attribution})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(book.model_dump_json(indent=1), encoding="utf-8")
    chars = sum(len(p.text) for p in paragraphs)
    print(f"{args.book}: {len(sections)} sections, {len(paragraphs)} paragraphs, {chars} chars -> {out}")


if __name__ == "__main__":
    main()
