"""Upload hardening: paragraph merging, visibility, caps, ids, rate limits, headings."""
import asyncio
import random
import re
import time

import pymupdf as fitz
import pytest

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


def test_book_listed_during_indexing_and_after_index_failure(client, monkeypatch):
    seen = {}

    async def failing_index(book, chunks, embedder, store, on_progress=None):
        seen["listed_during"] = [b["id"] for b in client.get("/api/books").json()]
        raise RuntimeError("boom")

    monkeypatch.setattr("app.gcp.indexing.index_book", failing_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(main.settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.\n\nTwo.").json()["book_id"]
    assert seen["listed_during"] == [book_id]  # readable while indexing
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["status"] == "ready" and st["index"]["state"] == "error"
    assert client.get(f"/api/books/{book_id}").status_code == 200


def test_extraction_failure_is_not_visible(client, tmp_path):
    book_id = post(client, "b.pdf", make_pdf([[]])).json()["book_id"]
    assert client.get(f"/api/books/{book_id}/status").json()["status"] == "error"
    assert [b["id"] for b in client.get("/api/books").json()] == []
    assert client.get(f"/api/books/{book_id}").status_code == 404
    d = tmp_path / "books" / book_id
    assert not (d / "book.json").exists() and not (d / "index").exists()


def test_page_and_char_caps(client, monkeypatch):
    from app.ingest import common  # the extraction caps are read from here (the facade only re-exports them)
    monkeypatch.setattr(common, "MAX_PAGES", 2)
    three = fitz.open()
    for _ in range(3):
        three.new_page().insert_text((72, 100), L1)
    try:
        ingest_upload.pdf_to_text(three.tobytes())
        raise AssertionError("expected UploadError")
    except ingest_upload.UploadError as e:
        assert "pages" in str(e)
    monkeypatch.setattr(common, "MAX_CHARS", 50)
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
    assert chunks and all(c.char_end - c.char_start <= 280 for c in chunks)


def test_upload_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", TokenBucket(2, 60))
    assert post(client, "a.txt", b"hello there").status_code == 202
    assert post(client, "a.txt", b"hello again").status_code == 202
    assert post(client, "a.txt", b"hello once more").status_code == 429


def test_upload_queue_is_bounded(client):
    tokens = [main._ingest_guard.acquire() for _ in range(main.UPLOAD_QUEUE_MAX)]
    r = post(client, "a.txt", b"hello there")
    assert r.status_code == 429 and "already being processed" in r.json()["detail"]
    for token in tokens:
        main._ingest_guard.release(token)
    assert post(client, "a.txt", b"hello there").status_code == 202
    assert not main._ingest_guard.busy  # released after the background task


def test_validation_failure_releases_guard(client):
    assert post(client, "a.pdf", b"not a pdf").status_code == 400
    assert not main._ingest_guard.busy


def test_stale_guard_is_taken_over():
    now = [0.0]
    g = main.IngestGuard(clock=lambda: now[0], capacity=1)
    t1 = g.acquire()
    assert t1 is not None and g.acquire() is None
    now[0] = main.IngestGuard.STALE_SECONDS - 1
    assert g.acquire() is None
    now[0] = main.IngestGuard.STALE_SECONDS + 1
    t2 = g.acquire()
    assert t2 is not None and t2 != t1
    g.release(t1)  # the old, stale holder must not free the new holder's guard
    assert g.busy
    g.release(t2)
    assert not g.busy


def test_guard_released_when_ingest_raises(client, monkeypatch):
    token = main._ingest_guard.acquire()

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "_set_status", boom)  # makes the error handler itself fail
    monkeypatch.setattr(ingest_upload, "process_upload", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(main._ingest("u-x-00000000", "t", "", "novel", b"hi", ".txt", token))
    assert not main._ingest_guard.busy


def test_oversized_content_length_rejected_before_endpoint(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_upload, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(ingest_upload, "MULTIPART_SLACK", 0)

    class Boom:
        def allow(self, key):
            raise AssertionError("endpoint must not be entered")

    monkeypatch.setattr(main, "upload_bucket", Boom())
    r = post(client, "a.txt", b"x" * 300)
    assert r.status_code == 413
    assert not main._ingest_guard.busy
    assert not (tmp_path / "uploads").exists()


async def drive(headers, chunks):
    """Call the ASGI app directly for POST /api/books/upload; returns (status, number of body chunks consumed)."""
    sent, consumed = [], 0
    queue = list(chunks)

    async def receive():
        nonlocal consumed
        if not queue:
            return {"type": "http.disconnect"}
        consumed += 1
        return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/api/books/upload", "raw_path": b"/api/books/upload", "query_string": b"", "root_path": "",
             "headers": [(k.encode(), v.encode()) for k, v in headers], "client": ("1.2.3.4", 1), "server": ("t", 80)}
    await main.app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), consumed


class BoomBucket:
    def allow(self, key):
        raise AssertionError("endpoint must not be entered")


def test_missing_content_length_is_411(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", BoomBucket())
    assert asyncio.run(drive([("content-type", "multipart/form-data; boundary=b")], [b"--b--"]))[0] == 411


def test_transfer_encoding_is_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", BoomBucket())
    headers = [("content-type", "multipart/form-data; boundary=b"), ("content-length", "5"),
               ("transfer-encoding", "chunked")]
    assert asyncio.run(drive(headers, [b"--b--"]))[0] == 400
    # and through the real client (chunked body from a generator)
    def body():
        yield b"--b--"
    r = client.post("/api/books/upload", content=body(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 400


def test_body_longer_than_declared_is_cut_off(client, monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_upload, "MAX_UPLOAD_BYTES", 100)
    monkeypatch.setattr(ingest_upload, "MULTIPART_SLACK", 0)
    monkeypatch.setattr(main, "upload_bucket", BoomBucket())
    headers = [("content-type", "multipart/form-data; boundary=b"), ("content-length", "10")]
    crlf = bytes((13, 10))
    opening = b'--b' + crlf + b'Content-Disposition: form-data; name="file"; filename="a.txt"' + crlf + crlf
    chunks = [opening] + [b"x" * 60] * 50  # a well-formed multipart start, then far more bytes than declared
    status, consumed = asyncio.run(drive(headers, chunks))
    assert status == 413
    assert consumed <= 3  # stopped reading right after the cap was crossed
    assert not (tmp_path / "uploads").exists()


def test_non_ascii_digit_content_length_is_not_a_500(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", BoomBucket())
    headers = [("content-type", "multipart/form-data; boundary=b"), ("content-length", "²")]
    # headers are latin-1 on the wire: ² is a superscript two, str.isdigit() is True but int() raises
    scope_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    assert "²".isdigit()
    sent = []

    async def go():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/api/books/upload", "raw_path": b"/api/books/upload", "query_string": b"", "root_path": "",
                 "headers": scope_headers, "client": ("1.2.3.4", 1), "server": ("t", 80)}
        await main.app(scope, receive, send)

    asyncio.run(go())
    assert sent[0]["status"] == 400


def test_live_holder_is_not_taken_over():
    now = [0.0]
    g = main.IngestGuard(clock=lambda: now[0], capacity=1)
    t1 = g.acquire()
    g.mark_started(t1)  # the ingest is running (possibly slow or hung)
    now[0] = main.IngestGuard.STALE_SECONDS * 3
    assert g.acquire() is None and g.acquire() is None and g.busy
    g.release(t1)
    assert g.acquire() is not None


# ---- linear-time guarantees ----

def test_punctuation_run_is_linear():
    t = time.perf_counter()
    assert sentence_spans("." * 200_000) == [(0, 200_000)]
    assert sentence_spans("?!" * 100_000 + " x") != []
    assert time.perf_counter() - t < 1.0
    t = time.perf_counter()
    pieces = ingest_upload.split_paragraph("." * 200_000)
    assert sum(len(x) for x in pieces) == 200_000 and all(len(x) <= ingest_upload.MAX_PARAGRAPH_CHARS for x in pieces)
    book = Book(id="b", title="t", author="a", kind="textbook", license="x", sections=[Section(id="s1", title="", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text="." * 200_000)], narrator=Narrator(voice="marcus"))
    chunk_paragraph(book.paragraphs[0], book)
    assert time.perf_counter() - t < 1.0


def test_merge_lines_is_linear():
    n = 100_000
    lines = [ingest_upload.Line("some wrapped words and", 72.0, 300.0, 100.0 + i, 110.0 + i, 72.0, 300.0, 0, False)
             for i in range(n)]
    t = time.perf_counter()
    paras = ingest_upload.merge_lines(lines)
    assert time.perf_counter() - t < 2.0
    assert len(paras) == 1 and paras[0].count("wrapped") == n


def test_merge_lines_dehyphenates_and_splits():
    L = ingest_upload.Line
    lines = [L("an exam-", 72, 300, 100, 110, 72, 300, 0, False), L("ple. Done.", 72, 300, 111, 121, 72, 300, 0, False),
             L("New one.", 92, 300, 122, 132, 72, 300, 0, False)]
    assert ingest_upload.merge_lines(lines) == ["an example. Done.", "New one."]


def test_absurdly_long_content_length_is_400(client, monkeypatch):
    monkeypatch.setattr(main, "upload_bucket", BoomBucket())
    headers = [("content-type", "multipart/form-data; boundary=b"), ("content-length", "9" * 13)]
    assert asyncio.run(drive(headers, [b"--b--"]))[0] == 400


def test_vertex_indexing_is_retried_after_a_failure(client, monkeypatch):
    calls = []

    async def flaky_index(book, chunks, embedder, store, on_progress=None):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("429 from Vertex")

    monkeypatch.setattr("app.gcp.indexing.index_book", flaky_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(main.settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.\n\nTwo.").json()["book_id"]
    st = client.get(f"/api/books/{book_id}/status").json()
    assert len(calls) == 3 and st["index"]["state"] == "done"
