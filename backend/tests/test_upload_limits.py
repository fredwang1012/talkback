"""Upload hardening: paragraph merging, visibility, caps, ids, rate limits, headings."""
import random
import re
import time

import pymupdf as fitz

from app import ingest_upload, main
from app.ratelimit import TokenBucket
from app.schema import Book, Narrator, Paragraph, Section
from app.ttschunks import ABBREVIATIONS, _SENT_END, chunk_paragraph, sentence_spans
from tests.test_upload import client, make_pdf, post  # noqa: F401  (client is a fixture)

L1 = "The morning light came slowly over the hills and into the valley below us"   # 74 chars, no period
L2 = "and nobody in the village was awake to see it happen on that cold day now."     # ends a sentence, full width
L3 = "Then the old baker opened his shutters and began to knead the first loaf of"
L4 = "bread for the day."                                                           # short last line


def lines_pdf(lines):
    """lines: (text, x, y) drawn one insert_text call each (so each is its own line/block)."""
    doc = fitz.open()
    page = doc.new_page()
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    return doc.tobytes()


def paras_of(pdf):
    return ingest_upload.pdf_to_text(pdf).split("\n\n")


def test_separate_lines_become_one_paragraph():
    # L2 ends a sentence mid-paragraph but is a full-width line, so it must not split (justified prose)
    paras = paras_of(lines_pdf([(L1, 72, 100), (L2, 72, 114), (L3, 72, 128), (L4, 72, 142)]))
    assert len(paras) == 1
    assert paras[0] == " ".join([L1, L2, L3, L4])


def test_gap_or_indent_splits_paragraphs():
    base = [(L1, 72, 100), (L2, 72, 114), (L3, 72, 128), (L4, 72, 142)]
    by_gap = paras_of(lines_pdf(base + [(L1, 72, 170), (L4, 72, 184)]))
    by_indent = paras_of(lines_pdf(base + [(L1, 92, 156), (L4, 72, 170)]))
    by_short_line = paras_of(lines_pdf(base + [(L1, 72, 156), (L4, 72, 170)]))
    for paras in (by_gap, by_indent, by_short_line):
        assert len(paras) == 2
        assert paras[0].endswith("bread for the day.") and paras[1].startswith("The morning light")


def test_headings_stand_alone_in_pdf():
    lines = [(L1, 72, 100), (L3, 72, 114), ("CHAPTER TWO", 72, 160), (L1, 72, 200), (L4, 72, 214)]
    paras = paras_of(lines_pdf(lines))
    assert "CHAPTER TWO" in paras and len(paras) == 3
    # full path: it becomes a section
    book = ingest_upload.build_book("u-x-00000000", "T", "", "novel", "\n\n".join(paras))
    assert [s.title for s in book.sections] == ["", "CHAPTER TWO"]


def test_numbered_and_word_headings_in_text():
    raw = "CHAPTER ONE\n\nOpening text here.\n\n12 The Wedding\n\nMore text here.\n\n3 apples fell.\n\nPart of the plan."
    book = ingest_upload.build_book("u-x-00000000", "T", "", "novel", raw)
    assert [s.title for s in book.sections] == ["CHAPTER ONE", "12 The Wedding"]
    assert [p.text for p in book.paragraphs][-2:] == ["3 apples fell.", "Part of the plan."]


def test_errored_and_processing_uploads_are_not_visible(client, monkeypatch, tmp_path):
    seen = {}

    async def failing_index(book, chunks, embedder, store):
        seen["listed_during"] = [b["id"] for b in client.get("/api/books").json()]
        seen["book_json_during"] = (tmp_path / "books" / book.id / "book.json").exists()
        raise RuntimeError("boom")

    monkeypatch.setattr("app.gcp.indexing.index_book", failing_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(main.settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.\n\nTwo.").json()["book_id"]
    assert seen["listed_during"] == [] and seen["book_json_during"] is False
    assert client.get(f"/api/books/{book_id}/status").json()["status"] == "error"
    assert [b["id"] for b in client.get("/api/books").json()] == []
    assert client.get(f"/api/books/{book_id}").status_code == 404
    d = tmp_path / "books" / book_id
    assert not (d / "book.json").exists() and not (d / "index").exists()


def test_page_and_char_caps(client, monkeypatch):
    monkeypatch.setattr(ingest_upload, "MAX_PAGES", 2)
    three = fitz.open()
    for _ in range(3):
        three.new_page().insert_text((72, 100), L1)
    try:
        ingest_upload.pdf_to_text(three.tobytes())
        raise AssertionError("expected UploadError")
    except ingest_upload.UploadError as e:
        assert "pages" in str(e)
    monkeypatch.setattr(ingest_upload, "MAX_CHARS", 50)
    for name, data in (("a.txt", ("word " * 30).encode()), ("a.pdf", make_pdf([[L1, L2]]))):
        book_id = post(client, name, data).json()["book_id"]
        st = client.get(f"/api/books/{book_id}/status").json()
        assert st["status"] == "error" and "too much text" in st["message"]


def test_long_paragraph_is_split(client):
    sent = "This is a fairly ordinary sentence of moderate length. "
    long = (sent * 400).strip()  # ~22k chars
    nopunct = "word " * 3000
    book = ingest_upload.build_book("u-x-00000000", "T", "", "textbook", f"Intro.\n\n{long}\n\n{nopunct}\n\nEnd.")
    assert all(len(p.text) <= ingest_upload.MAX_PARAGRAPH_CHARS for p in book.paragraphs)
    assert [p.idx for p in book.paragraphs] == list(range(len(book.paragraphs)))
    assert len(book.paragraphs) > 8
    assert " ".join(p.text for p in book.paragraphs[1:5]).count("ordinary sentence") > 20
    assert book.sections[0].start_para == 0


def _old_sentence_spans(text):
    """The previous quadratic implementation, kept as the reference for identical output."""
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        before = text[start:m.start() + 1]
        last_word = re.findall(r"[A-Za-z]+\.?$", before)
        word = last_word[0].rstrip(".").lower() if last_word else ""
        nxt = text[end:].lstrip()
        if m.group().rstrip("\"')]") == ".":
            if word in ABBREVIATIONS or re.search(r"(?:^|\s)[A-Z]\.$", before):
                continue
        if nxt[:1].islower():
            continue
        spans.append((start, end))
        start = end + (len(text[end:]) - len(text[end:].lstrip()))
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text.rstrip())))
    return spans


def test_sentence_spans_matches_reference():
    rng = random.Random(7)
    words = ["Mr.", "Dr.", "J.", "and", "He", "she", "said.", "wait...", "No?", "yes!", '"Hi."', "St.", "the", "A.", "end.)"]
    for _ in range(300):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 60)))
        if rng.random() < 0.3:
            text = "  " + text + "  "
        assert sentence_spans(text) == _old_sentence_spans(text), text


def test_huge_paragraph_chunks_fast():
    text = ("Mr. Smith went home. He was tired! Was he? Yes, quite so. " * 4000)[:200_000]
    book = Book(id="b", title="t", author="a", kind="textbook", license="x", sections=[Section(id="s1", title="", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text=text)], narrator=Narrator(voice="marcus"))
    t = time.perf_counter()
    chunks = chunk_paragraph(book.paragraphs[0], book)
    assert time.perf_counter() - t < 1.0
    assert chunks and all(len(c.tts_text) <= 280 for c in chunks)


def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", TokenBucket(2, 60))
    assert post(client, "a.txt", b"hello there").status_code == 202
    assert post(client, "a.txt", b"hello there").status_code == 202
    assert post(client, "a.txt", b"hello there").status_code == 429


def test_one_ingest_at_a_time(client):
    main._ingest_busy = True
    r = post(client, "a.txt", b"hello there")
    assert r.status_code == 429 and "still being processed" in r.json()["detail"]
    main._ingest_busy = False
    assert post(client, "a.txt", b"hello there").status_code == 202
    assert main._ingest_busy is False  # released after the background task


def test_validation_failure_releases_busy_flag(client):
    assert post(client, "a.pdf", b"not a pdf").status_code == 400
    assert main._ingest_busy is False


def test_content_length_precheck(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_upload, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(ingest_upload, "MULTIPART_SLACK", 0)

    async def no_read(*a, **k):
        raise AssertionError("body must not be read")

    monkeypatch.setattr("starlette.datastructures.UploadFile.read", no_read)
    r = post(client, "a.txt", b"x" * 300)
    assert r.status_code == 413
    assert main._ingest_busy is False
    assert not (tmp_path / "uploads").exists()
