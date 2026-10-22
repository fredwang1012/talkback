import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import ingest_upload, main
from app.config import settings
from app.ratelimit import TokenBucket
from app.ttschunks import chunk_paragraph, sentence_spans

PARA_A = ("The quick brown fox jumped over the lazy dog near the old river bank today. "
          "It was a very long and rather exciting afternoon for everyone involved here.")
PARA_B = ("A second paragraph follows with plenty of words so that the wrapping logic has "
          "something real to chew on, including an exam-\nple hyphenated word.")


def make_pdf(pages: list[list[str]], header: str | None = None, number_pages: bool = False) -> bytes:
    doc = fitz.open()
    for n, blocks in enumerate(pages, 1):
        page = doc.new_page()
        if header:
            page.insert_textbox(fitz.Rect(72, 20, 500, 50), header, fontsize=9)
        y = 80
        for text in blocks:
            page.insert_textbox(fitz.Rect(72, y, 500, y + 100), text, fontsize=11)
            y += 110
        if number_pages:
            page.insert_textbox(fitz.Rect(250, 800, 350, 830), str(n), fontsize=9)
    return doc.tobytes()


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


def post(client, name, data, kind="novel", title="My Test Book!", author="Me"):
    return client.post("/api/books/upload", files={"file": (name, data)},
                       data={"title": title, "author": author, "kind": kind})


def test_pdf_goes_to_ready(client, tmp_path):
    pdf = make_pdf([[PARA_A, PARA_B], [PARA_A, PARA_B]])
    r = post(client, "x.pdf", pdf)
    assert r.status_code == 202
    book_id = r.json()["book_id"]
    assert r.json()["status"] == "processing"
    assert book_id.startswith("u-my-test-book-") and len(book_id.rsplit("-", 1)[1]) == 8
    assert (tmp_path / "uploads" / f"{book_id}.pdf").read_bytes() == pdf
    assert client.get(f"/api/books/{book_id}/status").json() == {"status": "ready"}
    assert (tmp_path / "books" / book_id / "status.json").is_file()
    book = client.get(f"/api/books/{book_id}").json()
    assert book["license"] == "Uploaded by the user" and book["narrator"]["voice"] == "marcus"
    assert book["characters"] == [] and book["summaries"] == []
    assert len(book["paragraphs"]) >= 2
    assert all(p["tts_chunks"] and all(len(c["tts_text"]) <= 280 for c in p["tts_chunks"]) for p in book["paragraphs"])
    assert (tmp_path / "books" / book_id / "index" / "chunks.json").is_file()
    assert any(b["id"] == book_id for b in client.get("/api/books").json())


def test_txt_goes_to_ready_and_is_retrievable(client):
    txt = "First paragraph about dragons.\n\nSecond paragraph about knights.\n\nThird about castles.\n".encode()
    book_id = post(client, "story.txt", txt, kind="textbook").json()["book_id"]
    assert client.get(f"/api/books/{book_id}/status").json()["status"] == "ready"
    book = client.get(f"/api/books/{book_id}").json()
    assert [p["text"] for p in book["paragraphs"]][0] == "First paragraph about dragons."
    r = client.post("/api/retrieve", json={"book_id": book_id, "query": "knights", "reader_para": 0})
    assert r.status_code == 200


def test_novel_upload_keeps_spoiler_rule(client):
    txt = "\n\n".join(f"Paragraph number {i} mentions item{i}." for i in range(12)).encode()
    book_id = post(client, "n.txt", txt, kind="novel").json()["book_id"]
    out = client.post("/api/retrieve", json={"book_id": book_id, "query": "item11", "reader_para": 3}).json()
    assert out["passages"]
    assert all(p["para_end"] <= 3 for p in out["passages"])
    assert "item11" not in str(out["passages"])


def test_scanned_pdf_is_error(client):
    doc = fitz.open()
    doc.new_page()
    book_id = post(client, "scan.pdf", doc.tobytes()).json()["book_id"]
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["status"] == "error" and "scanned" in st["message"].lower()
    assert client.get(f"/api/books/{book_id}").status_code == 404


def test_rejections(client, monkeypatch):
    assert post(client, "x.txt", b"hello", kind="poem").status_code == 422
    assert post(client, "x.txt", b"hello", title="").status_code == 422
    assert post(client, "x.docx", b"hello").status_code == 400
    assert post(client, "x.pdf", b"not a pdf").status_code == 400
    monkeypatch.setattr(ingest_upload, "MAX_UPLOAD_BYTES", 100)
    assert post(client, "big.txt", b"x" * 101).status_code == 413
    assert client.get("/api/books/u-nope-0000/status").status_code == 404


def test_requires_access_token(client, monkeypatch):
    monkeypatch.setattr(settings, "access_token", "sekret")
    assert post(client, "x.txt", b"hello").status_code == 401


def test_cap_is_20_mb():
    assert ingest_upload.MAX_UPLOAD_BYTES == 20 * 1024 * 1024


def test_vertex_indexing_only_when_enabled(client, monkeypatch):
    calls = []

    async def fake_index_book(book, chunks, embedder, store, on_progress=None):
        calls.append((book.id, len(chunks), embedder, store))
        on_progress(len(chunks), len(chunks))
        return len(chunks)

    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index_book)
    monkeypatch.setattr(main, "_vertex_deps", lambda: ("EMB", "STORE"))
    post(client, "a.txt", b"One paragraph.")
    assert calls == []
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.").json()["book_id"]
    assert len(calls) == 1 and calls[0][0] == book_id and calls[0][2:] == ("EMB", "STORE")
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["status"] == "ready" and st["index"]["state"] == "done" and st["index"]["done"] == st["index"]["total"]
    assert main._vertex_index_ready(book_id)


def test_vertex_failure_keeps_book_readable_with_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("secret detail")

    monkeypatch.setattr("app.gcp.indexing.index_book", boom)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.").json()["book_id"]
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["status"] == "ready" and st["index"]["state"] == "error" and "secret" not in st["index"]["message"]
    assert (settings.data_dir / "books" / book_id / "book.json").is_file()
    assert not main._vertex_index_ready(book_id)  # stays on BM25


def test_book_readable_and_bm25_while_vertex_indexing(client, monkeypatch):
    import asyncio
    seen = {}

    async def slow_index(book, chunks, embedder, store, on_progress=None):
        on_progress(1, 5)
        seen["status"] = client.get(f"/api/books/{book.id}/status").json()
        seen["readable"] = client.get(f"/api/books/{book.id}").status_code
        seen["index"] = main._index_for(book.id)
        return 0

    monkeypatch.setattr("app.gcp.indexing.index_book", slow_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    book_id = post(client, "b.txt", b"One paragraph.").json()["book_id"]
    assert seen["status"]["status"] == "ready"
    assert seen["status"]["index"] == {"state": "running", "done": 1, "total": 5}
    assert seen["readable"] == 200
    assert type(seen["index"]).__name__ == "Index"  # local BM25, not VertexIndex
    assert type(main._index_for(book_id)).__name__ == "VertexIndex"  # after completion


def test_interrupted_indexing_reported_on_restart(client, monkeypatch):
    ingest_upload.write_status("u-x-00000000", "ready", index={"state": "running", "done": 3, "total": 9})
    st = client.get("/api/books/u-x-00000000/status").json()
    assert st["index"]["state"] == "error" and st["status"] == "ready"


def test_index_status_survives_enrich_and_kind_updates(client):
    ingest_upload.write_status("bk", "ready", index={"state": "running", "done": 1, "total": 2})
    assert ingest_upload.write_status("bk", "ready", enrich={"status": "running"})["index"]["done"] == 1
    assert ingest_upload.write_status("bk", "ready", kind="novel")["index"]["total"] == 2


def test_novel_enrichment_runs_after_vertex_indexing(client, monkeypatch):
    order = []

    async def fake_index(book, chunks, embedder, store, on_progress=None):
        order.append("index")
        return 0

    async def fake_plan(book_id, book):
        order.append("enrich")

    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(main, "_plan_enrichment", fake_plan)
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(settings, "upload_enrich", True)
    post(client, "n.txt", b"One paragraph.", kind="novel")
    assert order == ["index", "enrich"]


def test_status_survives_restart(client, tmp_path):
    book_id = post(client, "a.txt", b"One paragraph.").json()["book_id"]
    main._upload_status.clear()
    assert client.get(f"/api/books/{book_id}/status").json() == {"status": "ready"}


def test_make_book_dir(client, tmp_path):
    i, d = ingest_upload.make_book_dir("  Café Ünïcode: " + "x" * 80)
    assert i.startswith("u-cafe-unicode-xxx") and d.is_dir() and len(i) <= 2 + 40 + 9
    assert len(i.rsplit("-", 1)[1]) == 8
    assert ingest_upload.make_book_dir("!!!")[0].startswith("u-book-")


def test_make_book_dir_retries_on_collision(client, monkeypatch, tmp_path):
    seq = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(ingest_upload.secrets, "token_hex", lambda n: next(seq))
    assert ingest_upload.make_book_dir("t")[0] == "u-t-aaaaaaaa"
    assert ingest_upload.make_book_dir("t")[0] == "u-t-bbbbbbbb"


# ---- PDF extraction heuristics ----

def test_pdf_joins_wrapped_lines_and_dehyphenates():
    text = ingest_upload.pdf_to_text(make_pdf([[PARA_A, PARA_B]]))
    paras = text.split("\n\n")
    assert paras[0] == PARA_A and "\n" not in paras[0]
    assert "example" in paras[1] and "exam-" not in paras[1]


def test_pdf_drops_headers_footers_and_page_numbers():
    pages = [[PARA_A + f" Page {i}."] for i in range(5)]
    text = ingest_upload.pdf_to_text(make_pdf(pages, header="RUNNING TITLE OF THE BOOK", number_pages=True))
    assert "RUNNING TITLE" not in text
    assert not any(p.strip().isdigit() for p in text.split("\n\n"))
    assert text.count("quick brown fox") == 5


def test_pdf_joins_paragraph_across_pages():
    first = "This sentence runs on and does not stop at the end of the page so it"
    second = "continues at the top of the next page and ends here."
    text = ingest_upload.pdf_to_text(make_pdf([[PARA_A, first], [second, PARA_B]]))
    assert any(first in p and second in p for p in text.split("\n\n"))


def test_estimate_prices_gemini_when_claude_has_no_key(monkeypatch):
    from app.config import settings
    from app.enrich import llm as enrich_llm, pipeline
    from app.schema import Book
    book = Book(id="b", title="b", author="a", paragraphs=[], sections=[], characters=[], kind="novel", license="x")
    monkeypatch.setattr(settings, "upload_llm", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    seen = []
    real = pipeline.estimate_usd
    monkeypatch.setattr(pipeline, "estimate_usd", lambda prov, *a: (seen.append(prov), real(prov, *a))[1])
    assert enrich_llm.effective_provider() == "gemini"
    pipeline.estimate(book)
    assert set(seen) == {"gemini"}
    seen.clear()
    monkeypatch.setattr(settings, "anthropic_api_key", "k")
    assert enrich_llm.effective_provider() == "claude"
    pipeline.estimate(book)
    assert set(seen) == {"claude"}


def test_vertex_indexing_runs_one_job_at_a_time(client, monkeypatch):
    """Uploads release the upload guard once published, so indexing itself must be serialized (cost control)."""
    import asyncio
    state = {"now": 0, "max": 0}

    async def fake_index(book, chunks, embedder, store, on_progress=None):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return 0

    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index)
    monkeypatch.setattr(main, "_vertex_deps", lambda: (None, None))
    books = [ingest_upload.build_book(f"u-b{i}-0000000{i}", "T", "A", "textbook", "One paragraph.") for i in range(3)]

    async def run():
        monkeypatch.setattr(main, "_index_lock", asyncio.Lock())  # bound to this test's event loop
        await asyncio.gather(*(main._index_in_background(b, []) for b in books))

    asyncio.run(run())
    assert state["max"] == 1
