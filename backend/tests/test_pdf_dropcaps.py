"""Novels set with decorative drop caps and unnumbered chapters (an epigraph, then a big initial letter).

The fixtures are synthetic PDFs laid out like such a novel: 12pt body text, a 9pt italic epigraph with its
attribution, and a 40pt initial letter (optionally after an opening quote) beside the first two lines.
"""
import pymupdf as fitz

from app import ingest_upload
from app.ingest_upload import HEADING_REGEX, build_book, pdf_to_text
from app.parsing import parse

BODY = 12
LEAD = 15          # baseline-to-baseline distance of body lines
LEFT = 72
WORDS = ("the sand moved under a pale sky while the water sellers walked slowly toward the old market of the "
         "city and nobody spoke of the storm that was coming over the ridge before the night fell again").split()


def _lines(n: int, seed: int) -> list[str]:
    """n lines of filler prose, about 60 characters each; the last one ends a sentence."""
    out, i = [], seed
    for k in range(n):
        words = []
        while len(" ".join(words)) < 60:
            words.append(WORDS[i % len(WORDS)])
            i += 1
        out.append(" ".join(words) + ("." if k == n - 1 else ""))
    return out


def _paragraph(page, y: float, n: int, seed: int) -> float:
    """A body paragraph: an indented first line, then full lines, the last one short. Returns the next baseline."""
    for k, text in enumerate(_lines(n, seed)):
        if k == n - 1:
            text = " ".join(text.split()[:4]) + "."
        if k == 0:
            text = text[0].upper() + text[1:]
        page.insert_text((LEFT + (18 if k == 0 else 0), y), text, fontsize=BODY)
        y += LEAD
    return y


def _chapter(page, y: float, cap: str, rest: str, *, epigraph: bool = True, seed: int = 0) -> float:
    """Epigraph + attribution (9pt), then a drop cap `cap` whose word continues with `rest`. Returns next baseline."""
    if epigraph:
        # each chapter's own words: a line repeated at the top of every page would be a running header
        for text in (f"Every road through the desert of {WORDS[seed]} is a road through the self,",
                     f"and the self is the hardest country to cross, {WORDS[seed + 1]} said."):
            page.insert_text((LEFT + 40, y), text, fontsize=9, fontname="heit")
            y += 11
        y += 4
        page.insert_text((LEFT + 140, y), f"-from Sayings of the Old Guide, {WORDS[seed + 2]}", fontsize=9)
        y += 30
    cap_w = fitz.get_text_length(cap, fontname="helv", fontsize=40)
    first = _lines(5, seed)
    beside = LEFT + cap_w + 3
    page.insert_text((LEFT, y + LEAD), cap, fontsize=40)   # the cap's baseline sits on the second line's
    page.insert_text((beside, y), rest + " " + first[0], fontsize=BODY)
    page.insert_text((beside, y + LEAD), first[1], fontsize=BODY)
    y += 2 * LEAD
    for text in first[2:4]:
        page.insert_text((LEFT, y), text, fontsize=BODY)
        y += LEAD
    page.insert_text((LEFT, y), "so it ended.", fontsize=BODY)
    y += LEAD
    for k in range(3):
        y = _paragraph(page, y, 4, seed + 7 * k)
    return y


def novel(pages) -> bytes:
    """pages: list of lists of items; an item is ("chapter", cap, rest, kwargs), ("part", text) for a big centred
    part heading, ("caps", text) for a body-size all-caps centred line, ("byline", text) for a small centred line,
    ("text", n) for n body paragraphs, or ("line", text, size, x)."""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page()
        y = 80.0
        for item in items:
            if item[0] == "chapter":
                y = _chapter(page, y, item[1], item[2], **(item[3] if len(item) > 3 else {}))
            elif item[0] == "part":
                page.insert_text((230, y + 10), item[1], fontsize=22, fontname="hebo")
                y += 40
            elif item[0] == "caps":
                page.insert_text((250, y), item[1], fontsize=BODY)
                y += 16
            elif item[0] == "byline":
                page.insert_text((255, y), item[1], fontsize=9)
                y += 40
            elif item[0] == "text":
                for k in range(item[1]):
                    y = _paragraph(page, y, 5, 3 * k)
            elif item[0] == "line":
                page.insert_text((item[3], y), item[1], fontsize=item[2])
                y += item[2] + 6
    return doc.tobytes()


def sections_of(data: bytes):
    return parse(pdf_to_text(data), None, None, HEADING_REGEX)


def chapter_pages(n: int, start: int = 0) -> list[list[tuple]]:
    caps = [("D", "une"), ('"Y', "ou"), ("T", "he")]  # a base-14 font cannot draw a curly quote
    return [[("chapter", *caps[(start + k) % 3], {"seed": k})] for k in range(n)]


def test_drop_cap_is_rejoined_to_its_word_and_is_never_a_heading():
    sections, paras = sections_of(novel(chapter_pages(3)))
    titles = [s.title for s in sections]
    assert not any(t in ('"Y', "D", "T", "Y") for t in titles), titles
    texts = [p.text for p in paras]
    assert any(t.startswith("Dune the sand") for t in texts), texts[:6]
    assert any(t.startswith('"You ') for t in texts), texts[:6]
    assert not any(t.startswith(("D une", "une ", "ou ", '"Y ou')) or " D une " in t for t in texts)


def test_lines_beside_a_drop_cap_continue_its_paragraph():
    _sections, paras = sections_of(novel(chapter_pages(3)))
    first = next(p.text for p in paras if p.text.startswith("Dune"))
    assert first.endswith("so it ended."), first


def test_each_drop_cap_starts_a_numbered_chapter_with_its_epigraph():
    sections, paras = sections_of(novel([[("text", 2)]] + chapter_pages(3)))
    assert [s.title for s in sections] == ["", "Chapter 1", "Chapter 2", "Chapter 3"]
    for s in sections[1:]:
        # the small epigraph is kept (it is part of the chapter) and opens it
        assert paras[s.start_para].text.startswith("Every road through the desert"), paras[s.start_para].text
        assert any(p.text.startswith("-from Sayings") for p in paras[s.start_para:s.start_para + 3])


def test_part_headings_reset_numbering_and_hold_their_chapters():
    pages = [[("part", "BOOK ONE")]] + chapter_pages(2) + [[("part", "BOOK TWO")]] + chapter_pages(2, 1)
    sections, _paras = sections_of(novel(pages))
    assert [s.title for s in sections] == ["BOOK ONE", "Chapter 1", "Chapter 2", "BOOK TWO", "Chapter 1",
                                           "Chapter 2"]
    book = build_book("b", "T", "A", "novel", pdf_to_text(novel(pages)))
    assert [s.level for s in book.sections] == [1, 2, 2, 1, 2, 2]


def test_novel_without_parts_keeps_a_flat_chapter_list():
    book = build_book("b", "T", "A", "novel", pdf_to_text(novel(chapter_pages(3))))
    assert [s.level for s in book.sections] == [1, 1, 1]


def test_heading_above_a_drop_cap_is_kept_and_no_chapter_is_added_for_it():
    pages = [[("caps", "INTRODUCTION"), ("byline", "BY A FRIEND"),
              ("chapter", "D", "une", {"epigraph": False})]] + chapter_pages(3, 1)
    sections, paras = sections_of(novel(pages))
    assert [s.title for s in sections] == ["INTRODUCTION", "Chapter 1", "Chapter 2", "Chapter 3"]
    intro = [p.text for p in paras[:sections[1].start_para]]
    assert any(t.startswith("Dune the sand") for t in intro), intro


def test_numbered_chapter_heading_with_a_drop_cap_gets_no_second_section():
    pages = [[("part", "Chapter One"), ("chapter", "H", "arry", {"epigraph": False})],
             [("part", "Chapter Two"), ("chapter", "T", "he", {"epigraph": False})],
             [("part", "Chapter Three"), ("chapter", "F", "inally", {"epigraph": False})]]
    sections, paras = sections_of(novel(pages))
    # (titles differ in words: the same text at every page top would be a running header)
    assert [s.title for s in sections] == ["Chapter One", "Chapter Two", "Chapter Three"]
    assert paras[0].text.startswith("Harry the sand"), paras[0].text


def test_a_single_drop_cap_is_rejoined_but_makes_no_chapter():
    sections, paras = sections_of(novel([[("chapter", "D", "une", {"epigraph": False})]]))
    assert [s.title for s in sections] == [""]
    assert paras[0].text.startswith("Dune the sand")


def test_contents_page_tail_does_not_leak_into_the_text():
    toc = [("line", "CONTENTS", 14, 260)] + [("line", f"Chapter {k}", 13, LEFT) for k in range(1, 30)]
    pages = [toc, [("line", "Epilogue", 13, LEFT)], [("caps", "INTRODUCTION"),
                                                     ("chapter", "D", "une", {"epigraph": False})]]
    pages += chapter_pages(3, 1)
    sections, paras = sections_of(novel(pages))
    assert not any("Epilogue" in p.text for p in paras), [p.text[:40] for p in paras[:3]]
    assert sections[0].title == "INTRODUCTION"


def _span(text, size, bbox):
    return {"text": text, "size": size, "bbox": bbox, "font": "Times", "flags": 4, "origin": (bbox[0], bbox[3])}


def _line(*spans):
    xs = [s["bbox"] for s in spans]
    return {"dir": (1.0, 0.0), "spans": list(spans),
            "bbox": (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs))}


def test_merge_drop_caps_on_raw_spans():
    cap = {"type": 0, "lines": [_line(_span("“W", 50.6, (45, 243, 115, 299)))]}
    text = {"type": 0, "lines": [_line(_span("hat do you want?", 16.9, (117, 255, 300, 274))),
                                 _line(_span("asked the old woman.", 16.9, (117, 276, 320, 295))),
                                 _line(_span("Nobody answered her.", 16.9, (45, 297, 300, 316)))]}
    blocks = [cap, text]
    ingest_upload._merge_drop_caps(blocks)
    assert cap["lines"] == []
    first, second, third = text["lines"]
    assert first["spans"][0]["text"] == "“What do you want?" and first["spans"][0].get("dropcap")
    assert first["spans"][0]["bbox"][0] == 45     # the word now starts where the cap does
    assert second["spans"][0]["bbox"][0] == 45    # a line beside the cap is not an indented paragraph start
    assert third["spans"][0]["bbox"][0] == 45 and not third["spans"][0].get("dropcap")


def test_merge_drop_caps_ignores_big_letters_that_sit_beside_nothing():
    lone = {"type": 0, "lines": [_line(_span("A", 40, (45, 100, 70, 140)))]}
    far = {"type": 0, "lines": [_line(_span("text far to the right", 12, (300, 110, 500, 124)))]}
    same = {"type": 0, "lines": [_line(_span("B", 13, (45, 300, 53, 314))),
                                 _line(_span("eside a letter in body size", 12, (55, 300, 300, 314)))]}
    blocks = [lone, far, same]
    ingest_upload._merge_drop_caps(blocks)
    assert lone["lines"][0]["spans"][0]["text"] == "A" and far["lines"][0]["spans"][0]["text"].startswith("text")
    assert same["lines"][0]["spans"][0]["text"] == "B"


def test_drop_cap_without_an_epigraph_mid_book_is_a_scene_break_not_a_chapter():
    pages = chapter_pages(3) + [[("text", 1), ("line", "* * *", BODY, 280),
                                 ("chapter", "W", "hen", {"epigraph": False, "seed": 5})]]
    sections, paras = sections_of(novel(pages))
    assert [s.title for s in sections] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert any(p.text.startswith("When pale sky") for p in paras)


def test_all_caps_attribution_is_not_taken_for_a_heading():
    def chapter_with_caps_source(seed):
        return [("line", f"Those who would repeat the past must {WORDS[seed]} the teaching of it.", 9, LEFT + 30),
                ("line", f"-BENE GESSERIT CODA {WORDS[seed + 3].upper()}", BODY, 240),
                ("chapter", "W", "hen", {"epigraph": False, "seed": seed})]
    sections, paras = sections_of(novel([chapter_with_caps_source(k) for k in range(3)]))
    assert [s.title for s in sections] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert paras[sections[0].start_para].text.startswith("Those who would repeat")


def test_each_novel_of_a_collection_numbers_its_chapters_from_one():
    def one_novel(start):
        return [[("caps", "INTRODUCTION"), ("chapter", "D", "une", {"epigraph": False, "seed": start})]] \
            + chapter_pages(3, start)
    sections, _paras = sections_of(novel(one_novel(0) + one_novel(1)))
    assert [s.title for s in sections] == ["INTRODUCTION", "Chapter 1", "Chapter 2", "Chapter 3"] * 2


def test_small_text_without_an_attribution_over_a_drop_cap_is_no_chapter():
    dedication = [("line", "Here is another book dedicated to a friend, who gave this one its title.", 9, LEFT + 30),
                  ("line", " ", 24, LEFT), ("chapter", "O", "ne", {"epigraph": False, "seed": 4})]
    sections, paras = sections_of(novel(chapter_pages(3) + [dedication]))
    assert [s.title for s in sections] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert any(p.text.startswith("Here is another book") for p in paras)


def test_attribution_with_dots_and_a_number_is_not_a_contents_line():
    from app.ingest_upload import _drop_contents
    line = ("\u2014ANALYSIS, ARCHIVES #XOXTM99 . . . .. 041", 72, 100, 300, 112, 9.0, False, False)
    assert _drop_contents([[line]]) == [[line]]
