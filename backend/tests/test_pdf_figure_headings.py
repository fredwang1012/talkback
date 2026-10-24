"""Figure text, axis labels, equation numbers and back-matter running headers never become chapters (#84)."""
import pymupdf as fitz

from app import ingest_upload
from app.ingest_upload import HEADING_REGEX, pdf_to_text
from app.parsing import parse

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")
FONTS = {"h": (20, "hebo"), "label": (14, "helv"), "boldlabel": (14, "hebo"), "line": (11, "helv"),
         "small": (8, "helv")}


def make(pages):
    """pages: lists of items. ('p', text) paragraph; (kind, text) a line of FONTS[kind]; ('figure', h) a vector
    figure box h points tall with a plotted line; ('image', h) a grey image h points tall; ('at', kind, text, dx,
    dy) a line placed relative to the last figure's top-left; ('rot', text) text rotated 90 degrees in the last
    figure."""
    doc = fitz.open()
    for items in pages:
        page = doc.new_page()
        y = 60
        fig = None
        for item in items:
            kind = item[0]
            if kind == "p":
                page.insert_textbox(fitz.Rect(72, y, 500, y + 100), item[1], fontsize=11)
                y += 110
            elif kind == "figure":
                fig = fitz.Rect(150, y, 450, y + item[1])
                page.draw_rect(fig, color=(0, 0, 0), width=0.8)
                page.draw_line(fig.bl + (10, -10), fig.tr + (-10, 10), color=(0, 0, 0))
                y += item[1] + 20
            elif kind == "image":
                fig = fitz.Rect(150, y, 450, y + item[1])
                pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 30, 20), False)
                pix.clear_with(200)
                page.insert_image(fig, pixmap=pix)
                y += item[1] + 20
            elif kind == "at":
                size, font = FONTS[item[1]]
                page.insert_text((fig.x0 + item[3], fig.y0 + item[4] + size), item[2], fontsize=size, fontname=font)
            elif kind == "rot":
                page.insert_text((fig.x0 + 8, fig.y0 + 120), item[1], fontsize=14, fontname="helv", rotate=90)
            else:
                size, font = FONTS[kind]
                page.insert_text((72, y + size), item[1], fontsize=size, fontname=font)
                y += size + 22
    return doc.tobytes()


def titles(data):
    sections, _ = parse(pdf_to_text(data), None, None, HEADING_REGEX)
    return [s.title for s in sections if s.title]


def test_labels_inside_a_vector_figure_are_not_headings():
    pages = [[("h", "Chapter 1 Cells"), ("p", BODY), ("figure", 200),
              ("at", "label", "China", 30, 40), ("at", "label", "Kenya", 160, 150), ("at", "label", "UK", 200, 20),
              ("p", BODY2), ("h", "1.2 Results"), ("p", BODY)]]
    assert titles(make(pages)) == ["Chapter 1 Cells", "1.2 Results"]


def test_text_over_an_image_is_not_a_heading():
    pages = [[("h", "Chapter 1 Cells"), ("p", BODY), ("image", 150),
              ("at", "boldlabel", "REUTERS", 10, 10), ("at", "boldlabel", "Extreme conditions create rare clouds", 10, 60),
              ("at", "small", "You are here: Home > News > Science", 10, 35), ("at", "small", "Tue Aug 1, 2006", 10, 85),
              ("p", BODY2)]]
    assert titles(make(pages)) == ["Chapter 1 Cells"]


def test_rotated_axis_label_is_not_a_heading():
    pages = [[("h", "Chapter 1 Cells"), ("p", BODY), ("figure", 200), ("rot", "Precision"),
              ("at", "label", "Recall", 140, 170),
              ("at", "small", "0.0", 30, 150), ("at", "small", "0.5", 140, 150), ("at", "small", "1.0", 250, 150),
              ("p", BODY2)]]
    assert titles(make(pages)) == ["Chapter 1 Cells"]


def test_heading_set_close_above_a_figure_is_kept():
    pages = [[("h", "Chapter 1 Cells"), ("p", BODY), ("h", "1.2 Results"), ("figure", 120), ("p", BODY2)]]
    assert titles(make(pages)) == ["Chapter 1 Cells", "1.2 Results"]


def test_numbered_line_smaller_than_the_body_is_not_a_heading():
    line = ("20 NEWSGROUPS", 87.0, 559.0, 148.0, 569.0, 8.0, False, False)
    assert ingest_upload._heading_candidate([line], 10.0) is None
    real = ("12 The Wedding", 87.0, 559.0, 148.0, 569.0, 10.0, False, False)
    assert ingest_upload._heading_candidate([real], 10.0) is not None


def test_control_glyphs_from_math_fonts_do_not_fake_a_heading_mark():
    """CMEX10 brackets extract as \\x01, the parser's font-heading prefix: "\\x01 + b = 0 (15.3)" was a chapter."""
    assert ingest_upload._clean_line_text("\x01 + b = 0") == "+ b = 0"
    line = (ingest_upload._clean_line_text("\x01 + b = 0 (15.3)"), 128.0, 471.0, 384.0, 509.0, 10.0, False, False)
    assert ingest_upload._heading_candidate([line], 10.0) is None


FILLER = [[("p", BODY), ("p", BODY2)] for _ in range(30)]  # a long book: the headers are on few of its pages


def _back_matter(title, first_page, n):
    """A back-matter section: its opening page, then pages headed alternately '<n> Title' / 'Title <n>'."""
    pages = [[("h", title), ("p", BODY)]]
    for k in range(n):
        num = first_page + k
        head = f"{num} {title}" if num % 2 == 0 else f"{title} {num}"
        pages.append([("line", head), ("p", BODY2 if k % 2 else BODY)])
    return pages


def test_back_matter_running_headers_are_one_section():
    pages = [[("h", "Chapter 21 Link analysis"), ("p", BODY)]] + FILLER + _back_matter("Bibliography", 484, 6) \
        + _back_matter("Author Index", 522, 6)
    assert titles(make(pages)) == ["Chapter 21 Link analysis", "Bibliography", "Author Index"]


def test_big_numbered_chapter_heading_survives_matching_running_headers():
    """'12 The Wedding' set large opens the chapter; the small 'The Wedding 185' headers do not remove it."""
    pages = [[("h", "11 The Party"), ("p", BODY)]] + FILLER + [[("h", "12 The Wedding"), ("p", BODY)]]
    for num in range(185, 191):
        head = f"{num} The Wedding" if num % 2 == 0 else f"The Wedding {num}"
        pages.append([("line", head), ("p", BODY2)])
    assert titles(make(pages)) == ["11 The Party", "12 The Wedding"]


def test_figure_detection_is_bounded_on_a_page_full_of_drawings(monkeypatch):
    """A crafted page with thousands of vector paths skips figure detection instead of clustering them all."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Chapter 1 Cells", fontsize=20, fontname="hebo")
    page.insert_textbox(fitz.Rect(72, 120, 500, 220), BODY, fontsize=11)
    shape = page.new_shape()
    for i in range(ingest_upload.MAX_DRAWINGS + 500):
        x, y = 72 + (i % 400), 300 + (i // 400) * 3
        shape.draw_line((x, y), (x + 1, y + 1))
        shape.finish(color=(0, 0, 0))  # one path per line
    shape.commit()

    def boom(*a, **k):
        raise AssertionError("cluster_drawings must not run past MAX_DRAWINGS")
    monkeypatch.setattr(fitz.Page, "cluster_drawings", boom)
    assert titles(doc.tobytes()) == ["Chapter 1 Cells"]


def test_figure_regions_bail_out_on_too_many_rects_and_scan_lines_by_position():
    lines = [(f"label {i}", 10.0, float(i), 40.0, i + 8.0, 10.0, False, False) for i in range(0, 50_000, 10)]
    many = [(0.0, 0.0, 50.0, 50.0)] * (ingest_upload.MAX_FIGURE_RECTS + 1)
    assert ingest_upload._figure_regions(many, lines, 595.0 * 842.0) == []
    # a small region near the top sees only its neighbours, and three of them are enough
    assert len(ingest_upload._figure_regions([(0.0, 0.0, 50.0, 50.0)], lines, 595.0 * 842.0)) == 1
    assert ingest_upload._figure_regions([(0.0, 0.0, 50.0, 50.0)], [], 1.0) == []
