"""Vertex datapoints a delete could not remove, kept in data_dir/cache/vertex_orphans.json and retried in the background.

A book delete never waits on Vertex: whatever ids Vector Search refused (429s, 5xx, network errors) are added here and
the book's files go at once. Leftover datapoints are harmless for correctness: every search filters on book_id, both in
the query filter (vectorstore.spoiler_filter) and in VertexIndex._eligible's post-filter, and book ids are never reused,
so a deleted book's chunks can never be returned. They only cost index space until the retry removes them.

The list is global server state (ids only, no book text), written atomically under a lock and bounded to MAX_IDS (the
newest kept). retry_forever runs once shortly after startup and then every INTERVAL_S, LIMIT ids per pass, and never
raises; VectorStore.delete paces its own requests.
"""
import asyncio
import json
import logging
import threading
from typing import Any, Awaitable, Callable

from ..config import settings

log = logging.getLogger(__name__)

MAX_IDS = 50_000
LIMIT = 2_000           # ids per retry pass
START_DELAY_S = 60.0    # first pass a minute after startup (warm-up first)
INTERVAL_S = 600.0      # then every 10 minutes

_lock = threading.Lock()


def _path():
    return settings.data_dir / "cache" / "vertex_orphans.json"


def _read() -> list[str]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in data if isinstance(i, str)] if isinstance(data, list) else []


def _write(ids: list[str]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(ids), encoding="utf-8")
    tmp.replace(p)


def load() -> list[str]:
    with _lock:
        return _read()


def add(ids: list[str]) -> None:
    """Append ids (no duplicates), keeping only the newest MAX_IDS. Never raises."""
    if not ids:
        return
    try:
        with _lock:
            cur = _read()
            seen = set(cur)
            cur += [i for i in dict.fromkeys(ids) if i not in seen]
            if len(cur) > MAX_IDS:
                log.warning("vertex orphan list full: dropping %d oldest id(s)", len(cur) - MAX_IDS)
                cur = cur[-MAX_IDS:]
            _write(cur)
    except OSError as e:
        log.warning("could not save vertex orphans: %s", type(e).__name__)


def _remove(done: set[str]) -> None:
    with _lock:
        _write([i for i in _read() if i not in done])


async def retry(store: Any, limit: int = LIMIT) -> None:
    """One pass: delete up to `limit` listed ids, keep the ones Vertex still refuses. Never raises."""
    try:
        ids = await asyncio.to_thread(load)
        if not ids:
            return
        batch = ids[:limit]
        refused = set(await store.delete(batch) or [])
        await asyncio.to_thread(_remove, set(batch) - refused)
        log.info("vertex orphan cleanup: %d removed, %d left", len(batch) - len(refused), len(ids) - len(batch) + len(refused))
    except Exception as e:
        log.warning("vertex orphan cleanup failed: %s", type(e).__name__)


async def retry_forever(store_fn: Callable[[], Any],
                        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Background task (Vertex backend): a pass after START_DELAY_S, then every INTERVAL_S. Vertex is only called
    when the list is not empty."""
    await sleep(START_DELAY_S)
    while True:
        if await asyncio.to_thread(load):
            await retry(store_fn())
        await sleep(INTERVAL_S)
