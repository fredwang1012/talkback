"""The client of the public API: enrichment through it (and the fallbacks), and the /api/answer proxy."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, public_api, public_client
from app.config import settings
from app.enrich import pipeline, spend
from app.enrich.llm import LLM
from app.ratelimit import TokenBucket
from app.schema import Book

from .test_api import env  # noqa: F401  (fixture)
from .test_novel_pipeline import FakeProvider, cpu_calls, data_dir, make_book_on_disk, saved, story  # noqa: F401
from .test_answer import LeakyIndex, lines

BOOK = "u-test-aaaaaaaa"


async def fast_sleep(_):
    await asyncio.sleep(0.01)


@pytest.fixture
def served(data_dir, monkeypatch):  # noqa: F811
    """The real public app, reached in-process through httpx.ASGITransport, with a fake Gemini."""
    monkeypatch.setattr(public_api, "_jobs", {})
    monkeypatch.setattr(public_api, "enrich_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "poll_bucket", TokenBucket(10_000, 60))
    monkeypatch.setattr(spend, "_state", None)
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": FakeProvider()}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=public_api.app), base_url="http://public")


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://public")


def run(client, indexer=None):
    events: list[dict] = []
    ok = asyncio.run(public_client.enrich_book(BOOK, lambda **f: events.append(f), client=client, sleep=fast_sleep,
                                               indexer=indexer))
    return ok, events


# ---------- when the service is used ----------

def test_active_needs_url_and_no_local_llm(monkeypatch):
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert not public_client.active()                       # PUBLIC_API_URL unset: nothing changes
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    assert public_client.active()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-x")
    assert not public_client.active()                       # a local LLM wins
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(public_client, "gcp_credentials_usable", lambda: True)
    assert not public_client.active()
    monkeypatch.setattr(public_client, "gcp_credentials_usable", lambda: False)
    monkeypatch.setattr(settings, "public_api", True)       # the service itself never calls a service
    assert not public_client.active()


# ---------- enrichment through the API ----------

def test_enrich_through_the_public_api(served, data_dir):  # noqa: F811
    make_book_on_disk(data_dir)
    indexed = []

    async def indexer(book):
        indexed.append(book)

    ok, events = run(served, indexer)
    assert ok and indexer and indexed
    last = events[-1]
    assert last["status"] == "done" and last["progress"] == 1.0 and not last.get("degraded")
    assert {e["stage"] for e in events} >= {"cast", "segment", "summaries", "index", "done"}
    book = saved(data_dir)
    assert [c.id for c in book.characters] == ["alice", "bob"] and book.summaries
    assert {s.speaker for p in book.paragraphs for s in p.segments} <= {"narrator", "alice", "bob"}
    assert any(c.speaker == "alice" for p in book.paragraphs for c in p.tts_chunks)
    assert all("".join(s.text for s in p.segments).replace(" ", "") == p.text.replace(" ", "") for p in book.paragraphs)


def _assert_fallback(data_dir, events, note):  # noqa: F811
    last = events[-1]
    assert last["status"] == "done" and last["degraded"] is True and last["message"] == note
    book = saved(data_dir)
    assert {c.id for c in book.characters} >= {"alice"} and all(c.persona for c in book.characters)  # fallback_cast
    assert all(len(p.segments) == 1 for p in book.paragraphs) and not book.summaries


@pytest.mark.parametrize("status", [403, 404, 401, 500, 503])
def test_closed_or_broken_service_falls_back(data_dir, status):  # noqa: F811
    make_book_on_disk(data_dir)
    ok, events = run(mock_client(lambda req: httpx.Response(status, json={"message": "nope"})))
    assert ok
    _assert_fallback(data_dir, events, public_client.UNAVAILABLE_NOTE)


@pytest.mark.parametrize("status,code", [(402, "cap"), (402, "book_cap"), (429, "rate")])
def test_capped_service_falls_back_with_the_limit_message(data_dir, status, code):  # noqa: F811
    make_book_on_disk(data_dir)
    ok, events = run(mock_client(lambda req: httpx.Response(status, json={"detail": "x", "code": code})))
    assert ok
    _assert_fallback(data_dir, events, public_client.LIMIT_NOTE)


def test_timeout_and_connection_errors_fall_back(data_dir):  # noqa: F811
    make_book_on_disk(data_dir)

    def boom(req):
        raise httpx.ConnectTimeout("slow")

    ok, events = run(mock_client(boom))
    assert ok
    _assert_fallback(data_dir, events, public_client.UNAVAILABLE_NOTE)


def test_cap_hit_mid_job_falls_back(data_dir):  # noqa: F811
    make_book_on_disk(data_dir)
    calls = {"n": 0}

    def handler(req):
        if req.method == "POST":
            return httpx.Response(202, json={"job_id": "j"})
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(200, json={"status": "running", "stage": "segment", "progress": 0.4, "message": "m"})
        return httpx.Response(200, json={"status": "error", "code": "cap", "message": "m"})

    ok, events = run(mock_client(handler))
    assert ok and any(e.get("stage") == "segment" and e.get("progress") == 0.4 for e in events)
    _assert_fallback(data_dir, events, public_client.LIMIT_NOTE)


def test_busy_service_is_waited_for(data_dir):  # noqa: F811
    make_book_on_disk(data_dir)
    n = {"post": 0}
    book = Book.model_validate_json((data_dir / "books" / BOOK / "book.json").read_text(encoding="utf-8"))
    result = {"characters": [{"id": "alice", "name": "Alice", "aliases": ["Alice"], "first_para": 0, "gender": "female",
                              "voice": "chloe", "voice_tags": "", "persona": "Brisk."}],
              "narrator": {"voice": "nora", "is_character": None}, "segments": {}, "summaries": []}

    def handler(req):
        if req.method == "POST":
            n["post"] += 1
            return httpx.Response(429, json={"detail": "busy", "code": "busy"}) if n["post"] < 3 else \
                httpx.Response(202, json={"job_id": "j"})
        return httpx.Response(200, json={"status": "done", "stage": "done", "progress": 1.0, "result": result})

    ok, events = run(mock_client(handler))
    assert ok and n["post"] == 3 and not events[-1].get("degraded")
    assert any(e.get("message") == "Waiting for a free processing slot" for e in events)
    assert [c.id for c in saved(data_dir).characters] == ["alice"] and len(book.paragraphs) > 3


def test_a_malformed_or_text_altering_result_is_not_trusted(data_dir):  # noqa: F811
    make_book_on_disk(data_dir)
    bad = {"characters": [], "narrator": None, "segments": {}, "summaries": []}
    ok, events = run(mock_client(lambda req: httpx.Response(202, json={"job_id": "j"}) if req.method == "POST" else
                                 httpx.Response(200, json={"status": "done", "result": bad})))
    assert ok
    _assert_fallback(data_dir, events, public_client.UNAVAILABLE_NOTE)
    # segments that do not reproduce the paragraph are replaced by plain narration
    book = saved(data_dir)
    target = next(p for p in book.paragraphs if "Alice" in p.text)
    book.characters, book.narrator = [], None
    result = {"characters": [{"id": "alice", "name": "Alice", "aliases": ["Alice"], "first_para": 0, "gender": "female",
                              "voice": "nope", "voice_tags": "", "persona": "Brisk."}],
              "narrator": {"voice": "nora", "is_character": "ghost"},
              "segments": {str(target.idx): [{"speaker": "alice", "text": "Something else entirely"}]},
              "summaries": [{"upto_para": 10_000, "text": "Recap."}]}
    public_client.apply_result(book, result)
    assert [s.speaker for s in book.paragraphs[target.idx].segments] == ["narrator"]
    assert book.characters[0].voice == "nora" and book.narrator.is_character is None
    assert book.summaries[0].upto_para == len(book.paragraphs) - 1


# ---------- the app uses it only when configured ----------

def test_run_enrichment_uses_the_public_api_only_when_active(data_dir, monkeypatch):  # noqa: F811
    make_book_on_disk(data_dir)
    called = []

    async def fake_public(book_id, report, **kw):
        called.append("public")
        report(status="done", stage="done", progress=1.0, message="ok")

    async def fake_local(book_id, report, **kw):
        called.append("local")
        report(status="done", stage="done", progress=1.0, message="ok")

    monkeypatch.setattr(public_client, "enrich_book", fake_public)
    monkeypatch.setattr(pipeline, "enrich_book", fake_local)
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    asyncio.run(main._run_enrichment(BOOK))
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    asyncio.run(main._run_enrichment(BOOK))
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-local")
    asyncio.run(main._run_enrichment(BOOK))
    assert called == ["local", "public", "local"]


# ---------- /api/answer through the public API ----------

def ask(client, **kw):
    body = {"book_id": "demo", "query": "What happened?", "reader_para": 1, "speaker_id": "harry"}
    body.update(kw)
    return client.post("/api/answer", json=body)


@pytest.fixture
def via_public(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    monkeypatch.setattr(main, "answer_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "get_answerer", lambda: pytest.fail("the local Gemini must not be used"))
    return env[0]


def test_answer_proxies_spoiler_filtered_passages(via_public, monkeypatch):
    seen = []

    async def fake_stream(payload, client=None):
        seen.append(payload)
        yield {"type": "sentence", "text": "He smiled."}
        yield {"type": "sentence", "text": "Then he left."}

    monkeypatch.setattr(public_client, "stream_answer", fake_stream)
    monkeypatch.setattr(main, "_index_for", lambda book_id: LeakyIndex())  # offers a future paragraph as well
    out = lines(ask(via_public, query="King arrived"))
    assert [o["type"] for o in out] == ["sentence", "sentence", "done"] and out[2]["passages"] >= 1
    p = seen[0]
    assert p["speaker_id"] == "harry" and p["speaker_name"] == "Harry" and p["persona"] == "Precise."
    assert p["query"] == "King arrived" and p["title"] == "Demo" and p["spoilers"] is False
    text = json.dumps(p)
    assert "ELIGIBLE-TEXT-ALPHA" in text and "FUTURE-TEXT-OMEGA" not in text   # the server never sees ineligible text


def test_answer_failure_is_a_clear_error_and_health_falls_back_to_realtime(via_public, monkeypatch):
    async def closed(payload, client=None):
        raise public_client.PublicFailure("unavailable", "403")
        yield  # pragma: no cover

    monkeypatch.setattr(public_client, "stream_answer", closed)
    out = lines(ask(via_public))
    assert out == [{"type": "error", "message": public_client.ANSWER_UNAVAILABLE}]
    assert public_client._probe is not None and public_client._probe[1] is False  # health now says "realtime"
    assert via_public.get("/api/health").json()["answer_backend"] == "realtime"


def test_answer_limit_message(via_public, monkeypatch):
    async def capped(payload, client=None):
        raise public_client.PublicFailure("limit", "daily_cap")
        yield  # pragma: no cover

    monkeypatch.setattr(public_client, "stream_answer", capped)
    out = lines(ask(via_public))
    assert out == [{"type": "error", "message": public_client.ANSWER_LIMIT}]


def test_health_reports_realtime_when_the_service_is_closed(via_public, monkeypatch):
    def refuse(url, timeout):
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", refuse)
    assert via_public.get("/api/health").json()["answer_backend"] == "realtime"
    assert public_client._probe[1] is False
    monkeypatch.setattr(public_client, "_probe", None)
    monkeypatch.setattr(httpx, "get", lambda url, timeout: httpx.Response(200, json={"ok": True, "public": True},
                                                                         request=httpx.Request("GET", url)))
    assert via_public.get("/api/health").json()["answer_backend"] == "gemini"


def test_stream_answer_refusals_become_failures():
    def run_stream(handler):
        async def go():
            async with mock_client(handler) as c:
                return [m async for m in public_client.stream_answer({"query": "q"}, client=c)]
        return asyncio.run(go())

    for status, kind in [(403, "unavailable"), (404, "unavailable"), (402, "limit"), (429, "limit")]:
        with pytest.raises(public_client.PublicFailure) as e:
            run_stream(lambda req, s=status: httpx.Response(s, json={"code": "x"}))
        assert e.value.kind == kind
    ok = run_stream(lambda req: httpx.Response(200, text='{"type":"sentence","text":"Hi."}\n\n{"type":"done"}\n'))
    assert ok == [{"type": "sentence", "text": "Hi."}]


def test_without_public_url_the_local_answer_path_is_unchanged(env, monkeypatch):  # noqa: F811
    from .test_answer import FakeAnswerer
    client, _ = env
    fake = FakeAnswerer(["Local."])
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    monkeypatch.setattr(public_client, "stream_answer", lambda *a, **k: pytest.fail("public API must not be called"))
    assert [o["type"] for o in lines(ask(client))] == ["sentence", "done"]
    assert client.get("/api/health").json()["answer_backend"] == "realtime"


def test_apply_result_runs_off_the_event_loop(served, data_dir, cpu_calls):  # noqa: F811
    """The service's result is applied (segments + a whole-book rechunk) in a worker thread, not on the loop."""
    make_book_on_disk(data_dir)
    ok, _ = run(served)
    assert ok
    client_side = cpu_calls[-1:]   # the service's own pipeline runs first (in-process); the client's rechunk is last
    assert client_side == [("rechunk", False)], cpu_calls
