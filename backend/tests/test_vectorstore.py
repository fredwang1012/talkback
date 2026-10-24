from app.config import settings
import asyncio
import json

import httpx
import pytest

from app.gcp.vectorstore import VectorStore, VectorStoreError, rrf, spoiler_filter

COLL = "projects/frederick-talkback/locations/us-central1/collections/chunks"
BASE = f"https://vectorsearch.googleapis.com/v1/{COLL}"


def make(handler, sleeps=None):
    async def fake_sleep(s):
        if sleeps is not None:
            sleeps.append(s)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VectorStore(COLL, token_fn=lambda: "tok", client=client, sleep=fake_sleep)


def obj(i):
    return {"id": f"b__{i}", "data": {"chunk_id": str(i), "text": "t"}, "embedding": [0.1, 0.2]}


def run(coro):
    return asyncio.run(coro)


def test_spoiler_filter():
    assert spoiler_filter("b", None) == {"book_id": {"$eq": "b"}}
    assert spoiler_filter("b", 7) == {"$and": [{"book_id": {"$eq": "b"}}, {"para_end": {"$lte": 7}}]}


def test_upsert_shape_and_auth(monkeypatch):
    monkeypatch.setattr(settings, "gcp_project", "frederick-talkback")   # its own project, never from .env
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})
    run(make(handler).upsert([obj(1)]))
    r = seen[0]
    assert r.method == "POST" and str(r.url) == f"{BASE}/dataObjects:batchCreate"
    assert r.headers["authorization"] == "Bearer tok"
    assert r.headers["x-goog-user-project"] == "frederick-talkback"
    assert json.loads(r.content) == {"requests": [{
        "parent": COLL, "dataObjectId": "b__1",
        "dataObject": {"data": {"chunk_id": "1", "text": "t"},
                       "vectors": {"embedding": {"dense": {"values": [0.1, 0.2]}}}}}]}


def test_upsert_batches_at_1000():
    sizes = []

    def handler(req):
        sizes.append(len(json.loads(req.content)["requests"]))
        return httpx.Response(200, json={})
    run(make(handler).upsert([obj(i) for i in range(2500)]))
    assert sizes == [1000, 1000, 500]


def test_upsert_409_deletes_and_retries_once():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith(":batchCreate") and calls.count(("POST", req.url.path)) == 1:
            return httpx.Response(409, json={"error": "exists"})
        return httpx.Response(200, json={})
    run(make(handler).upsert([obj(1), obj(2)]))
    assert [c[0] for c in calls] == ["POST", "POST", "POST"]
    assert calls[1][1].endswith("/dataObjects:batchDelete")


def test_upsert_409_twice_raises():
    store = make(lambda req: httpx.Response(409, text="exists") if req.method == "POST"
                 else httpx.Response(200))
    with pytest.raises(VectorStoreError) as e:
        run(store.upsert([obj(1)]))
    assert e.value.status == 409


def test_delete_uses_batch_delete_in_chunks_of_1000():
    """POST {collection}/dataObjects:batchDelete, body {"requests": [{"name": <full DataObject name>}]}, at most
    1000 per batch (Vector Search 2.0 REST reference)."""
    bodies = []

    def handler(req):
        assert req.method == "POST" and str(req.url) == f"{BASE}/dataObjects:batchDelete"
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={})
    ids = [f"b__{i}" for i in range(2500)]
    assert run(make(handler).delete(ids)) == []
    assert [len(b["requests"]) for b in bodies] == [1000, 1000, 500]
    assert bodies[0]["requests"][0] == {"name": f"{COLL}/dataObjects/b__0"}


def test_delete_falls_back_per_object_and_404_is_success():
    urls = []

    def handler(req):
        urls.append((req.method, str(req.url)))
        return httpx.Response(404)
    assert run(make(handler).delete(["a", "b"])) == []
    assert urls == [("POST", f"{BASE}/dataObjects:batchDelete"),
                    ("DELETE", f"{BASE}/dataObjects/a"), ("DELETE", f"{BASE}/dataObjects/b")]


def test_delete_never_raises_and_returns_the_refused_ids(caplog):
    """One refusal does not cancel the rest: every id is tried, the refused ones come back, the log names the status
    and counts only (never the response text or the ids)."""
    def handler(req):
        if req.method == "POST":
            return httpx.Response(500, text="secret body")
        if req.url.path.endswith(("/b__3", "/b__7")):
            return httpx.Response(429, text="secret body")
        if req.url.path.endswith("/b__5"):
            raise httpx.ConnectError("c", request=req)
        return httpx.Response(200, json={})
    ids = [f"b__{i}" for i in range(10)]
    with caplog.at_level("WARNING"):
        refused = run(make(handler).delete(ids))
    assert sorted(refused) == ["b__3", "b__5", "b__7"]
    text = caplog.text
    assert "3 of 10 refused" in text and "429" in text
    assert "secret" not in text and "b__3" not in text


def test_delete_429_backs_off_longer():
    from app.gcp import vectorstore
    sleeps = []

    def handler(req):
        return httpx.Response(429) if req.method == "DELETE" else httpx.Response(400)
    assert run(make(handler, sleeps).delete(["a"])) == ["a"]
    retry_sleeps = [s for s in sleeps if s >= vectorstore.DELETE_429_PAUSE_S]
    assert len(retry_sleeps) >= vectorstore.MAX_RETRIES


def test_vector_search_shape_and_parse():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"results": [
            {"dataObject": {"name": "n", "data": {"chunk_id": "1"}}, "distance": 0.25},
            {"dataObject": {"name": "m", "data": {"chunk_id": "2"}}}]})
    f = spoiler_filter("b", 5)
    out = run(make(handler).vector_search([1.0, 2.0], f, 8))
    assert str(seen[0].url) == f"{BASE}/dataObjects:search"
    assert json.loads(seen[0].content) == {"vector_search": {
        "search_field": "embedding", "vector": {"values": [1.0, 2.0]}, "filter": f, "top_k": 8,
        "output_fields": {"data_fields": "*"}}}
    assert out == [{"data": {"chunk_id": "1"}, "distance": 0.25},
                   {"data": {"chunk_id": "2"}, "distance": None}]


def test_text_search_shape_and_errors_to_empty():
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(200, json={"results": [{"dataObject": {"data": {"chunk_id": "1"}}}]})
    f = spoiler_filter("b", None)
    assert run(make(ok).text_search("hi", f, 3)) == [{"data": {"chunk_id": "1"}, "distance": None}]
    assert json.loads(seen[0].content) == {"text_search": {
        "search_text": "hi", "data_field_names": ["text"], "top_k": 3, "filter": f,
        "output_fields": {"data_fields": "*"}}}
    assert run(make(lambda r: httpx.Response(400, text="bad")).text_search("hi", f, 3)) == []
    assert run(make(lambda r: httpx.Response(200, text="not json")).text_search("hi", f, 3)) == []
    assert run(make(lambda r: httpx.Response(200, json={"results": [{"x": 1}]})).text_search("hi", f, 3)) == []


def test_retry_5xx_then_success_and_exhaustion():
    sleeps = []
    n = {"c": 0}

    def flaky(req):
        n["c"] += 1
        return httpx.Response(503) if n["c"] < 3 else httpx.Response(200, json={"results": []})
    assert run(make(flaky, sleeps).vector_search([1.0], {}, 1)) == []
    assert n["c"] == 3 and len(sleeps) == 2 and 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0   # jittered backoff

    n["c"] = 0
    sleeps.clear()

    def always(req):
        n["c"] += 1
        return httpx.Response(429, text="slow")
    with pytest.raises(VectorStoreError) as e:
        run(make(always, sleeps).vector_search([1.0], {}, 1))
    assert e.value.status == 429 and n["c"] == 4 and len(sleeps) == 3


def test_no_retry_on_4xx():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(400, text="bad")
    with pytest.raises(VectorStoreError):
        run(make(handler).vector_search([1.0], {}, 1))
    assert n["c"] == 1


def test_rrf_ordering_and_dedup():
    def it(c):
        return {"data": {"chunk_id": c}, "distance": None}
    a = [it("x"), it("y"), it("z")]
    b = [it("y"), it("w")]
    out = rrf([a, b])
    assert [o["data"]["chunk_id"] for o in out] == ["y", "x", "w", "z"]
    assert rrf([]) == []
    assert [o["data"]["chunk_id"] for o in rrf([[it("p"), it("q")], [it("q"), it("p")]])] == ["p", "q"]


def test_text_search_4xx_is_warned_other_failures_are_not(caplog):
    f = spoiler_filter("b", None)
    with caplog.at_level("DEBUG", logger="talkback.gcp.vectorstore"):
        assert run(make(lambda r: httpx.Response(400, text="bad")).text_search("hi", f, 3)) == []
    assert any(r.levelname == "WARNING" for r in caplog.records)
    caplog.clear()
    with caplog.at_level("DEBUG", logger="talkback.gcp.vectorstore"):
        assert run(make(lambda r: httpx.Response(200, text="not json")).text_search("hi", f, 3)) == []
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_owned_client_is_reused_within_a_loop_and_replaced_across_loops(monkeypatch):
    import asyncio

    import httpx

    from app.gcp import vectorstore

    made = []

    class FakeClient:
        def __init__(self, **kw):
            made.append(self)

        async def request(self, method, url, json=None, headers=None):
            return httpx.Response(200, json={"results": []}, request=httpx.Request(method, url))

    monkeypatch.setattr(vectorstore.httpx, "AsyncClient", FakeClient)
    vs = vectorstore.VectorStore("projects/p/locations/l/collections/c", token_fn=lambda: "t")

    async def two_searches():
        await vs.vector_search([0.0], {}, 1)
        await vs.vector_search([0.0], {}, 1)

    asyncio.run(two_searches())
    assert len(made) == 1, "keep-alive: one client per loop"
    asyncio.run(two_searches())
    assert len(made) == 2, "a closed loop's client is never reused"


def test_delete_runs_requests_concurrently_but_bounded():
    from app.gcp import vectorstore
    state = {"now": 0, "max": 0, "seen": []}

    async def handler(req):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.005)
        state["now"] -= 1
        state["seen"].append(str(req.url).rsplit("/", 1)[1])
        return httpx.Response(404 if len(state["seen"]) % 3 == 0 else 200, json={})

    async def routed(req):
        if req.method == "POST":   # batchDelete refused: the per-object fallback runs
            return httpx.Response(400)
        return await handler(req)
    ids = [f"b__{i}" for i in range(200)]
    sleeps = []
    assert run(make(routed, sleeps).delete(ids)) == []
    assert sorted(state["seen"]) == sorted(ids)
    assert 1 < state["max"] <= vectorstore.DELETE_CONCURRENCY <= 4
    assert len([s for s in sleeps if s >= vectorstore.DELETE_GAP_S]) >= len(ids)   # every request is paced


def test_upsert_409_retries_until_refused_deletes_land():
    """A half-indexed book: the first re-create still finds objects whose delete Vertex refused (429); the store
    deletes again and the next create succeeds instead of failing the whole indexing run."""
    creates, deletes = [], []

    def handler(req):
        if req.url.path.endswith(":batchCreate"):
            creates.append(1)
            return httpx.Response(409, json={"error": "exists"}) if len(creates) <= 2 else httpx.Response(200, json={})
        deletes.append(1)
        return httpx.Response(200, json={})
    run(make(handler).upsert([obj(1), obj(2)]))
    assert len(creates) == 3 and len(deletes) >= 2
