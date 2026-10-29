"""The PDF engines behind app/pdfdoc.py agree on small synthetic pages (built with pymupdf, which the tests have),
and the ingest heuristics give the same book under either. pdfium tests are skipped when pypdfium2 is not
installed (it is not a project dependency while pymupdf is the default engine)."""
import struct
import time

import pymupdf as fitz
import pytest

from app import pdfdoc
from app.config import settings
from app.ingest_upload import HEADING_REGEX, build_book, pdf_to_text

BODY = ("The morning light came slowly over the hills and into the valley below us, where the river ran wide "
        "and the mill wheel turned without a sound. ") * 3
try:
    import pypdfium2  # noqa: F401
    HAVE_PDFIUM = True
except ImportError:
    HAVE_PDFIUM = False
pdfium_only = pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdfium2 not installed (the backend's pdfium extra)")


def book_pdf() -> bytes:
    """Two chapters, a bold heading, an italic span, an indented paragraph, a drawing and an image."""
    doc = fitz.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 90), f"Chapter {n}", fontsize=20, fontname="hebo")
        page.insert_textbox(fitz.Rect(72, 120, 520, 400), BODY, fontsize=11)
        page.insert_text((90, 420), "An indented paragraph starts here and goes on for a few more words.", fontsize=11)
        page.insert_text((72, 434), "The end of it, set in italics:", fontsize=11, fontname="tiit")
        page.draw_rect(fitz.Rect(72, 500, 300, 600), color=(0, 0, 0), width=1)
        page.insert_text((80, 520), "label one", fontsize=8)
        page.insert_text((80, 540), "label two", fontsize=8)
        page.insert_text((80, 560), "label three", fontsize=8)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
        pix.clear_with(128)
        page.insert_image(fitz.Rect(350, 500, 450, 600), pixmap=pix)
    doc.set_metadata({"title": "A Title", "author": "An Author"})
    doc.set_toc([[1, "Chapter 1", 1], [1, "Chapter 2", 2]])
    return doc.tobytes()


def lines_of(page: pdfdoc.Page) -> list[tuple[str, int, float]]:
    """(text, span count, size) per text line, as the ingest pipeline reads them."""
    out = []
    for b in page.text_blocks():
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            text = "".join(s["text"] for s in ln["spans"]).strip()
            if text:
                out.append((text, len(ln["spans"]), round(max(s["size"] for s in ln["spans"]), 1)))
    return out


def test_pymupdf_engine_is_the_default_and_unknown_engines_are_refused():
    assert settings.pdf_engine == "pymupdf"
    with pdfdoc.open_pdf(book_pdf()) as doc:
        assert isinstance(doc, pdfdoc._MuPdfDocument) and doc.page_count == 2
    with pytest.raises(ValueError):
        pdfdoc.open_pdf(book_pdf(), engine="xpdf")
    with pytest.raises(pdfdoc.PdfError):
        pdfdoc.open_pdf(b"not a pdf at all")


def test_pymupdf_engine_pages_images_drawings_and_metadata():
    with pdfdoc.open_pdf(book_pdf()) as doc:
        assert doc.metadata == {"title": "A Title", "author": "An Author"}
        assert doc.toc() == [[1, "Chapter 1", 1], [1, "Chapter 2", 2]]
        page = doc.page(0)
        assert page.rect == (0.0, 0.0, 595.0, 842.0) and page.width == 595.0 and page.area == 595.0 * 842.0
        texts = [t for t, _n, _s in lines_of(page)]
        assert texts[0] == "Chapter 1" and any(t.startswith("The morning light") for t in texts)
        images = [b["bbox"] for b in page.text_blocks() if b.get("type") == 1]
        assert len(images) == 1 and [round(v) for v in images[0]] == [350, 500, 450, 600]
        clusters = page.drawing_clusters(1_000_000, 2000)
        assert len(clusters) == 1 and [round(v) for v in clusters[0]] == [72, 500, 300, 600]
        assert page.drawing_clusters(1_000_000, 0) is None   # past the path bound: no figure detection
        raster = page.render((72, 500, 300, 600), zoom=1.0)
        raster.paint_out(72, 500, 100, 520)
        assert raster.png().startswith(b"\x89PNG")
        assert page.clip_rect((-10, -10, 50, 50)) == (0.0, 0.0, 50.0, 50.0)
        assert "Chapter 1" in page.plain_text()


def test_cluster_rects_joins_neighbours_and_drops_rules():
    page = (0, 0, 100, 100)
    rects = [(10, 10, 20, 20), (22, 10, 30, 20), (50, 50, 60, 60), (0, 90, 100, 91), (200, 200, 300, 300)]
    assert pdfdoc.cluster_rects(rects, page) == [(10, 10, 30, 20), (50, 50, 60, 60)]  # the rule and the off-page rect go
    assert pdfdoc.cluster_rects([], page) == []


def font_blob(subtables: list[bytes], num_glyphs: int, repeat_offset: bool = False) -> bytes:
    """A TrueType table directory holding a cmap with these subtables (platform 3) and a maxp glyph count."""
    records, body = b"", b""
    for sub in subtables:
        records += struct.pack(">HHI", 3, 1, 4 + 8 * (len(subtables) + repeat_offset) + len(body))
        body += sub
    if repeat_offset:   # a second record pointing at the first subtable again
        records += struct.pack(">HHI", 3, 1, 4 + 8 * (len(subtables) + 1))
    cmap = struct.pack(">HH", 0, len(subtables) + repeat_offset) + records + body
    maxp = struct.pack(">IH", 0x00010000, num_glyphs)
    tables = [(b"cmap", cmap), (b"maxp", maxp)]
    head = struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0)
    directory, data = b"", b""
    for tag, table in tables:
        directory += struct.pack(">4sIII", tag, 0, 12 + 16 * len(tables) + len(data), len(table))
        data += table
    return head + directory + data


def format4(segments: list[tuple[int, int, int, int]], glyph_array: list[int]) -> bytes:
    """A format 4 subtable: segments (start, end, delta, id range offset) plus the glyph id array."""
    n = len(segments)
    ends = b"".join(struct.pack(">H", s[1]) for s in segments)
    starts = b"".join(struct.pack(">H", s[0]) for s in segments)
    deltas = b"".join(struct.pack(">h", s[2]) for s in segments)
    offsets = b"".join(struct.pack(">H", s[3]) for s in segments)
    glyphs = b"".join(struct.pack(">H", g) for g in glyph_array)
    length = 16 + 8 * n + len(glyphs)
    return struct.pack(">HHHHHHH", 4, length, 0, 2 * n, 0, 0, 0) + ends + b"\x00\x00" + starts + deltas + offsets + glyphs


def test_reverse_cmap_reads_real_mappings_and_stays_bounded_on_a_crafted_font():
    # segment 0: A-C -> glyphs 5-7 by delta; segment 1: a-b -> glyphs 9-10 through the glyph array (offset from
    # &idRangeOffset[1] past its own and the 3 later offsets: 2 * (5 - 1)); segment 2 runs backwards; segment 3 maps
    # every code from U+1000 up to its own glyph id (61,440 codes); then the closing 0xFFFF segment
    segs = [(0x41, 0x43, 5 - 0x41, 0), (0x61, 0x62, 0, 2 * (5 - 1)), (0x300, 0x100, 0, 0), (0x1000, 0xFFFF, 0, 0), (0xFFFF, 0xFFFF, 1, 0)]
    huge12 = struct.pack(">HHIII", 12, 0, 16 + 12 * 20_000, 0, 20_000) + struct.pack(">III", 0, 0x10FFFF, 1) * 20_000
    blob = font_blob([format4(segs, [9, 10])], num_glyphs=20_000, repeat_offset=True)
    started = time.perf_counter()
    rev = pdfdoc._reverse_cmap(blob)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    assert rev[5] == 0x41 and rev[7] == 0x43 and rev[9] == 0x61 and rev[10] == 0x62
    assert 30_000 not in rev          # past the font's glyph count, although the full-range segment maps it
    assert len(rev) <= pdfdoc.CMAP_BUDGET
    assert pdfdoc._reverse_cmap(b"not a font") == {} and pdfdoc._reverse_cmap(b"") == {}
    assert pdfdoc._reverse_cmap(font_blob([], num_glyphs=10)) == {}
    # format 12 alone (20,000 groups of 1.1 million codes each, listed twice) stops at the budget too
    started = time.perf_counter()
    rev = pdfdoc._reverse_cmap(font_blob([huge12], num_glyphs=0xFFFF, repeat_offset=True))
    assert time.perf_counter() - started < 2.0
    assert rev[1] == 0 and rev[2] == 1
    assert len(rev) <= pdfdoc.CMAP_BUDGET


def test_png_encoder_round_trips_through_pymupdf():
    png = pdfdoc.encode_png(3, 2, bytes([0, 128, 255, 10, 20, 30]), gray=True)
    pix = fitz.Pixmap(png)
    assert (pix.width, pix.height, pix.n) == (3, 2, 1) and bytes(pix.samples) == bytes([0, 128, 255, 10, 20, 30])
    png = pdfdoc.encode_png(1, 1, bytes([1, 2, 3]), gray=False)
    assert fitz.Pixmap(png).n == 3


@pdfium_only
def test_pdfium_engine_groups_text_like_pymupdf():
    data = book_pdf()
    with pdfdoc.open_pdf(data, "pymupdf") as a, pdfdoc.open_pdf(data, "pdfium") as b:
        assert isinstance(b, pdfdoc._PdfiumDocument)
        assert b.page_count == 2 and b.metadata == a.metadata and b.toc() == a.toc()
        pa, pb = a.page(0), b.page(0)
        assert pb.rect == pa.rect
        la, lb = lines_of(pa), lines_of(pb)
        assert [t for t, _n, _s in lb] == [t for t, _n, _s in la]
        assert [s for _t, _n, s in lb] == [s for _t, _n, s in la]
        # the heading is bold, the italic line italic, under both engines
        for page in (pa, pb):
            spans = [s for blk in page.text_blocks() if blk.get("type") == 0 for ln in blk["lines"] for s in ln["spans"]]
            assert all(s["flags"] & 16 for s in spans if s["text"].startswith("Chapter"))
            assert all(s["flags"] & 2 for s in spans if "italics" in s["text"])
        # spans carry the same origins and widths; the font box heights of these non-embedded fonts come from
        # each library's own metrics for the standard 14 fonts (embedded fonts agree to the point)
        sa = [s for blk in pa.text_blocks() if blk.get("type") == 0 for ln in blk["lines"] for s in ln["spans"]]
        sb = [s for blk in pb.text_blocks() if blk.get("type") == 0 for ln in blk["lines"] for s in ln["spans"]]
        for x, y in zip(sa, sb):
            assert x["text"] == y["text"]
            assert all(abs(p - q) < 1.0 for p, q in zip(x["origin"], y["origin"]))
            assert abs(x["bbox"][0] - y["bbox"][0]) < 1.0 and abs(x["bbox"][2] - y["bbox"][2]) < 1.0
            assert abs(x["bbox"][1] - y["bbox"][1]) < 0.35 * x["size"] and abs(x["bbox"][3] - y["bbox"][3]) < 0.35 * x["size"]
        assert [tuple(round(v) for v in blk["bbox"]) for blk in pb.text_blocks() if blk.get("type") == 1] == \
            [tuple(round(v) for v in blk["bbox"]) for blk in pa.text_blocks() if blk.get("type") == 1]
        ca, cb = pa.drawing_clusters(10 ** 6, 2000), pb.drawing_clusters(10 ** 6, 2000)
        assert len(ca) == len(cb) == 1   # pdfium's path box includes the stroke width, pymupdf's does not
        assert all(abs(p - q) <= 1.0 for p, q in zip(ca[0], cb[0]))
        assert pb.drawing_clusters(10 ** 6, 0) is None
        assert "Chapter 1" in pb.plain_text()


@pdfium_only
def test_pdfium_engine_renders_and_paints_out():
    data = book_pdf()
    with pdfdoc.open_pdf(data, "pdfium") as doc:
        page = doc.page(0)
        raster = page.render((72, 500, 300, 600), dpi=72, gray=True)
        before = sum(v < 128 for v in fitz.Pixmap(raster.png()).samples)
        raster.paint_out(72, 500, 300, 600)
        after = fitz.Pixmap(raster.png())
        assert before > 0 and sum(v < 128 for v in after.samples) == 0 and after.n == 1
        assert fitz.Pixmap(page.render((72, 500, 300, 600), zoom=2.0).png()).n == 3


@pdfium_only
def test_same_book_under_both_engines(monkeypatch):
    data = book_pdf()
    books = {}
    for engine in ("pymupdf", "pdfium"):
        monkeypatch.setattr(settings, "pdf_engine", engine)
        books[engine] = build_book("u-x", "x", "", "novel", pdf_to_text(data))
    a, b = books["pymupdf"], books["pdfium"]
    assert [s.title for s in b.sections] == [s.title for s in a.sections] == ["Chapter 1", "Chapter 2"]
    assert [p.text for p in b.paragraphs] == [p.text for p in a.paragraphs]
    assert HEADING_REGEX  # the shared regex is what parses both
