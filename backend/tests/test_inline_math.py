"""Inline formulas of uploaded textbook PDFs: marked during extraction when their text would read wrong, cropped as
pictures, read by Gemini, shown typeset inside the sentence and read out in place of the garbled text."""
import asyncio

import pymupdf as fitz
import pytest

from app import main
from app.config import settings
from app.enrich import equations
from app.ingest_upload import (HEADING_REGEX, _attach_deco_rows, _by_x, _cap_paragraphs, _clean_line_text, _crop,
                               _equation_clip, _mark_inline, _math_class, build_book, equation_path, make_book_dir,
                               pdf_to_text, plain_text, publish_book, render_inline, update_equations)
from app.parsing import IM_CLOSE, IM_OPEN, inline_marker, parse, split_inline, strip_inline
from app.schema import Book, InlineMath, Paragraph, Section
from app.ttschunks import MAX_CHARS, chunk_paragraph, inline_spans
from app.tts_cache import align_timestamps

TAIL = "is unbiased for every sample we draw from the population."


def pdf(sub: bool = True, n: int = 1) -> bytes:
    """A prose line "The slope estimate b1 is unbiased ..." with b in Symbol and, if `sub`, a smaller lowered 1."""
    doc = fitz.open()
    page = doc.new_page()
    for i in range(n):
        y = 200 + 40 * i
        page.insert_text((72, y), "The slope estimate", fontsize=11)
        page.insert_text((170, y), "b", fontsize=11, fontname="symb")
        if sub:
            page.insert_text((176, y + 3), "1", fontsize=7)
        page.insert_text((182, y), TAIL, fontsize=11)
    return doc.tobytes()


def paras(raw: str) -> list[Paragraph]:
    return parse(raw, None, None, HEADING_REGEX)[1]


def span(text, font="CMR10", size=10.0, x=0.0, y=100.0):
    return {"text": text, "font": font, "size": size, "origin": (x, y), "bbox": (x, y - size, x + 5 * len(text), y + 2)}


# ---- detection ----

def test_subscripted_symbol_is_marked_with_its_box():
    [p] = paras(pdf_to_text(pdf()))
    assert p.text == "The slope estimate b1 " + TAIL
    [m] = p.inline_math
    assert m.id == "m1" and p.text[m.start:m.end] == "b1" and m.page == 0
    x0, y0, x1, y1 = m.bbox
    assert 169 < x0 < 171 and 178 < x1 < 182 and y0 < 200 < y1


def test_a_plain_symbol_reads_fine_and_is_not_marked():
    [p] = paras(pdf_to_text(pdf(sub=False)))
    assert p.inline_math == [] and "b " in p.text


def test_markers_never_reach_shown_or_model_text():
    raw = pdf_to_text(pdf())
    assert IM_OPEN in raw
    assert IM_OPEN not in plain_text(raw) and IM_CLOSE not in plain_text(raw) and "b1" in plain_text(raw)
    assert all(IM_OPEN not in p.text and IM_CLOSE not in p.text for p in paras(raw))


def test_prose_punctuation_and_numbers_stay_outside_the_formula():
    line = [span("In Equation"), span(" "), span("3.1,"), span(" "), span("β", "CMMI10", x=40),
            span("0", "CMR7", 7.0, x=45, y=101.5), span(" "), span("and"), span(" "), span("β", "CMMI10", x=70),
            span("1.", "CMR7", 7.0, x=75, y=101.5)]
    text, [a, b] = split_inline(_mark_inline(line, 0), 1)
    assert text == "In Equation 3.1, β0 and β1."
    assert text[a.start:a.end] == "β0" and text[b.start:b.end] == "β1"


def test_operator_names_and_brackets_open_a_formula_but_prose_words_do_not():
    line = [span("solves"), span(" "), span("max"), span("π", "CMMI7", 7.0, y=101.5), span(" "),
            span("J", "CMMI10"), span("(i.e.,"), span(" "), span("the"), span(" "), span("return)")]
    text, [m] = split_inline(_mark_inline(line, 0), 1)
    assert text[m.start:m.end] == "maxπ J" and text.startswith("solves maxπ J(i.e.,")


def test_a_root_drawn_as_its_own_block_joins_the_formula_beside_it():
    """A tall root is its own PDF block and its font baseline sits near the line above. It belongs in the gap
    between "RSE =" and "RSS", and "SE" in front of a hat stays in that formula."""
    def S(text, font, x, y, size=10.0):
        return {"text": text, "font": font, "size": size, "origin": (x, y),
                "bbox": (x, y - size, x + 5 * len(text), y + 2)}
    above = [S("given by the formula ", "CMR10", 50, 286)]
    root = [S("⎨", "CMEX10", 90, 289)]
    root[0]["bbox"] = (90, 270, 100, 320)
    body = [S("RSE =", "CMR10", 50, 298), S("RSS/(n-2).", "CMR10", 110, 298),
            S(" Strictly speaking, this is the estimate.", "CMR10", 200, 298)]
    groups = [[[above, 286.0]], [[root, 289.4]], [[body, 298.0]]]
    _attach_deco_rows(groups)
    marked = []
    for blk in groups:
        for item in blk:
            if item is None:
                continue
            spans = _by_x(item[0])
            if _math_class(spans) == 0:
                marked.append(_mark_inline(spans, 0) or _clean_line_text("".join(s["text"] for s in spans)))
    assert any(m and "RSE" in m and "RSS" in m and "⎨" in m for m in marked)
    assert all("⎨" not in m or "RSE" in m for m in marked)


def test_a_tall_delimiter_stays_with_the_equation_under_it():
    """A display equation's brackets are their own block, and the font baseline sits nearer the sentence above
    than the equation. The bracket belongs to the equation, not inside "a 95% chance"."""
    def S(text, font, x, y, size=10.0, box=None):
        span = {"text": text, "font": font, "size": size, "origin": (x, y),
                "bbox": (x, y - size, x + 8 * len(text), y + 2)}
        if box:
            span["bbox"] = box
        return span
    sentence = [S("That is, there is approximately a 95 % chance that the interval", "CMR10", 52, 432.6)]
    bracket = [S("&", "CMEX10", 141, 442.4, box=(141.7, 434.7, 146.4, 471.9))]
    equation = [S("β1 − 2 SE(β1), β1 + 2 SE(β1)", "CMMI10", 146, 455)]
    groups = [[[sentence, 432.6]], [[bracket, 442.4]], [[equation, 455.0]]]
    _attach_deco_rows(groups)
    sentence_text = ""
    for blk in groups:
        for item in blk:
            if item is None:
                continue
            spans = _by_x(item[0])
            if _math_class(spans) == 0:
                sentence_text += _clean_line_text("".join(s["text"] for s in spans))
    assert "&" not in sentence_text
    assert "95" in sentence_text and "chance" in sentence_text


def test_equation_box_stops_above_the_next_line():
    eq = ("sum", 60, 142, 200, 193, 10, False, False)
    where = ("where the variance is", 52, 178, 360, 192, 10, False, False)
    _x0, _y0, _x1, y1 = _equation_clip([eq], [164.0], [where], {id(where): 189.0})
    assert y1 < 182  # above the letters, which start near the baseline 189


def test_ordinary_lines_are_left_alone():
    assert _mark_inline([span("Plain prose with no math at all.")], 0) is None
    assert _mark_inline([span("where "), span("x", "CMMI10"), span(" is the input.")], 0) is None


def test_markers_round_trip():
    marked = "Let " + inline_marker(3, (10.0, 20.5, 30.25, 40.0), "ˆy") + " be the fit."
    assert strip_inline(marked) == "Let ˆy be the fit."
    text, [m] = split_inline(marked, 7)
    assert m.id == "m7" and text[m.start:m.end] == "ˆy" and m.page == 3 and m.bbox == [10.0, 20.5, 30.2, 40.0]


def test_a_heading_with_a_formula_is_still_a_heading():
    raw = "3.1 Estimating " + inline_marker(0, (0, 0, 1, 1), "β0") + "\n\nBody text here."
    sections, ps = parse(raw, None, None, HEADING_REGEX)
    assert sections[0].title == "3.1 Estimating β0" and [p.text for p in ps] == ["Body text here."]


def test_novels_keep_no_inline_formulas():
    raw = pdf_to_text(pdf())
    assert all(not p.inline_math for p in build_book("u-x-00000000", "T", "A", "novel", raw).paragraphs)
    assert any(p.inline_math for p in build_book("u-x-00000000", "T", "A", "textbook", raw).paragraphs)


def test_long_paragraph_split_keeps_formulas_whole_and_shifted():
    sentence = "The estimate β1 is fine. "
    text = sentence * 400
    inline = [InlineMath(id=f"m{i}", start=i * len(sentence) + 13, end=i * len(sentence) + 15, page=0,
                         bbox=[0, 0, 1, 1]) for i in range(400)]
    out = _cap_paragraphs([Section(id="s1", title="", start_para=0)],
                          [Paragraph(idx=0, section_id="s1", text=text.strip(), inline_math=inline)])
    assert len(out) > 1 and sum(len(p.inline_math) for p in out) == 400
    assert all(p.text[m.start:m.end] == "β1" for p in out for m in p.inline_math)


# ---- speech ----

BOOK = Book(id="b", title="T", author="A", kind="textbook", license="x", sections=[], paragraphs=[])


def para(spoken: str | None = "beta one hat", n: int = 1) -> Paragraph:
    s = "The slope ˆβ1 is unbiased for every sample we draw. "
    text = (s * n).strip()
    inline = [InlineMath(id=f"m{i}", start=i * len(s) + 10, end=i * len(s) + 13, page=0, bbox=[0, 0, 1, 1],
                         spoken=spoken) for i in range(n)]
    return Paragraph(idx=0, section_id="s1", text=text, inline_math=inline)


def test_the_reading_replaces_the_garbled_text():
    [c] = chunk_paragraph(para(), BOOK)
    assert c.tts_text == "The slope beta one hat is unbiased for every sample we draw."
    [c] = chunk_paragraph(para(spoken=None), BOOK)   # no reading yet: the shown text, as before
    assert "hat" not in c.tts_text and c.tts_text.startswith("The slope ˆ")


def test_chunks_stay_under_the_limit_with_long_readings():
    p = para(spoken="beta one hat, the least squares estimate of the slope of the regression line", n=12)
    cs = chunk_paragraph(p, BOOK)
    assert len(cs) > 1 and all(len(c.tts_text) <= MAX_CHARS for c in cs)
    assert sum(c.tts_text.count("beta one hat") for c in cs) == 12


def test_spoken_words_highlight_the_formula():
    p = para()
    [c] = chunk_paragraph(p, BOOK)
    plain = p.text[c.char_start:c.char_end]
    words = [{"word": w, "start": i, "end": i + 1} for i, w in enumerate(c.tts_text.split())]
    aligned = align_timestamps(words, plain, inline_spans(p, c.char_start, c.char_end))
    spans = [plain[a["char_start"]:a["char_end"]] for a in aligned]
    assert spans[:6] == ["The", "slope", "ˆβ1", "ˆβ1", "ˆβ1", "is"]


# ---- pictures and readings ----

@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def published(data: bytes) -> str:
    book_id, _ = make_book_dir("Inline Test")
    publish_book(build_book(book_id, "Inline Test", "A", "textbook", pdf_to_text(data)))
    return book_id


def load(book_id: str) -> Book:
    return Book.model_validate_json((settings.data_dir / "books" / book_id / "book.json").read_text())


def test_identical_formulas_share_one_picture(data_dir):
    data = pdf(n=3)
    book_id = published(data)
    ids = [m.id for p in load(book_id).paragraphs for m in p.inline_math]
    assert len(ids) == 3
    crops, shared = render_inline(book_id, data)
    assert list(crops) == [ids[0]] and shared == {ids[0]: ids}
    assert crops[ids[0]][0].startswith(b"\x89PNG") and crops[ids[0]][1] == "b1"
    assert equation_path(book_id, ids[0]).exists() and not equation_path(book_id, ids[1]).exists()
    images = {m.image for p in load(book_id).paragraphs for m in p.inline_math}
    [image] = images
    assert image.startswith(f"/api/books/{book_id}/equations/{ids[0]}.png?v=")


def test_crops_paint_out_other_lines():
    """A math-font box taller than its glyphs must not bring the next line's letters into the picture."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 200), "x", fontsize=11)
    page.insert_text((72, 209), "WWWW", fontsize=11)   # tight leading: its tops reach into the box below
    clean = fitz.open()
    clean.new_page().insert_text((72, 200), "x", fontsize=11)
    box = [72.0, 190.0, 100.0, 203.0]

    def ink(png: bytes) -> int:
        return sum(v < 128 for v in fitz.Pixmap(png).samples)
    assert ink(_crop(doc, 0, box, (0.5, 0.5), {})) == ink(_crop(clean, 0, box, (0.5, 0.5), {})) > 0


def test_crop_hides_a_word_tucked_into_a_tall_formula():
    """A sum or root reaches the line above, so that line's baseline falls inside the box. The word goes; the formula stays."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((80, 100), "we can", fontsize=11)
    page.insert_text((100, 112), "y", fontsize=11)
    clean = fitz.open()
    clean.new_page().insert_text((100, 112), "y", fontsize=11)
    box = [90.0, 92.0, 140.0, 116.0]

    def ink(png: bytes) -> int:
        return sum(v < 128 for v in fitz.Pixmap(png).samples)
    kept = _crop(doc, 0, box, (0, 0), {}, mask_prose=True)
    assert ink(kept) == ink(_crop(clean, 0, box, (0, 0), {}, mask_prose=True)) > 0


def test_update_rechunks_with_the_reading(data_dir):
    book_id = published(pdf())
    [m] = [m for p in load(book_id).paragraphs for m in p.inline_math]
    assert update_equations(book_id, {m.id: {"latex": "\\beta_1", "spoken": "beta one"}}) == 1
    [p] = load(book_id).paragraphs
    assert p.inline_math[0].latex == "\\beta_1" and "beta one" in p.tts_chunks[0].tts_text


def test_equations_job_reads_inline_formulas(data_dir, monkeypatch):
    data = pdf(n=2)
    book_id = published(data)
    monkeypatch.setattr(equations, "available", lambda: True)
    monkeypatch.setattr(equations, "make_llm", lambda provider: None)
    calls = []

    async def fake_transcribe(bid, found, **kw):
        calls.append((list(found), kw.get("fit", False)))
        return {k: {"latex": "\\beta_1", "spoken": "beta one"} for k in found}
    monkeypatch.setattr(equations, "transcribe", fake_transcribe)
    asyncio.run(main._equations_job(book_id, data))
    assert calls == [(["m1"], True)]
    ms = [m for p in load(book_id).paragraphs for m in p.inline_math]
    assert len(ms) == 2 and all(m.latex == "\\beta_1" and m.spoken == "beta one" and m.image for m in ms)


def test_inline_pass_fits_under_the_book_cap(monkeypatch):
    monkeypatch.setattr(settings, "upload_book_cap_usd", equations.estimate(10))
    assert equations.fitting(100) <= 10 and equations.fitting(5) == 5
    assert equations.fitting(10, spent=settings.upload_book_cap_usd) == 0


def test_picture_route_serves_inline_ids(data_dir, monkeypatch):
    from fastapi.testclient import TestClient
    data = pdf()
    book_id = published(data)
    render_inline(book_id, data)
    c = TestClient(main.app)
    assert c.get(f"/api/books/{book_id}/equations/m1.png").status_code == 200
    assert c.get(f"/api/books/{book_id}/equations/x1.png").status_code == 404
