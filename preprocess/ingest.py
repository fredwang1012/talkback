"""ingest: data/raw/<name>.txt + <name>.meta.yaml -> data/books/<book_id>/book.json (paragraphs + sections only).

Usage: uv run --project backend python preprocess/ingest.py --book scandal-in-bohemia
Idempotent. Running later stages (cast, segment, ...) fills in the rest of book.json; re-running ingest
keeps existing characters/narrator/summaries/segments only if paragraph text is unchanged.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.schema import Book, Paragraph, Section  # noqa: E402

ROMAN = re.compile(r"^[IVXLC]+\.$")
CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def normalize(text: str) -> str:
    text = text.translate(CURLY).replace("﻿", "")
    text = re.sub(r"_([^_\n]+)_", r"\1", text)  # Gutenberg italics markers
    return re.sub(r"\s+", " ", text).strip()


def parse(raw: str, start_marker: str, end_marker: str | None):
    raw = raw.replace("\r\n", "\n")
    # skip the Gutenberg header/footer and any table of contents before the real body
    body_start = raw.index("*** START OF")
    start = raw.index(start_marker, raw.index("\n", body_start))
    # first occurrence after the TOC: take the LAST occurrence before the first prose paragraph
    # is overkill; the TOC lists titles without the exact "I. TITLE" heading twice, so use the heading
    occurrences = [m.start() for m in re.finditer(re.escape(start_marker), raw[body_start:])]
    start = body_start + occurrences[-1] if len(occurrences) > 1 else start
    end = raw.index(end_marker, start) if end_marker else raw.index("*** END OF", start)
    body = raw[raw.index("\n", start) : end]

    sections: list[Section] = []
    paragraphs: list[Paragraph] = []
    current = None
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        if ROMAN.match(block):
            current = Section(id=f"s{len(sections) + 1}", title=block, start_para=len(paragraphs))
            sections.append(current)
            continue
        if current is None:
            current = Section(id="s1", title="", start_para=len(paragraphs))
            sections.append(current)
        text = normalize(block)
        if text:
            paragraphs.append(Paragraph(idx=len(paragraphs), section_id=current.id, text=text))
    return sections, paragraphs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    args = ap.parse_args()

    metas = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in (ROOT / "data/raw").glob("*.meta.yaml")]
    meta = next((m for m in metas if m["book_id"] == args.book), None)
    if meta is None:
        sys.exit(f"No data/raw/*.meta.yaml with book_id={args.book}")
    txt = next(p for p in (ROOT / "data/raw").glob("*.txt") if (p.with_suffix(".meta.yaml").exists()
               and yaml.safe_load(p.with_suffix(".meta.yaml").read_text(encoding="utf-8"))["book_id"] == args.book))
    sections, paragraphs = parse(txt.read_text(encoding="utf-8"), meta["start_marker"], meta.get("end_marker"))

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
