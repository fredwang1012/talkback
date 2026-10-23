"""Program code and pseudocode in textbooks: detected during PDF extraction, shown, but not read word by word."""
import pymupdf as fitz

from app import ingest_upload
from app.ingest_upload import HEADING_REGEX, CODE_MARK, _mark_code, build_book, pdf_to_text
from app.parsing import parse
from app.schema import Paragraph

BODY = ("Plants convert light energy into chemical energy and store it in sugars for later use by the cell, "
        "which is why green leaves matter so much to nearly every food chain on land.")
BODY2 = ("A second paragraph explains how the stored sugars are broken down again, releasing energy that the "
         "organism spends on growth and repair throughout its whole life.")


def page_with(draw):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 60, 500, 150), BODY, fontsize=11)
    draw(page)
    page.insert_textbox(fitz.Rect(72, 520, 500, 600), BODY2, fontsize=11)
    return doc.tobytes()


def listing(text):
    sections, paras = parse(text, None, None, HEADING_REGEX)
    return paras


def test_monospace_block_becomes_a_code_paragraph():
    def draw(page):
        y = 200
        for line in ("def total(xs):", "    s = 0", "    for x in xs:", "        s += x", "    return s"):
            page.insert_text((72, y), line, fontsize=10, fontname="cour")
            y += 13
    paras = listing(pdf_to_text(page_with(draw)))
    assert [p.kind for p in paras] == [None, "code", None]
    assert "def total(xs):" in paras[1].text and "return s" in paras[1].text
    assert paras[0].text == BODY and paras[2].text == BODY2


def test_numbered_pseudocode_in_a_proportional_font_is_code():
    def draw(page):
        y = 200
        page.insert_text((72, y), "INTERSECT(p1, p2)", fontsize=10, fontname="hebo")
        rows = ["answer = empty", "while p1 != NIL and p2 != NIL", "do if docID(p1) = docID(p2)",
                "then ADD(answer, docID(p1))", "else return answer"]
        page.insert_text((72, y + 13), "\n".join(f"{n}   {row}" for n, row in enumerate(rows, 1)), fontsize=10,
                         fontname="helv", lineheight=1.3)
    paras = listing(pdf_to_text(page_with(draw)))
    assert [p.kind for p in paras] == [None, "code", None]
    assert "INTERSECT(p1, p2)" in paras[1].text and "while p1" in paras[1].text


def test_prose_with_a_few_keywords_is_not_code():
    def draw(page):
        page.insert_textbox(fitz.Rect(72, 200, 500, 300),
                            "If the cell has enough light, then it makes sugar; otherwise it will return to rest "
                            "and wait while the day ends, and so on for the following hours of the day.",
                            fontsize=11)
    paras = listing(pdf_to_text(page_with(draw)))
    assert all(p.kind is None for p in paras)


def test_numbered_prose_list_is_not_code():
    """A numbered exercise/step list ("1. If ... then ...") is prose, even with code-like keywords on every line."""
    def ln(t, y):
        return (t, 72.0, y, 400.0, y + 10, 10.0, False, False)
    rows = ["1. If the index is too large, then split it.", "2. For each term, do a lookup in the dictionary.",
            "3. Return the list of postings until done."]
    blk = [ln(t, 100 + 12 * i) for i, t in enumerate(rows)]
    assert not ingest_upload._code_score(blk)
    assert ingest_upload.section_level("2.1 Heading") == 2   # the section-number regex is separate and still works


def test_mark_code_detects_arrows_and_numbered_lines_in_tuples():
    def ln(t, y):
        return (t, 72.0, y, 200.0, y + 10, 10.0, False, False)
    blk = [ln("1", 100), ln("answer \u2190 \u27e8\u27e9", 100), ln("2", 112), ln("while p1 \u2260 NIL", 112),
           ln("3", 124), ln("do return answer", 124)]
    out = _mark_code([blk])
    assert len(out) == 1 and out[0][0][0].startswith(CODE_MARK)
    assert "1 answer \u2190 \u27e8\u27e9" in out[0][0][0]


def test_code_chunk_is_a_short_spoken_note_and_text_is_kept_for_display():
    raw = f"{BODY}\n\n{CODE_MARK}1 x = 1\n2 return x\n\n{BODY2}"
    book = build_book("u-x", "T", "A", "textbook", raw)
    code = [p for p in book.paragraphs if p.kind == "code"][0]
    assert code.text == "1 x = 1\n2 return x"
    assert [c.tts_text for c in code.tts_chunks] == ["Code listing skipped."]
    other = [p for p in book.paragraphs if p.kind is None]
    assert len(other) == 2 and other[0].tts_chunks[0].tts_text.removeprefix("<|prosody:long_pause|>").startswith("Plants")


def test_non_textbook_does_not_skip_code():
    raw = f"{BODY}\n\n{CODE_MARK}1 x = 1\n2 return x"
    book = build_book("u-x", "T", "A", "novel", raw)
    assert all(p.kind is None for p in book.paragraphs)
    assert "Code listing" not in " ".join(c.tts_text for p in book.paragraphs for c in p.tts_chunks)


def test_old_book_json_without_kind_still_loads():
    assert Paragraph.model_validate({"idx": 0, "section_id": "s1", "text": "hi"}).kind is None


def test_long_code_listing_keeps_its_kind_when_split():
    big = "\n".join(f"{i} x = {i}" for i in range(1, 900))
    book = build_book("u-x", "T", "A", "textbook", f"{BODY}\n\n{CODE_MARK}{big}")
    codes = [p for p in book.paragraphs if p.kind == "code"]
    assert len(codes) >= 2 and all(c.tts_chunks[0].tts_text == "Code listing skipped." for c in codes)
