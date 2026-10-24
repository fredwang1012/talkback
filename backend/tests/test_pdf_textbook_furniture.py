"""Textbook front matter and page furniture: contents without dot leaders, title page, copyright, running headers."""
import pymupdf as fitz

from app.ingest_upload import HEADING_REGEX, pdf_to_text
from app.parsing import parse

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")


def make(pages):
    """pages: lists of (kind, text[, x]); kinds: 'p' paragraph, 'line' 10pt, 'h' 20pt bold, 'big' 24pt,
    'small' 9pt italic header line."""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page()
        y = 60
        for item in items:
            kind, text = item[0], item[1]
            x = item[2] if len(item) > 2 else 72
            if kind == "p":
                page.insert_textbox(fitz.Rect(72, y, 500, y + 100), text, fontsize=11)
                y += 110
                continue
            size, font = {"h": (20, "hebo"), "big": (24, "helv"), "line": (11, "helv"), "small": (9, "heit")}[kind]
            page.insert_text((x, y + size), text, fontsize=size, fontname=font)
            y += size + (6 if kind in ("line", "small") else 22)
    return doc.tobytes()


def parsed(data):
    sections, paras = parse(pdf_to_text(data), None, None, HEADING_REGEX)
    return [s.title for s in sections], [p.text for p in paras]


def test_toc_without_dot_leaders_single_lines_is_not_read():
    entries = [f"{i}.{j} Topic number {i}{j} here {i * 10 + j}" for i in range(1, 4) for j in range(1, 5)]
    pages = [[("p", BODY)], [("small", "Contents")] + [("line", e) for e in entries],
             [("h", "Chapter 1 Cells"), ("p", BODY2)]]
    titles, paras = parsed(make(pages))
    assert not any("Topic number" in p for p in paras) and not any("Topic number" in t for t in titles)
    assert "Chapter 1 Cells" in titles and BODY2 in paras


def test_toc_with_fragmented_number_title_page_lines_is_not_read():
    items = []
    for i in range(1, 11):
        items += [("line", f"{i}.1", 72), ("line", f"Section title {i}", 120), ("line", str(i * 5), 450)]
    pages = [[("p", BODY)], items, [("h", "Chapter 1 Cells"), ("p", BODY2)]]
    titles, paras = parsed(make(pages))
    assert not any("Section title" in p for p in paras) and not any("Section title" in t for t in titles)


def test_title_page_lines_become_one_section_and_copyright_is_no_heading():
    pages = [[("big", "An Introduction"), ("big", "to Information"), ("big", "Retrieval"),
              ("big", "(c) 2009 Cambridge University Press")],
             [("h", "Chapter 1 Cells"), ("p", BODY2)]]
    titles, _ = parsed(make(pages))
    assert titles[0] == "An Introduction to Information Retrieval"
    assert not any("Cambridge" in t for t in titles)
    assert titles.count("An Introduction to Information Retrieval") == 1


def test_isbn_and_copyright_text_never_heading():
    pages = [[("h", "Copyright 2009 Some Press"), ("h", "ISBN 978-0-12345-678-9"), ("p", BODY)]]
    titles, _ = parsed(make(pages))
    assert not any("ISBN" in t or "Copyright" in t for t in titles)


def test_running_header_with_page_number_does_not_duplicate_heading():
    pages = [[("line", "2.4 Positional postings and phrase queries 39"),
              ("line", "2.4 Positional postings and phrase queries"), ("p", BODY)],
             [("line", "2.4 Positional postings and phrase queries 40"), ("p", BODY2)]]
    titles, paras = parsed(make(pages))
    assert titles == ["2.4 Positional postings and phrase queries"]
    assert not any("queries 4" in p or "queries 3" in p for p in paras)


def test_real_chapter_heading_with_number_is_kept():
    titles, _ = parsed(make([[("line", "12 The Wedding"), ("p", BODY)], [("p", BODY2)]]))
    assert titles == ["12 The Wedding"]
