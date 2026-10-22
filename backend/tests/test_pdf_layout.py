"""Textbook page furniture: captions, footnotes/sidebars, tables and two-column reading order."""
import pymupdf as fitz

from app.ingest_upload import pdf_to_text

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")
LEFT = "Left column text starts here and keeps going for a while so that it wraps over several lines in the narrow column."
RIGHT = "Right column text follows after the left one and also wraps over several lines inside its own narrow column."


def build(draw):
    doc = fitz.open()
    page = doc.new_page()
    draw(page)
    return doc.tobytes()


def body_page(page, extra, size=11):
    page.insert_textbox(fitz.Rect(72, 80, 500, 180), BODY, fontsize=11)
    page.insert_textbox(fitz.Rect(72, 190, 500, 290), BODY2, fontsize=11)
    extra(page)


def test_caption_blocks_are_dropped():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 330), "Figure 8.2: The thylakoid membrane.", fontsize=11)
        page.insert_textbox(fitz.Rect(72, 340, 500, 370), "Table 3.1. Key enzymes and their roles.", fontsize=11)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "Figure 8.2" not in text and "Table 3.1" not in text and "convert light" in text


def test_body_sentence_about_a_figure_is_kept():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 400),
                            "Figure 8.2 shows how light is absorbed by the pigments inside the membrane, and the "
                            "next paragraph describes what happens to the energy that is captured.", fontsize=11)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "Figure 8.2 shows" in text


def test_small_font_footnotes_and_sidebars_are_dropped():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 340), "1 A footnote that is set well below the body size.",
                            fontsize=7)
        page.insert_textbox(fitz.Rect(72, 350, 500, 390), "Sidebar remark in tiny type.", fontsize=7.5)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "footnote" not in text and "Sidebar" not in text and "convert light" in text


def test_numeric_table_cells_are_dropped():
    def extra(page):
        y = 300
        for row in ("12", "3.5", "40%", "7.1", "88"):
            page.insert_text((72, y), row, fontsize=11)
            y += 16
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "3.5" not in text and "40%" not in text and "convert light" in text


def test_numbers_in_a_sentence_are_kept():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 400),
                            "The reaction runs at 25 degrees and yields 3.5 grams of product in about 40 minutes.",
                            fontsize=11)
    assert "3.5 grams" in pdf_to_text(build(lambda p: body_page(p, extra)))


def test_two_columns_are_read_left_then_right():
    def draw(page):
        # right column drawn first so the stream order is wrong
        page.insert_textbox(fitz.Rect(320, 80, 520, 300), RIGHT, fontsize=11)
        page.insert_textbox(fitz.Rect(72, 80, 272, 300), LEFT, fontsize=11)
        page.insert_textbox(fitz.Rect(72, 320, 272, 420), LEFT + " More.", fontsize=11)
        page.insert_textbox(fitz.Rect(320, 320, 520, 420), RIGHT + " More.", fontsize=11)
    text = pdf_to_text(build(draw))
    assert text.count("Left column") == 2 and text.count("Right column") == 2
    assert text.rfind("Left column") < text.find("Right column")


def test_single_column_order_is_unchanged():
    text = pdf_to_text(build(lambda p: body_page(p, lambda q: None)))
    assert text.index("convert light") < text.index("second paragraph")


def test_caption_with_leading_marker_symbol_is_dropped():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 330), "\u25ee Figure 2.10 Postings lists with skip pointers.",
                            fontsize=9)
        page.insert_textbox(fitz.Rect(72, 340, 500, 370), "\u25b6Table 4.1 Sizes of the indexes", fontsize=9)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "Figure 2.10" not in text and "Table 4.1" not in text and "convert light" in text


def test_marker_caption_without_punctuation_in_body_size_is_kept_as_sentence():
    def extra(page):
        page.insert_textbox(fitz.Rect(72, 300, 500, 400),
                            "\u25ee Figure 8.2 shows how light is absorbed by the pigments inside the membrane, and "
                            "the next paragraph describes what happens to the energy that is captured.", fontsize=11)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "Figure 8.2 shows" in text


def test_small_caption_inside_a_body_block_is_dropped_without_the_following_text():
    def extra(page):
        # one pymupdf block: a small caption, a gap, then body-size text (as in the IR textbook)
        page.insert_text((72, 300), "\u25ee Figure 2.10 Postings lists intersection with skip pointers.", fontsize=9)
        page.insert_text((72, 340), "Skip pointers will only be available for the original postings lists.",
                         fontsize=11)
    text = pdf_to_text(build(lambda p: body_page(p, extra)))
    assert "Figure 2.10" not in text and "Skip pointers will only be" in text
