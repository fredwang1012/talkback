"""Chapter detection in uploaded textbooks and novels (font-size/bold signals plus the heading regex)."""
import pymupdf as fitz

from app import ingest_upload
from app.ingest_upload import HEADING_REGEX, pdf_to_text
from app.parsing import parse

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")


def make(pages):
    """pages: list of lists of (kind, text); kind is 'h' (20pt bold), 'h2' (15pt bold), 'p' (11pt paragraph),
    'line' (11pt single line), 'big' (20pt regular)."""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page()
        y = 80
        for kind, text in items:
            if kind == "p":
                page.insert_textbox(fitz.Rect(72, y, 500, y + 100), text, fontsize=11)
                y += 110
            else:
                size, font = {"h": (20, "hebo"), "h2": (15, "hebo"), "line": (11, "helv"), "big": (20, "helv")}[kind]
                page.insert_text((72, y + size), text, fontsize=size, fontname=font)
                y += size + 22
    return doc.tobytes()


def titles(data):
    sections, paras = parse(pdf_to_text(data), None, None, HEADING_REGEX)
    return [s.title for s in sections], paras


def test_regex_accepts_textbook_headings():
    import re
    rx = re.compile(HEADING_REGEX)
    for good in ("Chapter 8 Photosynthesis", "8 Photosynthesis", "8.1 Overview of Photosynthesis", "CHAPTER 8",
                 "Unit 2", "UNIT 2: Genetics", "Chapter I", "CHAPTER ONE", "IV."):
        assert rx.match(good), good
    for bad in ("1. Define osmosis.", "8.1", "The cell is small.", "Chapter 8 is where we start, and then some"):
        assert not rx.match(bad), bad


def test_decimal_heading_stands_alone():
    t, paras = titles(make([[("line", "8.1 Overview of Photosynthesis"), ("p", BODY)]]))
    assert t == ["8.1 Overview of Photosynthesis"] and paras[0].text == BODY


def test_two_line_chapter_heading_is_merged():
    t, paras = titles(make([[("h", "CHAPTER 8"), ("h", "Photosynthesis"), ("p", BODY)]]))
    assert t == ["CHAPTER 8 Photosynthesis"] and len(paras) == 1


def test_large_bold_heading_without_number_is_a_section():
    data = make([[("h", "Photosynthesis"), ("p", BODY), ("p", BODY2)], [("p", BODY2), ("h2", "Cell Respiration"),
                                                                         ("p", BODY)]])
    t, paras = titles(data)
    assert t == ["Photosynthesis", "Cell Respiration"]
    assert all("\x01" not in s for s in t)


def test_body_text_is_not_a_heading():
    t, paras = titles(make([[("p", BODY), ("p", BODY2)]]))
    assert t == [""] and len(paras) == 2


def test_toc_is_not_turned_into_sections():
    toc = [("line", f"Chapter {i} Topic number {i} ........ {i * 10}") for i in range(1, 7)]
    t, paras = titles(make([[("h", "Contents")] + toc, [("h", "Chapter 1 Cells"), ("p", BODY)],
                            [("h", "Chapter 2 Energy"), ("p", BODY2)]]))
    assert t == ["Chapter 1 Cells", "Chapter 2 Energy"]


def test_toc_without_dot_leaders_is_dropped_too():
    toc = [("p", BODY)] + [("line", f"Chapter {i}") for i in range(1, 6)] + [("p", BODY2)]
    t, paras = titles(make([toc, [("line", "Chapter 1"), ("p", BODY)], [("line", "Chapter 2"), ("p", BODY2)]]))
    assert t == ["", "Chapter 1", "Chapter 2"]  # the intro text, then the real chapters


def test_numbered_list_items_are_not_headings():
    t, paras = titles(make([[("h", "Chapter 3 Review"), ("line", "1. Define osmosis."),
                             ("line", "2. Explain diffusion."), ("p", BODY)]]))
    assert t == ["Chapter 3 Review"]


def test_novel_headings_still_work():
    pages = [[("line", "Chapter I"), ("p", BODY)], [("line", "CHAPTER TWO"), ("p", BODY2)],
             [("line", "Chapter III"), ("p", BODY)]]
    t, _ = titles(make(pages))
    assert t == ["Chapter I", "CHAPTER TWO", "Chapter III"]


def test_txt_two_line_chapter_heading_merges():
    sections, paras = parse("CHAPTER 8\nPhotosynthesis\n\nBody text here that is long enough.\n", None, None,
                            ingest_upload.HEADING_REGEX)
    assert [s.title for s in sections] == ["CHAPTER 8 Photosynthesis"] and len(paras) == 1


def test_dot_leader_check_is_linear_on_hostile_lines():
    import time
    from app.ingest_upload import _has_dot_leader
    t = time.perf_counter()
    assert not _has_dot_leader(". " * 100_000)
    assert not _has_dot_leader(". " * 100_000 + "x")
    assert time.perf_counter() - t < 0.5
    assert _has_dot_leader("Chapter 1 Cells ........ 12") and _has_dot_leader("Intro . . . . 3")
    assert not _has_dot_leader("Plain sentence 12")
