"""ingest_series: split a multi-book PDF (e.g. the Harry Potter collection) into one book per novel, one section per chapter.

Usage: uv run --project backend python preprocess/ingest_series.py --pdf data/uploads/harry-potter-collection.pdf [--only 1]
Writes data/books/<id>/book.json (narrator-only, with TTS chunks) and the retrieval chunks in data/books/<id>/index/.
No network, no cost. Next: cast.py, segment.py, chunk_tts.py, summaries.py, then scripts/index_vertex.py.

Structure comes from the PDF's table of contents: level-1 entries "Harry Potter and ..." are books, their level-2
entries "Chapter N - Title" / "Epilogue" are chapters, and every other entry (title pages, "Copyright page") only
marks where the previous chapter ends. On a chapter's first page the bold heading lines ("CHAPTER ONE", the title)
are dropped and the large drop-cap letter is joined back onto the first line of text.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import retrieval  # noqa: E402
from app.ingest_upload import _LIGATURES, _PAGE_NUMBER, Line, _cap_paragraphs, merge_lines  # noqa: E402
from app.parsing import normalize  # noqa: E402
from app.schema import Book, Narrator, Paragraph, Section  # noqa: E402
from app.ttschunks import chunk_paragraph  # noqa: E402

SERIES = "harry-potter"
AUTHOR = "J.K. Rowling"
LICENSE = "Personal copy, for private use only"
NARRATOR_VOICE = "nora"
DROP_CAP_SIZE = 30.0  # a 1-2 character line larger than this (pt) is a drop cap (titles are ~20 pt)
IDS = {  # TOC title (after "Harry Potter and the ") -> book id
    "Sorcerer's Stone": "hp1-sorcerers-stone",
    "Chamber of Secrets": "hp2-chamber-of-secrets",
    "Prisoner of Azkaban": "hp3-prisoner-of-azkaban",
    "Goblet of Fire": "hp4-goblet-of-fire",
    "Order of the Phoenix": "hp5-order-of-the-phoenix",
    "Half-Blood Prince": "hp6-half-blood-prince",
    "Deathly Hallows": "hp7-deathly-hallows",
}
_CHAPTER = re.compile(r"^Chapter\s+(\d+)\s*-\s*(.+)$")


def clean_title(t: str) -> str:
    return normalize(t.replace("�", "'"))


def plan_books(toc: list[list]) -> list[dict]:
    """[{title, id, index, chapters: [(section_title, first_page, last_page)]}] with 1-based inclusive pages."""
    books: list[dict] = []
    for i, (level, title, page) in enumerate(toc):
        title = clean_title(title)
        nxt = toc[i + 1][2] - 1 if i + 1 < len(toc) else None
        if level == 1 and title.startswith("Harry Potter and the "):
            short = title.removeprefix("Harry Potter and the ")
            books.append({"title": title, "id": IDS.get(short), "index": len(books) + 1, "chapters": []})
        elif level == 2 and books and (m := _CHAPTER.match(title) or (title == "Epilogue" and title)):
            section = f"Chapter {m.group(1)}: {m.group(2)}" if not isinstance(m, str) else "Epilogue"
            books[-1]["chapters"].append((section, page, nxt if nxt is not None else page))
    return [b for b in books if b["id"]]


def page_lines(page, first: bool) -> list[tuple]:
    """(text, x0, y0, x1, y1) per text line; on a chapter's first page, headings dropped and the drop cap re-joined."""
    out: list[tuple] = []
    drop_cap = ""
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            raw = "".join(s["text"] for s in spans).strip()
            if first and len(raw) <= 2 and all(s["size"] > DROP_CAP_SIZE for s in spans):
                drop_cap += raw
                continue
            if first and not out and all("Bold" in s["font"] for s in spans):
                continue  # "CHAPTER ONE", the chapter title (bold, sometimes bold italic)
            t = "".join(s["text"] for s in ln["spans"]).translate(_LIGATURES).strip()
            if _PAGE_NUMBER.match(t):
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if drop_cap and not out:
                t, drop_cap = drop_cap + t, ""
            out.append((t, x0, y0, x1, y1))
    return out


def chapter_paragraphs(doc, first_page: int, last_page: int) -> list[str]:
    lines: list[Line] = []
    for pno in range(first_page - 1, last_page):
        flat = page_lines(doc[pno], first=not lines)  # heading pages (a divider page, then the opening) until text starts
        if not flat:
            continue
        x0s, x1s = sorted(ln[1] for ln in flat), sorted(ln[3] for ln in flat)
        left, right = x0s[len(x0s) // 5], x1s[len(x1s) * 9 // 10]
        lines += [Line(t, x0, x1, y0, y1, left, right, pno, False) for t, x0, y0, x1, y1 in flat]
    return [p for p in (normalize(x) for x in merge_lines(lines)) if p]


def build(doc, spec: dict) -> Book:
    sections: list[Section] = []
    paragraphs: list[Paragraph] = []
    for n, (title, first, last) in enumerate(spec["chapters"], start=1):
        sid = f"s{n}"
        sections.append(Section(id=sid, title=title, start_para=len(paragraphs)))
        for text in chapter_paragraphs(doc, first, last):
            paragraphs.append(Paragraph(idx=len(paragraphs), section_id=sid, text=text))
    paragraphs = _cap_paragraphs(sections, paragraphs)
    book = Book(id=spec["id"], title=spec["title"], author=AUTHOR, kind="novel", license=LICENSE,
                sections=sections, paragraphs=paragraphs, narrator=Narrator(voice=NARRATOR_VOICE),
                series=SERIES, series_index=spec["index"])
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
    return book


def main() -> None:
    import pymupdf

    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--only", type=int, action="append", help="series index to ingest (repeatable); default all")
    args = ap.parse_args()
    doc = pymupdf.open(args.pdf)
    specs = plan_books(doc.get_toc())
    if len(specs) != len(IDS):
        sys.exit(f"expected {len(IDS)} books in the table of contents, found {[s['title'] for s in specs]}")
    for spec in specs:
        if args.only and spec["index"] not in args.only:
            continue
        book = build(doc, spec)
        d = ROOT / "data" / "books" / book.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "book.json").write_text(book.model_dump_json(indent=1), encoding="utf-8")
        retrieval.save_index(d / "index", retrieval.make_chunks(book), None)
        words = sum(len(p.text.split()) for p in book.paragraphs)
        print(f"{book.id}: {len(book.sections)} chapters, {len(book.paragraphs)} paragraphs, {words} words, "
              f"{sum(len(p.tts_chunks) for p in book.paragraphs)} TTS chunks")


if __name__ == "__main__":
    main()
