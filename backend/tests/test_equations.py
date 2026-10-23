"""Display equations of uploaded textbook PDFs (#41): detected during extraction, cropped as pictures, read by Gemini
as LaTeX and a spoken line, shown typeset in the reader and spoken as one short chunk."""
import asyncio
import json

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.enrich import equations
from app.enrich.llm import LLM, Raw
from app.ingest_upload import (HEADING_REGEX, NOT_MATH, STRONG, WEAK, _math_class, build_book, equation_path,
                               make_book_dir, pdf_to_text, plain_text, publish_book, render_equations,
                               update_equations)
from app.parsing import EQ_MARK, EQ_SEP, parse, split_equation_number
from app.schema import Book, MathBlock, Paragraph
from app.ttschunks import chunk_paragraph, equation_tts_text

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")


def pdf(draw) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 60, 500, 150), BODY, fontsize=11)
    draw(page)
    page.insert_textbox(fitz.Rect(72, 240, 500, 330), BODY2, fontsize=11)
    return doc.tobytes()


def display(page):
    page.insert_text((200, 200), "y = b + a x + e", fontsize=11, fontname="symb")
    page.insert_text((470, 200), "(3.4)", fontsize=11)


def paras(raw: str) -> list[Paragraph]:
    return parse(raw, None, None, HEADING_REGEX)[1]


def spans(*parts):
    return [{"text": t, "font": f} for t, f in parts]


# ---- line classes ----

def test_math_class():
    assert _math_class(spans(("y = βx + ε", "CMMI10"))) == STRONG
    assert _math_class(spans(("∑", "CMEX10"), ("xi", "CMMI10"))) == STRONG
    assert _math_class(spans(("n", "CMMI7"))) == WEAK                    # a sum's limit
    assert _math_class(spans(("(3.4)", "CMR10"))) == WEAK
    assert _math_class(spans(("R2 0.612 312.1", "CMR10"))) == WEAK      # a table row is never an equation alone
    assert _math_class(spans(("where the error term is", "CMR10"), ("ε", "CMMI10"))) == NOT_MATH
    assert _math_class(spans(("   ", "CMMI10"))) == NOT_MATH


# ---- detection ----

def test_display_equation_becomes_a_math_paragraph_with_its_number():
    ps = paras(pdf_to_text(pdf(display)))
    assert [p.kind for p in ps] == [None, "math", None]
    m = ps[1].math
    assert m.id == "e1" and m.page == 0 and m.number == "3.4" and not m.garbled
    assert ps[1].text == "y = b + a x + e (3.4)"
    assert ps[0].text == BODY and ps[2].text == BODY2
    x0, y0, x1, y1 = m.bbox
    assert 195 < x0 < 205 and x1 > 470 and y0 < 200 < y1


def test_inline_math_stays_in_its_sentence():
    def inline(page):
        page.insert_text((72, 200), "The fitted line is", fontsize=11)
        page.insert_text((152, 200), "y = b + a x", fontsize=11, fontname="symb")   # a space after "is"
        page.insert_text((207, 200), "for every value we observed here.", fontsize=11)
    ps = paras(pdf_to_text(pdf(inline)))
    assert all(p.kind is None for p in ps)
    assert any("The fitted line is" in p.text and "y = b + a x" in p.text for p in ps)


def test_last_line_of_a_paragraph_is_no_display_equation():
    """A short math-looking line at the margin, at normal leading under prose, ends the paragraph above it."""
    def tail(page):
        page.insert_textbox(fitz.Rect(72, 160, 500, 200), "and so the estimate for the slope is written as", fontsize=11)
        page.insert_text((72, 186), "b + a", fontsize=11, fontname="symb")
    ps = paras(pdf_to_text(pdf(tail)))
    assert all(p.kind is None for p in ps)


def test_markers_never_reach_shown_or_model_text():
    raw = pdf_to_text(pdf(display))
    assert EQ_MARK in raw
    assert plain_text(raw).count(EQ_SEP) == 0 and "200.0" not in plain_text(raw)
    assert all(EQ_MARK not in p.text and EQ_SEP not in p.text for p in paras(raw))


def test_split_equation_number():
    assert split_equation_number("y = 2x, (3.4)") == ("y = 2x", "3.4")
    assert split_equation_number("y = 2x (10.12a).") == ("y = 2x", "10.12a")
    assert split_equation_number("y = f(x)") == ("y = f(x)", None)


def test_novels_keep_no_equations():
    raw = pdf_to_text(pdf(display))
    novel = build_book("u-x-00000000", "T", "A", "novel", raw)
    assert all(p.kind is None and p.math is None for p in novel.paragraphs)
    textbook = build_book("u-x-00000000", "T", "A", "textbook", raw)
    assert [p.kind for p in textbook.paragraphs].count("math") == 1


# ---- speech ----

def math_para(**m) -> Paragraph:
    return Paragraph(idx=4, section_id="s0", text=m.pop("text", "y = b + a x (3.18)"), segments=[], kind="math",
                     math=MathBlock(id="e1", page=0, bbox=[0, 0, 1, 1], **m))


def test_spoken_equation():
    assert equation_tts_text(math_para(number="3.18")) == "y equals b plus a x"   # a faithful text is read out
    assert equation_tts_text(math_para(number="3.18", garbled=True)) == "Equation three point one eight."
    assert equation_tts_text(math_para(number=None, garbled=True)) == "Equation."
    said = equation_tts_text(math_para(number="3.4", spoken="y equals beta one times x."))
    assert said == "Equation three point four: y equals beta one times x."
    long = equation_tts_text(math_para(number="1.1", spoken=" ".join(["beta"] * 200)))
    assert len(long) <= 280


def test_equation_is_one_chunk_over_the_whole_text():
    book = Book(id="b", title="T", author="A", kind="textbook", license="x", sections=[], paragraphs=[])
    p = math_para(number="3.4", garbled=True)
    [c] = chunk_paragraph(p, book)
    assert (c.char_start, c.char_end) == (0, len(p.text)) and c.speaker == "narrator"


# ---- pictures and readings ----

@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def published(data: bytes) -> str:
    book_id, _ = make_book_dir("Eq Test")
    book = build_book(book_id, "Eq Test", "A", "textbook", pdf_to_text(data))
    publish_book(book)
    return book_id


def load(book_id: str) -> Book:
    return Book.model_validate_json((settings.data_dir / "books" / book_id / "book.json").read_text())


def test_render_crops_and_records_the_picture(data_dir):
    data = pdf(display)
    book_id = published(data)
    crops = render_equations(book_id, data)
    assert list(crops) == ["e1"]
    png, hint = crops["e1"]
    assert png.startswith(b"\x89PNG") and hint == "y = b + a x + e (3.4)"
    assert equation_path(book_id, "e1").read_bytes() == png
    [m] = [p.math for p in load(book_id).paragraphs if p.math]
    assert m.image.startswith(f"/api/books/{book_id}/equations/e1.png?v=")


def test_render_stops_for_a_deleted_book(data_dir):
    data = pdf(display)
    book_id = published(data)
    assert render_equations(book_id, data, stop=lambda: True) == {}
    assert not equation_path(book_id, "e1").exists()
    (data_dir / "books" / book_id / "book.json").unlink()
    (data_dir / "books" / book_id / "equations").rmdir()
    assert render_equations(book_id, data) == {}
    assert not (data_dir / "books" / book_id / "equations").exists()


def test_update_rechunks_with_the_spoken_reading(data_dir):
    book_id = published(pdf(display))
    assert update_equations(book_id, {"e1": {"latex": "y = \\beta x", "spoken": "y equals beta x"},
                                      "e99": {"latex": "z"}}) == 1
    [p] = [p for p in load(book_id).paragraphs if p.math]
    assert p.math.latex == "y = \\beta x"
    assert p.tts_chunks[0].tts_text == "Equation three point four: y equals beta x."
    assert update_equations("u-gone-00000000", {"e1": {"latex": "z"}}) == 0


class FakeGemini:
    def __init__(self, reply=None, usd_tokens=(1000, 100)):
        self.calls: list = []
        self.reply = reply or (lambda ids: [{"id": i, "latex": f"x_{{{i}}}", "spoken": f"x {i}"} for i in ids])
        self.tokens = usd_tokens

    async def complete(self, system, user, schema, tier, max_tokens, effort):
        ids = [part for part in user if isinstance(part, str)]
        self.calls.append(ids)
        return Raw(text=json.dumps({"items": self.reply(ids)}), in_tokens=self.tokens[0], out_tokens=self.tokens[1],
                   model="gemini-test")


def fake_parts(batch):
    return [k for k, _png, _hint in batch]


def run(crops, fake, **caps):
    llm = LLM("gemini", {"gemini": fake}, fallback=False)
    return asyncio.run(equations.transcribe("u-b-00000000", crops, llm=llm, parts=fake_parts))


@pytest.fixture
def budget(monkeypatch, data_dir):
    monkeypatch.setattr(settings, "upload_book_cap_usd", 5.0)
    monkeypatch.setattr(settings, "upload_total_cap_usd", 50.0)


def crops(n):
    return {f"e{i}": (b"png", "x") for i in range(1, n + 1)}


def test_transcribe_in_batches(budget):
    fake = FakeGemini()
    out = run(crops(19), fake)
    assert sorted(len(c) for c in fake.calls) == [3, 8, 8]
    assert out["e7"] == {"latex": "x_{e7}", "spoken": "x e7"} and len(out) == 19


def test_transcribe_ignores_unknown_ids_empty_and_delimited_latex(budget):
    def reply(ids):
        return [{"id": "e1", "latex": "\\[ a+b \\]", "spoken": "a plus b"},
                {"id": "e2", "latex": "", "spoken": ""},          # not an equation
                {"id": "e42", "latex": "z", "spoken": "z"}]        # not asked for
    assert run(crops(2), FakeGemini(reply)) == {"e1": {"latex": "a+b", "spoken": "a plus b"}}


def test_transcribe_stops_at_the_book_cap(budget, monkeypatch):
    monkeypatch.setattr(settings, "upload_book_cap_usd", 0.0011)
    monkeypatch.setattr(equations, "check_caps", lambda est: None)
    fake = FakeGemini(usd_tokens=(2_000_000, 0))   # every call is far over the cap
    out = run(crops(80), fake)
    assert len(fake.calls) < 10 and len(out) <= 8 * len(fake.calls)


def test_transcribe_refused_over_the_caps(budget, monkeypatch):
    monkeypatch.setattr(settings, "upload_book_cap_usd", 0.0)
    fake = FakeGemini()
    assert run(crops(3), fake) == {} and fake.calls == []


def test_transcribe_survives_a_failed_batch(budget):
    class Flaky(FakeGemini):
        async def complete(self, system, user, *a):
            if "e1" in user:
                raise RuntimeError("boom")
            return await super().complete(system, user, *a)
    out = run(crops(12), Flaky())
    assert sorted(out) == sorted(f"e{i}" for i in range(9, 13))


def test_only_with_gemini_configured(monkeypatch):
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(settings, "gcp_project", "")
    assert not equations.available()
    monkeypatch.setattr(settings, "gcp_project", "p")
    assert equations.available()


# ---- the picture route ----

def test_picture_route(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "access_token", "secret")
    data = pdf(display)
    book_id = published(data)
    render_equations(book_id, data)
    c = TestClient(main.app)
    r = c.get(f"/api/books/{book_id}/equations/e1.png")   # an <img> sends no access code
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    for bad in (f"/api/books/{book_id}/equations/e2.png", f"/api/books/{book_id}/equations/book.png",
                "/api/books/..%2Fcache/equations/e1.png", f"/api/books/{book_id}/equations/e1x.png"):
        assert c.get(bad).status_code == 404


def test_equations_job_crops_then_reads(data_dir, monkeypatch):
    data = pdf(display)
    book_id = published(data)
    monkeypatch.setattr(equations, "available", lambda: True)

    async def fake_transcribe(bid, found, **kw):
        assert list(found) == ["e1"]
        return {"e1": {"latex": "y=x", "spoken": "y equals x"}}
    monkeypatch.setattr(equations, "transcribe", fake_transcribe)
    monkeypatch.setattr(equations, "make_llm", lambda provider: None)
    asyncio.run(main._equations_job(book_id, data))
    [m] = [p.math for p in load(book_id).paragraphs if p.math]
    assert m.image and m.latex == "y=x" and m.spoken == "y equals x"
    assert book_id not in main._equation_jobs
