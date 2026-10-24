"""Async client for Vertex AI Vector Search 2.0 (REST): upsert, delete, vector/text search, RRF."""
import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from ..config import settings
from .auth import get_token

log = logging.getLogger(__name__)

BASE_URL = "https://vectorsearch.googleapis.com/v1"
BATCH_SIZE = 1000
MAX_RETRIES = 3
DELETE_CONCURRENCY = 32  # parallel per-object DELETEs: a 3000-chunk book takes seconds, not minutes


class VectorStoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


def spoiler_filter(book_id: str, reader_para: int | None, earlier: list[str] | None = None) -> dict:
    """Novel filter when reader_para is given (para_end <= reader_para), else the whole book (textbooks, full-book mode).

    `earlier`: ids of the series books before this one; their chunks are eligible in full ($in / $or verified live).
    """
    book = {"book_id": {"$eq": book_id}}
    if reader_para is None:  # textbook (no earlier), or full-book mode: the whole book, plus earlier books if given
        return {"$or": [{"book_id": {"$in": list(earlier)}}, book]} if earlier else book
    current = {"$and": [book, {"para_end": {"$lte": reader_para}}]}
    if not earlier:
        return current
    return {"$or": [{"book_id": {"$in": list(earlier)}}, current]}


def rrf(lists: list[list[dict]], key: str = "chunk_id", k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion over ranked result lists; dedups by data[key], stable on ties."""
    scores: dict[Any, float] = {}
    first: dict[Any, dict] = {}
    order: list[Any] = []
    for results in lists:
        for rank, item in enumerate(results, start=1):
            ident = item["data"][key]
            if ident not in first:
                first[ident] = item
                order.append(ident)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank)
    order.sort(key=lambda i: -scores[i])  # sort is stable: ties keep first-seen order
    return [first[i] for i in order]


class VectorStore:
    def __init__(self, collection_path: str, token_fn: Callable[[], str] = get_token,
                 client: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.collection_path = collection_path
        self._base = f"{BASE_URL}/{collection_path}"
        self._token_fn = token_fn
        self._client = client
        self._owned: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None
        self._sleep = sleep

    async def _send(self, method: str, url: str, json: Any = None) -> httpx.Response:
        """Send with retries on 429/5xx; returns the final response without raising on HTTP status."""
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token_fn()}",
                       "x-goog-user-project": settings.gcp_project}
            resp = await self._http().request(method, url, json=json, headers=headers)
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < MAX_RETRIES:
                await self._sleep(0.5 * 2**attempt)
                continue
            return resp
        raise AssertionError("unreachable")

    def _http(self) -> httpx.AsyncClient:
        """The injected client, else one keep-alive client per event loop (a fresh TLS handshake per search cost
        ~0.3 s; a client cannot be reused once its loop is closed, e.g. scripts that run several loops)."""
        if self._client is not None:
            return self._client
        loop = asyncio.get_running_loop()
        if self._owned is None or self._owned[0] is not loop:
            self._owned = (loop, httpx.AsyncClient(timeout=60))
        return self._owned[1]

    @staticmethod
    def _error(resp: httpx.Response) -> VectorStoreError:
        return VectorStoreError(resp.status_code, resp.text[:300])

    async def _call(self, method: str, url: str, json: Any = None) -> httpx.Response:
        resp = await self._send(method, url, json)
        if not resp.is_success:
            raise self._error(resp)
        return resp

    async def upsert(self, objects: list[dict]) -> None:
        for i in range(0, len(objects), BATCH_SIZE):
            batch = objects[i:i + BATCH_SIZE]
            body = {"requests": [
                {"parent": self.collection_path, "dataObjectId": o["id"],
                 "dataObject": {"data": o["data"],
                                "vectors": {"embedding": {"dense": {"values": o["embedding"]}}}}}
                for o in batch]}
            url = f"{self._base}/dataObjects:batchCreate"
            resp = await self._send("POST", url, body)
            if resp.status_code == 409:
                await self.delete([o["id"] for o in batch])
                resp = await self._send("POST", url, body)
            if not resp.is_success:
                raise self._error(resp)

    async def delete(self, ids: list[str]) -> None:
        """Delete data objects by id (a missing one is fine), at most DELETE_CONCURRENCY requests in flight.

        One DELETE per object: Vector Search 2.0's batchDelete is not used here because this client has never been
        verified against it live. The first failure cancels the rest and is raised.
        """
        slots = asyncio.Semaphore(DELETE_CONCURRENCY)

        async def one(id_: str) -> None:
            async with slots:
                resp = await self._send("DELETE", f"{self._base}/dataObjects/{id_}")
            if not resp.is_success and resp.status_code != 404:
                raise self._error(resp)

        tasks = [asyncio.ensure_future(one(i)) for i in ids]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _parse(payload: dict) -> list[dict]:
        return [{"data": r["dataObject"]["data"], "distance": r.get("distance")}
                for r in payload.get("results", [])]

    async def vector_search(self, vector: list[float], filter: dict, top_k: int) -> list[dict]:
        body = {"vector_search": {"search_field": "embedding", "vector": {"values": vector},
                                  "filter": filter, "top_k": top_k,
                                  "output_fields": {"data_fields": "*"}}}
        resp = await self._call("POST", f"{self._base}/dataObjects:search", body)
        return self._parse(resp.json())

    async def text_search(self, query: str, filter: dict, top_k: int) -> list[dict]:
        """Best-effort: any HTTP error or malformed response yields []."""
        body = {"text_search": {"search_text": query, "data_field_names": ["text"],
                                "top_k": top_k, "filter": filter,
                                "output_fields": {"data_fields": "*"}}}
        try:
            resp = await self._call("POST", f"{self._base}/dataObjects:search", body)
            return self._parse(resp.json())
        except (VectorStoreError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
            status = getattr(e, "status", None)
            if isinstance(status, int) and 400 <= status < 500:
                log.warning("text search rejected (HTTP %s), returning no results: %s", status, e)
            else:
                log.debug("text search failed, returning no results: %s", e)
            return []
