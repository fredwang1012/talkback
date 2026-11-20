"""Series support: earlier books are fully known to the reader; the current book stays spoiler-gated."""
import asyncio
import importlib.util
from pathlib import Path

from app import books, retrieval
from app.config import settings
from app.gcp.vectorstore import spoiler_filter
from app.gcp.vertex_index import VertexIndex
from app.personas import format_passages
from app.schema import Book, Paragraph, Section, Summary
from tests.test_vertex_index import FakeEmbedder, FakeStore, row


def series_book(i: int, n: int = 40, summaries=()) -> Book:
    return Book(id=f"s{i}", title=f"Book {i}", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="One", start_para=0)],
                paragraphs=[Paragraph(idx=j, section_id="s1", text=f"Book {i} paragraph {j}.") for j in range(n)],
                summaries=list(summaries), series="saga", series_index=i)


def write(tmp_path, book: Book):
    d = tmp_path / "books" / book.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "book.json").write_text(book.model_dump_json(), encoding="utf-8")


def test_earlier_in_series_and_recap(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    write(tmp_path, series_book(1, summaries=[Summary(upto_para=10, text="early"), Summary(upto_para=39, text="end of one")]))
    write(tmp_path, series_book(2, summaries=[Summary(upto_para=39, text="end of two " + "w " * 300)]))
    write(tmp_path, series_book(3))
    b3 = books.load_book("s3")
    assert [b.id for b in books.earlier_in_series(b3)] == ["s1", "s2"]
    assert books.earlier_in_series(books.load_book("s1")) == []
    recap = books.series_recap(b3, max_words=5)
    assert recap.splitlines()[0] == "Book 1: end of one"
    assert recap.splitlines()[1].endswith("…") and len(recap.splitlines()[1].split()) <= 8
    assert [b["id"] for b in books.list_books()] == ["s1", "s2", "s3"]


def test_series_recap_ends_on_a_whole_sentence_and_full_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    long = "First sentence here. Second one follows! " + "Third sentence has many many words in it. " * 10
    write(tmp_path, series_book(1, summaries=[Summary(upto_para=39, text=long)]))
    write(tmp_path, series_book(2))
    b2 = books.load_book("s2")
    out = books.series_recap(b2, max_words=20).split(": ", 1)[1]
    assert out.endswith((".", "!")) and "…" not in out and len(out.split()) <= 20
    assert books.series_recap(b2, max_words=20, full=True).split(": ", 1)[1] == long.strip()
    assert books.series_recap(b2).split(": ", 1)[1].endswith(".")  # default limit also ends on a sentence


def test_trim_to_sentence_edge_cases():
    assert books.trim_to_sentence("Short one.", 10) == "Short one."
    assert books.trim_to_sentence("One two three four five six seven", 3) == "One two three…"
    assert books.trim_to_sentence('He said "go." Then he left and kept walking on', 5) == 'He said "go."'


def test_load_book_cache_refreshes_when_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    write(tmp_path, series_book(1, n=3))
    assert books.load_book("s1") is books.load_book("s1")
    write(tmp_path, series_book(1, n=5))
    assert len(books.load_book("s1").paragraphs) == 5


def test_finish_keeps_earlier_books_and_gates_the_current_one():
    book = series_book(3)
    found = [retrieval.Chunk(id="r0", para_start=0, para_end=2, text="old", book_id="s1", book_title="Book 1"),
             retrieval.Chunk(id="r9", para_start=0, para_end=2, text="later book", book_id="s4", book_title="Book 4"),
             retrieval.Chunk(id="r1", para_start=3, para_end=5, text="now"),
             retrieval.Chunk(id="r2", para_start=30, para_end=32, text="future")]
    idx = retrieval.Index([])
    out = retrieval._finish(book, idx, found, 10, True, frozenset({"s1", "s2"}), "Book 1: recap")
    texts = [p["text"] for p in out["passages"]]
    assert "old" in texts and "now" in texts
    assert "later book" not in texts and "future" not in texts
    assert out["passages"][0]["book"] == "Book 1"
    assert out["story_so_far"].startswith("Earlier books:\nBook 1: recap")
    # textbooks / non-spoiler-safe: never other books
    out = retrieval._finish(book, idx, found, 10, False, frozenset({"s1"}))
    assert all("book" not in p for p in out["passages"])


def test_spoiler_filter_with_earlier_books():
    assert spoiler_filter("b3", 10, ["b1", "b2"]) == {"$or": [
        {"book_id": {"$in": ["b1", "b2"]}},
        {"$and": [{"book_id": {"$eq": "b3"}}, {"para_end": {"$lte": 10}}]}]}
    assert spoiler_filter("b3", 10, []) == {"$and": [{"book_id": {"$eq": "b3"}}, {"para_end": {"$lte": 10}}]}


def test_vertex_index_post_filter_with_earlier_books():
    store = FakeStore([row(50, "s1"), row(0, "s2"), row(3, "s3"), row(30, "s3"), row(0, "s9")])
    idx = VertexIndex(series_book(3), FakeEmbedder(), store, {"s1": "Book 1", "s2": "Book 2"})
    out = asyncio.run(idx.asearch("q", reader_para=10, k=8))
    got = [(c.book_id, c.id) for c in out]
    assert ("s1", "r50") in got and ("s2", "r0") in got and ("", "r3") in got
    assert ("", "r30") not in got and all(b != "s9" for b, _ in got)
    assert got[-1] == ("", "r3")  # earlier books first
    assert store.calls[0][1]["$or"][0] == {"book_id": {"$in": ["s1", "s2"]}}


def test_vertex_index_fuses_same_chunk_id_from_different_books_separately():
    store = FakeStore([row(0, "s1"), row(0, "s3")])
    idx = VertexIndex(series_book(3), FakeEmbedder(), store, {"s1": "Book 1"})
    out = asyncio.run(idx.asearch("q", reader_para=10, k=8))
    assert len(out) == 2


def test_format_passages_labels_other_books():
    s = format_passages([{"para_start": 0, "para_end": 1, "text": "x", "book": "Book 1"},
                         {"para_start": 4, "para_end": 4, "text": "y"}])
    assert s == "[Book 1, ¶1-2] x\n\n[¶5-5] y"


def _ingest_series():
    path = Path(__file__).resolve().parents[2] / "preprocess" / "ingest_series.py"
    spec = importlib.util.spec_from_file_location("ingest_series", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plan_books_from_toc():
    toc = [[1, "Title Page", 2], [1, "Harry Potter and the Sorcerer�s Stone", 7],
           [2, "Chapter 1 - The Boy Who Lived", 12], [2, "Chapter 2 - The Vanishing Glass", 26],
           [2, "Copyright page", 275], [1, "Harry Potter and the Chamber of Secrets", 276],
           [2, "Chapter 1 - The Worst Birthday", 282], [2, "Epilogue", 300], [2, "Copyright page", 310],
           [1, "Some Other Book", 311]]
    plan = _ingest_series().plan_books(toc)
    assert [b["id"] for b in plan] == ["hp1-sorcerers-stone", "hp2-chamber-of-secrets"]
    assert plan[0]["chapters"] == [("Chapter 1: The Boy Who Lived", 12, 25), ("Chapter 2: The Vanishing Glass", 26, 274)]
    assert plan[1]["chapters"] == [("Chapter 1: The Worst Birthday", 282, 299), ("Epilogue", 300, 309)]
    assert [b["index"] for b in plan] == [1, 2]


class FakePage:
    """A pdfdoc.Page with one block per line."""
    def __init__(self, lines):
        self._lines = lines

    def text_blocks(self):
        return [{"type": 0, "lines": [{"spans": [{"text": t, "size": size, "font": font}],
                                       "bbox": (72, 100 + 20 * i, 500, 114 + 20 * i)}]}
                for i, (t, size, font) in enumerate(self._lines)]


def test_page_lines_drops_headings_and_rejoins_drop_cap():
    page = FakePage([("M", 49.0, "Serif-Bold"), (" ", 14.4, "Serif"), ("CHAPTER  ONE", 10.8, "Serif-Bold"),
                     ("THE BOY WHO LIVED", 20.2, "Serif-BoldItal"), ("r. and Mrs. Smith lived here.", 14.4, "Serif"),
                     ("12", 14.4, "Serif")])
    mod = _ingest_series()
    assert [t for t, *_ in mod.page_lines(page, first=True)] == ["Mr. and Mrs. Smith lived here."]
    assert [t for t, *_ in mod.page_lines(FakePage([("Plain text.", 14.4, "Serif")]), first=False)] == ["Plain text."]


def test_series_index_sees_new_books_after_save(tmp_path, monkeypatch):
    from app import books
    from app.config import settings
    from app.schema import Book
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    books._series_index = None

    def put(bid, idx):
        d = tmp_path / "books" / bid
        d.mkdir(parents=True, exist_ok=True)
        b = Book(id=bid, title=bid, author="a", paragraphs=[], sections=[], characters=[], kind="novel", license="x", series="S", series_index=idx)
        books.save_book(d / "book.json", b)
        return b
    put("one", 1)
    two = put("two", 2)
    assert [b.id for b in books.earlier_in_series(two)] == ["one"]
    put("zero", 0)  # saved after the index was built
    assert [b.id for b in books.earlier_in_series(two)] == ["zero", "one"]
