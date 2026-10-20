"""Full-book ("spoilers on") mode: opt-in, novels only; the default stays spoiler-safe."""
import asyncio

from app import main, retrieval
from app.gcp.vectorstore import spoiler_filter
from app.gcp.vertex_index import VertexIndex
from app.main import RetrieveRequest
from app.personas import build_instructions
from app.schema import Character
from tests.test_retrieval import make_book
from tests.test_series import series_book
from tests.test_vertex_index import FakeEmbedder, FakeStore, row
from tests.test_api import env  # noqa: F401  (env fixture)
from tests.test_answer import FakeAnswerer, post


def test_request_default_is_spoiler_safe():
    assert RetrieveRequest(book_id="b", reader_para=1).spoilers is False


def test_full_mode_returns_chunks_beyond_reader_and_last_summary():
    book = make_book(60)
    idx = retrieval.Index(retrieval.make_chunks(book))
    safe = retrieval.retrieve(book, idx, "secretword55", 10)
    assert all(p["para_end"] <= 10 for p in safe["passages"]) and safe["story_so_far"] == "summary up to 9"
    full = retrieval.retrieve(book, idx, "secretword55", 10, spoilers=True)
    assert any(p["para_end"] > 10 for p in full["passages"])
    assert full["story_so_far"] == "Whole book:\nsummary up to 49"


def test_full_mode_story_includes_earlier_recap_and_earlier_chunks():
    book = series_book(3, summaries=[])
    book.summaries = make_book().summaries
    found = [retrieval.Chunk(id="r0", para_start=0, para_end=2, text="old", book_id="s1", book_title="Book 1"),
             retrieval.Chunk(id="r9", para_start=0, para_end=2, text="later book", book_id="s4"),
             retrieval.Chunk(id="r30", para_start=30, para_end=32, text="future")]
    out = retrieval._finish(book, retrieval.Index([]), found, 10, False, frozenset({"s1"}), "Book 1: recap",
                            full_book=True)
    texts = [p["text"] for p in out["passages"]]
    assert "old" in texts and "future" in texts and "later book" not in texts
    assert out["story_so_far"] == "Earlier books:\nBook 1: recap\n\nWhole book:\nsummary up to 49"


def test_textbook_ignores_spoilers_flag():
    book = make_book(60, kind="textbook")
    idx = retrieval.Index(retrieval.make_chunks(book))
    a = retrieval.retrieve(book, idx, "secretword5", 30)
    b = retrieval.retrieve(book, idx, "secretword5", 30, spoilers=True)
    assert a == b and b["story_so_far"] == ""
    assert retrieval._clamp(book, 5, True)[0] is False
    assert retrieval._clamp(make_book(), 5, True)[0] is False and retrieval._clamp(make_book(), 5)[0] is True


def test_aretrieve_full_mode_with_local_index():
    book = make_book(60)
    idx = retrieval.Index(retrieval.make_chunks(book))
    out = asyncio.run(retrieval.aretrieve(book, idx, "secretword55", 10, spoilers=True))
    assert any(p["para_end"] > 10 for p in out["passages"])


def test_spoiler_filter_full_mode():
    assert spoiler_filter("b3", None, ["b1", "b2"]) == {"$or": [
        {"book_id": {"$in": ["b1", "b2"]}}, {"book_id": {"$eq": "b3"}}]}
    assert spoiler_filter("b", None) == {"book_id": {"$eq": "b"}}
    assert spoiler_filter("b", None, []) == {"book_id": {"$eq": "b"}}


def test_vertex_full_mode_keeps_later_paragraphs_and_earlier_books():
    store = FakeStore([row(50, "s1"), row(30, "s3"), row(0, "s9")])
    idx = VertexIndex(series_book(3), FakeEmbedder(), store, {"s1": "Book 1"})
    out = asyncio.run(idx.asearch("q", reader_para=10, k=8, spoiler_safe=False))
    got = [(c.book_id, c.id) for c in out]
    assert ("s1", "r50") in got and ("", "r30") in got and all(b != "s9" for b, _ in got)
    assert store.calls[0][1] == {"$or": [{"book_id": {"$in": ["s1"]}}, {"book_id": {"$eq": "s3"}}]}


def test_prompt_variants():
    book = make_book(10)
    book.characters = [Character(id="c", name="Cee", first_para=0, voice="v", persona="Wry.")]
    for sp in ("narrator", "c"):
        safe = build_instructions(book, sp, "story", [])
        assert safe == build_instructions(book, sp, "story", [], spoilers=False)
        assert "Never reveal, hint" in safe
        assert "Never state or deny an outcome" in safe and "has not revealed it yet" in safe
        full = build_instructions(book, sp, "story", [], spoilers=True)
        assert "chosen to allow spoilers" in full and "Never reveal, hint" not in full and "Never state or deny an outcome" not in full and "story" in full
    tb = make_book(10, kind="textbook")
    assert build_instructions(tb, "narrator", "", [], spoilers=True) == build_instructions(tb, "narrator", "", [])


def test_endpoints_thread_spoilers(env, monkeypatch):
    client, _ = env
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "King", "reader_para": 0, "speaker_id": "narrator"})
    assert "Later the King" not in r.text and "chosen to allow spoilers" not in r.json()["instructions"]
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "King", "reader_para": 0,
                                           "speaker_id": "narrator", "spoilers": True})
    j = r.json()
    assert "chosen to allow spoilers" in j["instructions"] and any("King" in p["text"] for p in j["passages"])
    fake = FakeAnswerer(["Ok."])
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    post(client, speaker_id="narrator", reader_para=0, query="King", spoilers=True)
    assert "chosen to allow spoilers" in fake.prompts[0][0]


def test_full_book_mode_unlocks_unmet_characters(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "get_answerer", lambda: FakeAnswerer(["Ok."]))
    assert post(client, speaker_id="king", reader_para=1).status_code == 403
    assert post(client, speaker_id="king", reader_para=1, spoilers=True).status_code == 200
    body = {"book_id": "demo", "query": "x", "reader_para": 1, "speaker_id": "king"}
    assert client.post("/api/retrieve", json=body).status_code == 403
    assert client.post("/api/retrieve", json={**body, "spoilers": True}).status_code == 200
    assert post(client, speaker_id="ghost", spoilers=True).status_code == 404  # unknown ids still 404


def test_realtime_retrieve_is_capped_below_answer(env):
    client, _ = env
    j = client.post("/api/retrieve", json={"book_id": "demo", "query": "x", "reader_para": 1, "k": 12}).json()
    assert sum(len(p["text"]) for p in j["passages"]) <= retrieval.REALTIME_MAX_PASSAGE_CHARS
    assert len(j["passages"]) <= retrieval.REALTIME_K + 1  # + the "what just happened" passage
    assert len(j["instructions"]) <= 8000


def test_quality_rules_in_every_prompt():
    book = make_book(10)
    book.characters = [Character(id="c", name="Cee", first_para=0, voice="v", persona="Wry.")]
    for sp in ("narrator", "c"):
        for spoilers in (False, True):
            t = build_instructions(book, sp, "story", [], spoilers=spoilers)
            assert "specifically and in detail" in t and "never invent facts" in t
            assert ("never confirm, deny, reassure or tease" in t) == (not spoilers)
