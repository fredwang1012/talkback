import asyncio

import numpy as np

from app.gcp import indexing
from app.gcp.indexing import build_objects, index_book
from app.retrieval import Chunk
from app.schema import Book, Paragraph, Section, Segment


def _book():
    paras = [
        Paragraph(idx=0, section_id="s0", text="a", segments=[Segment(speaker="narrator", text="a")]),
        Paragraph(idx=1, section_id="s0", text="b", segments=[Segment(speaker="ann", text="b")]),
        Paragraph(idx=2, section_id="s1", text="c", segments=[Segment(speaker="ann", text="c")]),
        Paragraph(idx=3, section_id="s1", text="d"),
    ]
    return Book(id="bk", title="T", author="A", kind="novel", license="PD",
                sections=[Section(id="s0", title="One", start_para=0), Section(id="s1", title="Two", start_para=2)],
                paragraphs=paras)


CHUNKS = [Chunk("r0", 0, 1, "a\nb"), Chunk("r1", 1, 2, "b\nc", context_blurb="blurb"), Chunk("r2", 3, 3, "d")]


def test_build_objects_shape_and_mapping():
    emb = np.ones((3, 4), dtype=np.float32)
    objs = build_objects(_book(), CHUNKS, emb)
    assert [o["id"] for o in objs] == ["bk__r0", "bk__r1", "bk__r2"]
    assert objs[0]["data"] == {"book_id": "bk", "chunk_id": "r0", "section": 0, "para_start": 0, "para_end": 1,
                               "speaker": "narrator", "text": "a\nb"}
    assert objs[0]["embedding"] == [1.0] * 4
    assert objs[1]["data"]["section"] == 0
    assert objs[1]["data"]["speaker"] == "ann"
    assert objs[2]["data"]["section"] == 1 and objs[2]["data"]["speaker"] == "narrator"


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(texts)
        return np.zeros((len(texts), 4), dtype=np.float32)


class FakeStore:
    def __init__(self):
        self.batches = []

    async def upsert(self, objects):
        self.batches.append(objects)


def test_index_book_embeds_with_blurb_and_upserts():
    emb, store = FakeEmbedder(), FakeStore()
    n = asyncio.run(index_book(_book(), CHUNKS, emb, store))
    assert n == 3
    assert emb.calls[0][1] == "blurb\nb\nc" and emb.calls[0][0] == "a\nb"
    assert sum(len(b) for b in store.batches) == 3


def test_index_book_empty_makes_no_calls():
    emb, store = FakeEmbedder(), FakeStore()
    assert asyncio.run(index_book(_book(), [], emb, store)) == 0
    assert emb.calls == [] and store.batches == []


def test_load_chunks_falls_back_to_make_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(indexing.settings, "data_dir", tmp_path)
    assert [c.id for c in indexing.load_chunks(_book())][0] == "r0"
    d = tmp_path / "books" / "bk" / "index"
    d.mkdir(parents=True)
    (d / "chunks.json").write_text('[{"id":"x","para_start":0,"para_end":0,"text":"t","context_blurb":""}]')
    assert [c.id for c in indexing.load_chunks(_book())] == ["x"]


def test_script_dry_run_makes_no_calls(monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("index_vertex", Path(__file__).resolve().parents[2] / "scripts" / "index_vertex.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "load_book", lambda _id: _book())
    monkeypatch.setattr(mod, "load_chunks", lambda b: CHUNKS)

    async def boom(*a, **k):
        raise AssertionError("index_book called")

    monkeypatch.setattr(mod, "index_book", boom)
    monkeypatch.setattr(sys, "argv", ["index_vertex.py", "--book", "bk", "--dry-run"])
    asyncio.run(mod.main())
    out = capsys.readouterr().out
    assert "3 chunks" in out and "estimate" in out


def test_index_book_runs_batches_concurrently_in_order_with_progress():
    import threading
    import time

    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    class SlowEmbedder:
        def embed_documents(self, texts):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return np.array([[float(t.split("-")[1])] * 4 for t in texts], dtype=np.float32)

    stored = []

    class Store:
        async def upsert(self, objects):
            stored.extend(objects)

    n = indexing.EMBED_BATCH * 7 + 5
    chunks = [Chunk(f"c-{i}", 0, 0, f"t-{i}") for i in range(n)]
    progress = []
    assert asyncio.run(index_book(_book(), chunks, SlowEmbedder(), Store(), lambda d, t: progress.append((d, t)))) == n
    assert 2 <= state["peak"] <= indexing.EMBED_CONCURRENCY
    assert len(stored) == n
    assert all(o["embedding"][0] == float(o["data"]["chunk_id"].split("-")[1]) for o in stored)  # right vector per chunk
    assert progress[-1] == (n, n) and [d for d, _ in progress] == sorted(d for d, _ in progress)


def test_index_book_failure_raises_and_cancels():
    class Bad:
        def embed_documents(self, texts):
            raise RuntimeError("boom")

    chunks = [Chunk(f"c-{i}", 0, 0, "t") for i in range(300)]
    try:
        asyncio.run(index_book(_book(), chunks, Bad(), FakeStore()))
    except RuntimeError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("expected failure")
