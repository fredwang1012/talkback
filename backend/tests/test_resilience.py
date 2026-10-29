"""Transient failures during an upload's enrichment (seen on Windows with Gemini on Vertex): network errors are retried
with backoff + jitter, a failed Vertex (re)index no longer fails the enrichment, a Windows PermissionError on os.replace
is retried and a failed checkpoint is retried at the next stage, skipped recap notes are retried and counted.
Fakes only: no network, no credit."""
import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from app import books, indexes, ingest_upload, processing, retrying
from app.config import settings
from app.enrich import pipeline
from app.enrich import summaries as sum_mod
from app.enrich.llm import LLM, GeminiProvider
from tests.test_novel_pipeline import FakeProvider, client, data_dir, make_book_on_disk, make_llm, run_pipeline, saved  # noqa: F401

BID = "u-test-aaaaaaaa"


class _ApiError(Exception):
    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.code = code


async def _no_sleep(_):
    pass


# ---------- the retry policy ----------

def test_transient_errors_are_network_429_and_5xx_never_quota_or_auth():
    for e in (httpx.ReadError("r"), httpx.ConnectError("c"), httpx.RemoteProtocolError("p"), httpx.ReadTimeout("t"),
              ConnectionResetError(), asyncio.TimeoutError(), _ApiError(429), _ApiError(503), _ApiError(500)):
        assert retrying.is_transient(e), type(e).__name__
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = httpx.ReadError("r")
    assert retrying.is_transient(wrapped)
    for e in (_ApiError(401), _ApiError(403), _ApiError(400), _ApiError(429, "insufficient_quota for project"),
              _ApiError(402, "billing"), ValueError("x"), KeyError("k")):
        assert not retrying.is_transient(e), type(e).__name__


def test_backoff_is_exponential_capped_and_jittered():
    delays = [retrying.backoff(a, base=0.5, cap=4.0) for a in range(6)]
    for a, d in enumerate(delays):
        full = min(4.0, 0.5 * 2 ** a)
        assert full / 2 <= d <= full
    assert max(delays) <= 4.0


def test_retry_async_bounded_then_success_and_persistent_failure_raises_last_error():
    calls, sleeps = [], []

    async def sleep(s):
        sleeps.append(s)

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadError("r")
        return "ok"
    assert asyncio.run(retrying.retry_async(flaky, what="t", sleep=sleep)) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2 and all(s > 0 for s in sleeps)

    calls.clear()

    async def dead():
        calls.append(1)
        raise httpx.ConnectError("c")
    with pytest.raises(httpx.ConnectError):
        asyncio.run(retrying.retry_async(dead, what="t", attempts=3, sleep=sleep))
    assert len(calls) == 3

    calls.clear()

    async def forbidden():
        calls.append(1)
        raise _ApiError(403)
    with pytest.raises(_ApiError):
        asyncio.run(retrying.retry_async(forbidden, what="t", sleep=sleep))
    assert len(calls) == 1   # auth is never retried


def test_retry_sync_logs_only_the_exception_type(caplog):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ReadError("secret host name")
        return 1
    with caplog.at_level("WARNING", logger="talkback.retry"):
        assert retrying.retry_sync(flaky, what="embed", sleep=lambda s: None) == 1
    assert any("ReadError" in r.getMessage() for r in caplog.records)
    assert not any("secret" in r.getMessage() for r in caplog.records)


# ---------- Gemini calls (enrichment, embeddings, answers, translation) ----------

def _gemini_client(fails: list, text='{"summary":"hi"}'):
    seen = []

    class Resp:
        usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0)
        candidates = [SimpleNamespace(finish_reason="STOP")]

        def __init__(self):
            self.text = text

    class Models:
        async def generate_content(self, model, contents, config):
            seen.append(contents)
            if fails:
                raise fails.pop(0)
            return Resp()
    return SimpleNamespace(aio=SimpleNamespace(models=Models())), seen


def test_gemini_provider_retries_network_errors_and_429_then_succeeds():
    client_, seen = _gemini_client([httpx.ReadError("r"), httpx.RemoteProtocolError("p"), _ApiError(429)])
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
    llm = LLM("gemini", {"gemini": GeminiProvider(client_, sleep=sleep)})
    out = asyncio.run(llm.complete_json("sys", "usr", sum_mod.NoteResult))
    assert out.summary == "hi" and len(seen) == 4 and len(sleeps) == 3 and all(s > 0 for s in sleeps)


def test_gemini_provider_gives_up_after_bounded_attempts_and_never_retries_auth():
    client_, seen = _gemini_client([httpx.ReadError("r")] * 20)
    llm = LLM("gemini", {"gemini": GeminiProvider(client_, sleep=_no_sleep)})
    with pytest.raises(httpx.ReadError):
        asyncio.run(llm.complete_json("sys", "usr", sum_mod.NoteResult))
    assert len(seen) == GeminiProvider.RETRIES

    client_, seen = _gemini_client([_ApiError(403, "forbidden")] * 3)
    llm = LLM("gemini", {"gemini": GeminiProvider(client_, sleep=_no_sleep)})
    with pytest.raises(_ApiError):
        asyncio.run(llm.complete_json("sys", "usr", sum_mod.NoteResult))
    assert len(seen) == 1


def test_embedder_retries_network_errors_with_jitter():
    from tests.test_gcp_core import make_embedder
    emb, models = make_embedder()
    real, fails = models.embed_content, [httpx.ReadError("r"), _ApiError(503)]

    def flaky(model, contents, config):
        if fails:
            raise fails.pop(0)
        return real(model, contents, config)
    models.embed_content = flaky
    sleeps = []
    emb._sleep = sleeps.append
    assert emb.embed_query("hi").shape == (768,)
    assert len(sleeps) == 2 and all(0 < s <= retrying.CAP_S for s in sleeps)


def test_vectorstore_retries_transport_errors_then_raises_them():
    from tests.test_vectorstore import make, run
    n = {"c": 0}

    def flaky(req):
        n["c"] += 1
        if n["c"] < 3:
            raise httpx.ReadError("r", request=req)
        return httpx.Response(200, json={"results": []})
    sleeps = []
    assert run(make(flaky, sleeps).vector_search([1.0], {}, 1)) == []
    assert n["c"] == 3 and len(sleeps) == 2 and all(s > 0 for s in sleeps)

    n["c"] = 0

    def dead(req):
        n["c"] += 1
        raise httpx.ConnectError("c", request=req)
    with pytest.raises(httpx.ConnectError):
        run(make(dead, []).upsert([{"id": "x", "data": {}, "embedding": [1.0]}]))
    assert n["c"] == 4   # MAX_RETRIES + 1 attempts, then the error surfaces


def test_answerer_retries_before_the_first_token_only():
    from tests.test_web_search import chunk, run_answerer
    from app.gcp import llm as gllm

    class Models:
        def __init__(self, fail_starts=0, fail_mid=False):
            self.fail_starts, self.fail_mid, self.calls = fail_starts, fail_mid, 0

        async def generate_content_stream(self, model, contents, config):
            self.calls += 1
            if self.fail_starts:
                self.fail_starts -= 1
                raise httpx.ReadError("r")

            async def gen():
                yield chunk("First sentence. ")
                if self.fail_mid:
                    raise httpx.ReadError("mid")
                yield chunk("Second.")
            return gen()

    models = Models(fail_starts=2)
    gllm.GeminiAnswerer._sleep = staticmethod(_no_sleep)
    assert run_answerer(models) == ["First sentence.", "Second."]
    assert models.calls == 3

    models = Models(fail_mid=True)
    with pytest.raises(httpx.ReadError):
        run_answerer(models)
    assert models.calls == 1   # a stream that broke after its first token is not replayed (it was already spoken)


def test_translate_retries_a_transient_error_within_its_timeout(monkeypatch):
    from app import multilingual
    fails = [httpx.ReadError("r")]
    calls = []

    class Models:
        async def generate_content(self, model, contents, config):
            calls.append(contents)
            if fails:
                raise fails.pop(0)
            return SimpleNamespace(text="Who is Harry?", usage_metadata=None)
    monkeypatch.setattr(settings, "gcp_project", "p")
    monkeypatch.setattr(multilingual, "_client", SimpleNamespace(aio=SimpleNamespace(models=Models())))
    monkeypatch.setattr(multilingual, "RETRY_BASE_S", 0.001)
    assert asyncio.run(asyncio.wait_for(multilingual.translate("¿Quién es Harry?"), multilingual.TRANSLATE_TIMEOUT_S)) == "Who is Harry?"
    assert len(calls) == 2


# ---------- summaries: skipped notes are retried, then counted ----------

class _FlakyNotes(FakeProvider):
    """The first `fail_first` NoteResult calls fail with a network error; `dead` users fail forever."""

    def __init__(self, fail_first=0, dead=()):
        super().__init__()
        self.fail_first, self.dead, self.note_attempts = fail_first, tuple(dead), 0

    async def complete(self, system, user, schema, *a):
        if schema["title"] == "NoteResult":
            self.note_attempts += 1
            if any(d in user for d in self.dead):
                raise httpx.ReadError("dead")
            if self.fail_first:
                self.fail_first -= 1
                raise httpx.ReadError("flaky")
        return await super().complete(system, user, schema, *a)


def test_skipped_notes_get_a_second_pass(data_dir):
    book = make_book_on_disk(data_dir)
    notes, _, _ = sum_mod.estimate_calls(book)
    fake = _FlakyNotes(fail_first=2)
    stats = {}
    out = asyncio.run(sum_mod.build_summaries(book, make_llm(fake), stats=stats))
    assert len(out) == notes and stats["skipped"] == 0
    assert fake.note_attempts == notes + 2 and fake.calls.count("NoteResult") == notes


def test_persistently_skipped_notes_still_finish_and_are_counted(data_dir):
    make_book_on_disk(data_dir)
    ok, events, _ = run_pipeline(BID, _FlakyNotes(dead=("m1x",)))   # the first chapter's notes never come
    assert ok and events[-1]["status"] == "done"
    assert "could not be written" in events[-1]["message"] and events[-1]["notes_skipped"] >= 1
    book = saved(data_dir)
    assert book.characters and book.summaries


# ---------- the index stage: a failed Vertex (re)index never fails the enrichment ----------

def test_index_failure_finishes_the_enrichment_with_basic_search(data_dir):
    make_book_on_disk(data_dir)

    async def broken_indexer(book):
        raise httpx.ReadError("vertex down")
    ok, events, _ = run_pipeline(BID, indexer=broken_indexer)
    assert ok and events[-1]["status"] == "done" and events[-1]["stage"] == "done"
    assert events[-1]["message"] == pipeline.INDEX_PENDING and events[-1]["index_failed"] is True
    book = saved(data_dir)
    assert [c.id for c in book.characters] == ["alice", "bob"] and book.summaries


def test_enrich_indexer_records_an_index_error_for_the_startup_retry(client, monkeypatch):  # noqa: F811
    book = make_book_on_disk(settings.data_dir)
    ingest_upload.write_status(BID, "ready", index={"state": "error", "done": 0, "total": 3})
    processing._upload_status.clear()
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(indexes, "_vertex_deps", lambda: (None, None))

    async def dead_index(book, chunks, embedder, store, on_progress=None):
        raise httpx.ReadError("r")
    monkeypatch.setattr("app.gcp.indexing.index_book", dead_index)
    with pytest.raises(httpx.ReadError):
        asyncio.run(processing._enrich_indexer(book))
    idx = ingest_upload.read_status(BID)["index"]
    assert idx["state"] == "error" and "basic search" in idx["message"]
    assert not indexes._vertex_index_ready(BID)
    assert BID not in processing._index_jobs


def test_enrich_indexer_keeps_a_complete_index(client, monkeypatch):  # noqa: F811
    """The upload indexed every chunk: no re-embed and rewrite just for the unused speaker field."""
    book = make_book_on_disk(settings.data_dir)
    ingest_upload.write_status(BID, "ready", index={"state": "done", "done": 3, "total": 3})
    processing._upload_status.clear()
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(indexes, "_vertex_deps", lambda: (None, None))

    async def must_not_run(*a, **k):
        raise AssertionError("re-indexed a complete index")
    monkeypatch.setattr("app.gcp.indexing.index_book", must_not_run)
    asyncio.run(processing._enrich_indexer(book))
    assert ingest_upload.read_status(BID)["index"]["state"] == "done"


def test_enrich_indexer_retries_a_failed_reindex(client, monkeypatch):  # noqa: F811
    book = make_book_on_disk(settings.data_dir)
    ingest_upload.write_status(BID, "ready", index={"state": "error", "done": 0, "total": 3})
    processing._upload_status.clear()
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(indexes, "_vertex_deps", lambda: (None, None))
    calls = []

    async def flaky_index(book, chunks, embedder, store, on_progress=None):
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ReadError("429")
    monkeypatch.setattr("app.gcp.indexing.index_book", flaky_index)
    asyncio.run(processing._enrich_indexer(book))
    assert len(calls) == 2 and ingest_upload.read_status(BID)["index"]["state"] == "done"


def test_startup_retries_failed_indexes_once_and_bounded(client, monkeypatch):  # noqa: F811
    ids = [f"u-test-{i:08d}" for i in range(3)]
    for bid in ids:
        make_book_on_disk(settings.data_dir, book_id=bid)
        ingest_upload.write_status(bid, "ready", index={"state": "error", "done": 0, "total": 3, "message": "x"})
    make_book_on_disk(settings.data_dir, book_id="u-test-ffffffff")
    ingest_upload.write_status("u-test-ffffffff", "ready", index={"state": "done", "done": 3, "total": 3})
    processing._upload_status.clear()
    monkeypatch.setattr(settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(indexes, "_vertex_deps", lambda: (None, None))
    monkeypatch.setattr(processing, "MAX_STARTUP_REINDEX", 2)
    indexed = []

    async def fake_index(book, chunks, embedder, store, on_progress=None):
        indexed.append(book.id)
        return len(chunks)
    monkeypatch.setattr("app.gcp.indexing.index_book", fake_index)
    asyncio.run(processing._retry_failed_indexes())
    assert indexed == ids[:2]   # bounded; the done book is left alone
    assert ingest_upload.read_status(ids[0])["index"]["state"] == "done"
    assert ingest_upload.read_status(ids[2])["index"]["state"] == "error"

    monkeypatch.setattr(settings, "retrieval_backend", "local")
    indexed.clear()
    asyncio.run(processing._retry_failed_indexes())
    assert indexed == []


# ---------- Windows: a reader holding book.json open blocks os.replace for a moment ----------

def test_replace_retries_permission_errors_with_backoff_then_succeeds(tmp_path, monkeypatch):
    tmp, dst = tmp_path / "a.tmp", tmp_path / "book.json"
    tmp.write_text("x")
    real, fails, sleeps = os.replace, [PermissionError()] * 3, []

    def flaky(src, d):
        if fails:
            fails.pop()
            raise PermissionError("in use")
        real(src, d)
    monkeypatch.setattr(books.os, "replace", flaky)
    monkeypatch.setattr(books.os, "name", "nt")
    monkeypatch.setattr(books.time, "sleep", sleeps.append)
    books._replace(tmp, dst)
    assert dst.read_text() == "x" and len(sleeps) == 3 and all(s > 0 for s in sleeps)


def test_replace_keeps_trying_for_about_two_seconds_then_raises(tmp_path, monkeypatch):
    tmp, dst = tmp_path / "a.tmp", tmp_path / "book.json"
    tmp.write_text("x")
    sleeps = []

    def always(src, d):
        raise PermissionError("in use")
    monkeypatch.setattr(books.os, "replace", always)
    monkeypatch.setattr(books.os, "name", "nt")
    monkeypatch.setattr(books.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        books._replace(tmp, dst)
    assert 2.0 <= sum(sleeps) <= 6.0


def test_book_reads_do_not_hold_the_file_open(data_dir, monkeypatch):
    """Reads go through read_bytes (open, read, close), never a long-lived handle: Path.read_text is not used."""
    make_book_on_disk(data_dir)
    path = data_dir / "books" / BID / "book.json"
    monkeypatch.setattr(type(path), "read_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read_text")))
    assert books.load_book(BID) is not None
    books.update_book(path, lambda b: None)
    assert ingest_upload.read_status(BID) is None or isinstance(ingest_upload.read_status(BID), dict)


def test_failed_checkpoint_is_retried_at_the_next_stage(data_dir, monkeypatch):
    make_book_on_disk(data_dir)
    real, state = books.save_book, {"n": 0, "fail": {1}}   # the segment checkpoint (2nd save) fails once

    def flaky(path, book):
        state["n"] += 1
        if state["n"] - 1 in state["fail"]:
            raise PermissionError("in use")
        real(path, book)
    monkeypatch.setattr(books, "save_book", flaky)
    ok, events, _ = run_pipeline(BID)
    assert ok and events[-1]["status"] == "done"
    book = saved(data_dir)
    assert book.characters and book.summaries and any(s.speaker != "narrator" for p in book.paragraphs for s in p.segments)


def test_final_checkpoint_that_never_lands_is_an_error(data_dir, monkeypatch):
    make_book_on_disk(data_dir)

    def dead(path, book):
        raise PermissionError("in use")
    monkeypatch.setattr(books, "save_book", dead)
    ok, events, _ = run_pipeline(BID)
    assert not ok and events[-1]["status"] == "error" and events[-1]["message"] == pipeline.FAILED


def test_status_writes_retry_permission_errors(data_dir, monkeypatch):
    fails = [PermissionError()] * 2
    real = ingest_upload.Path.write_text

    def flaky(self, *a, **k):
        if fails and self.name == "status.json":
            fails.pop()
            raise PermissionError("in use")
        return real(self, *a, **k)
    monkeypatch.setattr(ingest_upload.Path, "write_text", flaky)
    monkeypatch.setattr(books.time, "sleep", lambda s: None)
    out = ingest_upload.write_status(BID, "ready", enrich={"status": "done"})
    assert json.loads((data_dir / "books" / BID / "status.json").read_text())["enrich"] == {"status": "done"} and out["status"] == "ready"
    fails[:] = [PermissionError()]
    assert ingest_upload.save_status(BID, {"status": "ready"})


# ---------- parallel uploads: no practical limit, each book still bounded by upload_concurrency ----------

def test_parallel_uploads_are_not_limited_but_each_book_is():
    from app.config import Settings
    assert Settings.model_fields["enrich_parallel"].default == 1000
    assert Settings.model_fields["upload_concurrency"].default == 24
    assert LLM("gemini", {"gemini": FakeProvider()}).sem._value == settings.upload_concurrency


def test_startup_settles_an_old_index_only_failure_as_done(monkeypatch, tmp_path):
    from app import ingest_upload, main, processing
    from app.enrich import pipeline
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(processing, "_set_enrich", lambda bid, **f: calls.append((bid, f)))
    monkeypatch.setattr(main.settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(processing, "_upload_status", {"u-x-00000000": {"enrich": {"status": "error", "stage": "index"}}})
    book = SimpleNamespace(characters=[1], summaries=[1])
    processing._settle_index_only_failure("u-x-00000000", book)
    assert calls == [("u-x-00000000", {"status": "done", "stage": "done", "progress": 1.0,
                                       "message": pipeline.INDEX_PENDING})]
    calls.clear()
    processing._settle_index_only_failure("u-x-00000000", SimpleNamespace(characters=[], summaries=[]))
    assert calls == []   # a real failure (nothing saved) stays retryable


def test_settings_repr_never_shows_secrets():
    from app.config import Settings
    s = Settings(boson_api_key="bai-SECRET123", anthropic_api_key="sk-ant-SECRET", access_token="tok-SECRET")
    text = repr(s) + str(s)
    assert "SECRET" not in text


def test_settle_says_ready_when_the_index_is_already_rebuilt(monkeypatch):
    from app import main, processing
    from app.enrich import pipeline
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(main.settings, "retrieval_backend", "vertex")
    monkeypatch.setattr(processing, "_set_enrich", lambda bid, **f: calls.append(f["message"]))
    monkeypatch.setattr(processing, "_upload_status", {"u-x-00000000": {"enrich": {"status": "error", "stage": "index"},
                                                                  "index": {"state": "done"}}})
    processing._settle_index_only_failure("u-x-00000000", SimpleNamespace(characters=[1], summaries=[1]))
    assert calls == [pipeline.READY]


def test_book_processing_can_use_its_own_gemini_model(monkeypatch):
    from app.config import settings
    from app.enrich.llm import GeminiProvider
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash")
    monkeypatch.setattr(settings, "enrich_gemini_model", "gemini-2.5-flash-lite")
    assert GeminiProvider(client=object()).model == "gemini-2.5-flash-lite"
    monkeypatch.setattr(settings, "enrich_gemini_model", "")
    assert GeminiProvider(client=object()).model == "gemini-2.5-flash"   # default: same model as answers
