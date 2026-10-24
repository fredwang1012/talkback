"""Async client for Vertex AI Vector Search 2.0 (REST): upsert, delete, vector/text search, RRF."""
import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from .. import retrying
from ..config import settings
from .auth import get_token

log = logging.getLogger(__name__)

BASE_URL = "https://vectorsearch.googleapis.com/v1"
BATCH_SIZE = 1000
MAX_RETRIES = 3
DELETE_BATCH = 1000       # dataObjects:batchDelete takes at most 1000 DataObjects (Vector Search 2.0 REST reference)
DELETE_CONCURRENCY = 4    # per-object fallback: few in flight (32 drew 429s on 3000-chunk books live)
DELETE_GAP_S = 0.05       # pause after each per-object DELETE, so a 3000-chunk fallback stays well under the quota
DELETE_429_PAUSE_S = 2.0  # deletes add (attempt + 1) x this to the usual backoff after a 429


class VectorStoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


def _count(statuses: dict) -> dict:
    out: dict = {}
    for v in statuses.values():
        out[str(v)] = out.get(str(v), 0) + 1
    return out


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

    async def _send(self, method: str, url: str, json: Any = None, pause_429: float = 0.0) -> httpx.Response:
        """Send with retries (backoff + jitter) on 429/5xx and on network errors (httpx ReadError, ConnectError,
        timeouts...); returns the final response without raising on HTTP status. The last network error is raised.
        `pause_429`: extra (attempt + 1) x seconds of wait after a 429 (deletes back off longer)."""
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token_fn()}",
                       "x-goog-user-project": settings.gcp_project}
            try:
                resp = await self._http().request(method, url, json=json, headers=headers)
            except httpx.TransportError as e:
                if attempt >= MAX_RETRIES:
                    raise
                log.warning("vector search request failed (%s); retry %d/%d", type(e).__name__, attempt + 1, MAX_RETRIES)
                await self._sleep(retrying.backoff(attempt))
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < MAX_RETRIES:
                extra = pause_429 * (attempt + 1) if resp.status_code == 429 else 0.0
                await self._sleep(retrying.backoff(attempt) + extra)
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

    async def delete(self, ids: list[str]) -> list[str]:
        """Delete data objects by id; returns the ids Vertex refused (empty when all went). Never raises for an HTTP
        status or a network error: callers keep the refused ids for a later retry (app/gcp/orphans.py).

        Batches of DELETE_BATCH go through dataObjects:batchDelete (POST, {"requests": [{"name": ...}]}, per the
        Vector Search 2.0 REST reference). The docs do not say how a batch with a missing object behaves, so a batch
        that is not a plain success falls back to paced per-object DELETEs (404 = already gone = success), at most
        DELETE_CONCURRENCY in flight; one refusal never cancels the others. Logs name counts and HTTP status only.
        """
        refused: list[str] = []
        statuses: dict[str, int] = {}
        for i in range(0, len(ids), DELETE_BATCH):
            batch = ids[i:i + DELETE_BATCH]
            if await self._batch_delete(batch):
                continue
            await self._delete_each(batch, refused, statuses)
        if refused:
            counts = _count(statuses)
            why = ", ".join(f"status {k}" if len(counts) == 1 else f"{n} with status {k}" for k, n in sorted(counts.items()))
            log.warning("vertex delete: %d of %s refused (%s)", len(refused), f"{len(ids):,}", why)
        return refused

    async def _batch_delete(self, batch: list[str]) -> bool:
        body = {"requests": [{"name": f"{self.collection_path}/dataObjects/{i}"} for i in batch]}
        try:
            resp = await self._send("POST", f"{self._base}/dataObjects:batchDelete", body, pause_429=DELETE_429_PAUSE_S)
        except httpx.TransportError:
            return False
        if not resp.is_success:
            log.info("vertex batchDelete of %d not accepted (status %s); deleting one by one", len(batch),
                     resp.status_code)
        return resp.is_success

    async def _delete_each(self, batch: list[str], refused: list[str], statuses: dict[str, int]) -> None:
        slots = asyncio.Semaphore(DELETE_CONCURRENCY)

        async def one(id_: str) -> None:
            async with slots:
                try:
                    resp = await self._send("DELETE", f"{self._base}/dataObjects/{id_}", pause_429=DELETE_429_PAUSE_S)
                    status = resp.status_code if not resp.is_success and resp.status_code != 404 else None
                except httpx.TransportError as e:
                    status = type(e).__name__
                await self._sleep(DELETE_GAP_S)   # pacing: hold the slot a moment after each request
            if status is not None:
                refused.append(id_)
                statuses[id_] = status

        await asyncio.gather(*(one(i) for i in batch))

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
