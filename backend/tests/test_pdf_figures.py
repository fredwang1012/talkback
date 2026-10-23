"""Textbook figures shown in the reader (#40): found from their captions, cropped to PNGs, placed between paragraphs."""
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import ingest_upload, main
from app.config import settings
from app.ingest_upload import build_book, pdf_to_text
from app.parsing import FIGURE_MARK, parse
from app.ratelimit import TokenBucket

PROSE = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, which is "
         "why green leaves matter so much to nearly every food chain on land, and why the study of photosynthesis "
         "has occupied botanists for well over two centuries of careful observation and experiment.")
AFTER = ("The stored sugars are broken down again later, releasing the energy that the organism spends on growth and "
         "repair throughout its whole life, a process that every living cell carries out in one form or another.")
CELLS = ("Alpha", "Beta", "Gamma", "Delta")
JUSTIFY = fitz.TEXT_ALIGN_JUSTIFY


def prose(page, y0, y1, text=PROSE):
    page.insert_textbox(fitz.Rect(72, y0, 520, y1), text, fontsize=11, align=JUSTIFY)


def table_figure(page, y):
    """A figure built from text only (like the IR textbook's matrices): short cells in a grid, then its caption."""
    for r, name in enumerate(CELLS):
        page.insert_text((130, y + 14 * r), name, fontsize=11)
        for c in range(4):
            page.insert_text((230 + 60 * c, y + 14 * r), str((r + c) % 2), fontsize=11)
    page.insert_text((90, y + 14 * len(CELLS) + 14), "► Figure 2.1   A tiny incidence table of four terms.", fontsize=9)
    return y + 14 * len(CELLS) + 30


def figure_paras(text):
    return [p for p in text.split("\n\n") if p.startswith(FIGURE_MARK)]


def build(*pages):
    doc = fitz.open()
    for draw in pages:
        draw(doc.new_page())
    return doc.tobytes()


def textbook_page(page):
    prose(page, 60, 200)
    y = table_figure(page, 230)
    prose(page, y + 10, y + 150, AFTER)


def test_a_text_figure_becomes_one_figure_paragraph_with_its_crop(tmp_path):
    text = pdf_to_text(build(textbook_page, lambda p: prose(p, 60, 300)), tmp_path)
    figs = figure_paras(text)
    assert figs == [FIGURE_MARK + "fig-1.png\nFigure 2.1: A tiny incidence table of four terms."]
    png = (tmp_path / "fig-1.png").read_bytes()
    assert png.startswith(b"\x89PNG")
    pix = fitz.Pixmap(tmp_path / "fig-1.png")
    assert pix.width > 300 and 80 < pix.height < 200  # the grid only: not the prose above or the caption below
    body = text.replace(figs[0], "")
    assert not any(c in body for c in CELLS) and "Figure 2.1" not in body  # the figure's text is not read as prose
    assert "convert light" in body and "stored sugars" in body
    assert text.index("convert light") < text.index(FIGURE_MARK) < text.index("stored sugars")


def test_without_an_output_folder_the_figure_text_is_still_left_out(tmp_path):
    text = pdf_to_text(build(textbook_page, lambda p: prose(p, 60, 300)))
    assert not figure_paras(text) and not any(c in text for c in CELLS) and "Figure 2.1" not in text
    assert "convert light" in text and "stored sugars" in text


def test_a_figure_at_a_page_break_goes_after_the_sentence_it_interrupts(tmp_path):
    def first(page):
        prose(page, 60, 300)
        prose(page, 310, 380, "Before the figure this sentence runs over the page break and we will refer to")

    def second(page):
        y = table_figure(page, 70)
        prose(page, y + 10, y + 150, "them as words for now. " + AFTER)
    paras = pdf_to_text(build(first, second), tmp_path).split("\n\n")
    whole = next(p for p in paras if "we will refer to" in p)
    assert "refer to them as words for now." in whole
    assert paras[paras.index(whole) + 1].startswith(FIGURE_MARK)


def test_an_embedded_picture_above_its_caption(tmp_path):
    def page(p):
        prose(p, 60, 200)
        pic = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), False)
        pic.set_rect(pic.irect, (200, 60, 60))
        p.insert_image(fitz.Rect(150, 230, 450, 430), pixmap=pic)
        p.insert_text((90, 450), "Figure 3.4 A red square.", fontsize=9)
        prose(p, 465, 600, AFTER)
    text = pdf_to_text(build(page, lambda p: prose(p, 60, 300)), tmp_path)
    assert figure_paras(text) == [FIGURE_MARK + "fig-1.png\nFigure 3.4: A red square."]
    pix = fitz.Pixmap(tmp_path / "fig-1.png")
    assert pix.width > 500 and pix.height > 380  # the whole picture (300 x 200 pt at 150 dpi)


def test_a_caption_with_nothing_next_to_it_is_dropped_as_before(tmp_path):
    def page(p):
        prose(p, 60, 200)
        p.insert_text((90, 215), "Figure 9.9 A caption whose figure is on another page.", fontsize=9)
        prose(p, 225, 360, AFTER)
    text = pdf_to_text(build(page, lambda p: prose(p, 60, 300)), tmp_path)
    assert not figure_paras(text) and "Figure 9.9" not in text and not list(tmp_path.iterdir())


def test_a_body_sentence_starting_with_figure_is_not_a_caption(tmp_path):
    def page(p):
        prose(p, 60, 200)
        prose(p, 210, 300, "Figure 2.1 shows how light is absorbed by the pigments inside the membrane, and the next "
                           "paragraph describes what happens to the energy that is captured there.")
    text = pdf_to_text(build(page, lambda p: prose(p, 60, 300)), tmp_path)
    assert "Figure 2.1 shows" in text and not figure_paras(text)


def test_a_figure_and_a_display_equation_on_one_page_stay_apart(tmp_path):
    """#40 and #41 together: each keeps its own marker, the equation stays a math paragraph beside the figure."""
    def page(p):
        prose(p, 60, 200)
        p.insert_text((200, 232), "y = b + a x + e", fontsize=11, fontname="symb")
        p.insert_text((470, 232), "(3.4)", fontsize=11)
        prose(p, 255, 330, AFTER)
        y = table_figure(p, 370)
        prose(p, y + 10, y + 150)
    book = build_book("u-x-00000000", "T", "", "textbook", pdf_to_text(build(page, lambda p: prose(p, 60, 300)), tmp_path))
    kinds = [(p.kind, p.figure or (p.math.number if p.math else None)) for p in book.paragraphs if p.kind]
    assert kinds == [("math", "3.4"), ("figure", "fig-1.png")]
    assert not any(c in p.text for p in book.paragraphs for c in "\x02\x03\x04\x05")


def test_parse_and_build_book_make_figure_paragraphs_for_textbooks_only():
    raw = "Intro text here.\n\n" + FIGURE_MARK + "fig-1.png\nFigure 1.1: A matrix.\n\nMore text."
    _, paras = parse(raw, None, None)
    assert [(p.kind, p.figure, p.text) for p in paras] == [
        (None, None, "Intro text here."), ("figure", "fig-1.png", "Figure 1.1: A matrix."), (None, None, "More text.")]
    book = build_book("u-x-00000000", "T", "", "textbook", raw)
    fig = book.paragraphs[1]
    assert fig.kind == "figure" and fig.figure == "fig-1.png"
    assert "".join(c.tts_text for c in fig.tts_chunks).startswith("Figure")  # the caption is read aloud by default
    novel = build_book("u-x-00000000", "T", "", "novel", raw)
    assert novel.paragraphs[1].kind is None and novel.paragraphs[1].figure is None


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(settings, "retrieval_backend", "local")
    main._upload_status.clear()
    monkeypatch.setattr(main, "_ingest_guard", main.IngestGuard())
    monkeypatch.setattr(main, "upload_bucket", TokenBucket(1000, 60))
    main._index_for.cache_clear()
    return TestClient(main.app)


def test_an_uploaded_textbook_serves_its_figures(client, tmp_path):
    pdf = build(textbook_page, lambda p: prose(p, 60, 300))
    r = client.post("/api/books/upload", files={"file": ("t.pdf", pdf)},
                    data={"title": "Botany", "author": "", "kind": "textbook"})
    book_id = r.json()["book_id"]
    book = client.get(f"/api/books/{book_id}").json()
    figs = [p for p in book["paragraphs"] if p["kind"] == "figure"]
    assert [(p["figure"], p["text"]) for p in figs] == [("fig-1.png", "Figure 2.1: A tiny incidence table of four terms.")]
    img = client.get(f"/api/books/{book_id}/figures/fig-1.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content.startswith(b"\x89PNG")
    for bad in ("fig-2.png", "book.json", "..%2Fbook.json", "fig-1.png.json"):
        assert client.get(f"/api/books/{book_id}/figures/{bad}").status_code == 404
    assert client.get("/api/books/no-such-book/figures/fig-1.png").status_code == 404
    client.delete(f"/api/books/{book_id}")
    assert not ingest_upload.figures_dir(book_id).exists()


def test_figures_need_the_access_code_like_the_book(client, monkeypatch, tmp_path):
    d = tmp_path / "books" / "u-x-00000000" / "figures"
    d.mkdir(parents=True)
    (d / "fig-1.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(settings, "access_token", "secret")
    assert client.get("/api/books/u-x-00000000/figures/fig-1.png").status_code == 401
    ok = client.get("/api/books/u-x-00000000/figures/fig-1.png", headers={"X-Talkback-Token": "secret"})
    assert ok.status_code == 200
