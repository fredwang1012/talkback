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


def test_upsert_shape_and_auth():
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
        if req.method == "POST" and calls.count(("POST", req.url.path)) == 1:
            return httpx.Response(409, json={"error": "exists"})
        return httpx.Response(200, json={})
    run(make(handler).upsert([obj(1), obj(2)]))
    assert [c[0] for c in calls] == ["POST", "DELETE", "DELETE", "POST"]
    assert calls[1][1].endswith("/dataObjects/b__1")


def test_upsert_409_twice_raises():
    store = make(lambda req: httpx.Response(409, text="exists") if req.method == "POST"
                 else httpx.Response(200))
    with pytest.raises(VectorStoreError) as e:
        run(store.upsert([obj(1)]))
    assert e.value.status == 409


def test_delete_ignores_404_raises_other():
    urls = []

    def handler(req):
        urls.append((req.method, str(req.url)))
        return httpx.Response(404)
    run(make(handler).delete(["a", "b"]))
    assert urls == [("DELETE", f"{BASE}/dataObjects/a"), ("DELETE", f"{BASE}/dataObjects/b")]
    with pytest.raises(VectorStoreError):
        run(make(lambda req: httpx.Response(403, text="no")).delete(["a"]))


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
    assert n["c"] == 3 and sleeps == [0.5, 1.0]

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
