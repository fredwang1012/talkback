"""Cumulative upload-enrichment spend, persisted in data/usage/upload_spend.json (atomic writes).

Totals live in memory (caps are enforced from them at once); `flush()` writes the file atomically. `add(flush=False)`
plus a `flush()` in a worker thread keeps file I/O off the event loop during a job.
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

log = logging.getLogger("talkback.enrich")
_lock = threading.Lock()
_state: dict | None = None
_state_path: Path | None = None
_dirty = False


def _path() -> Path:
    return settings.data_dir / "usage" / "upload_spend.json"


def _load() -> dict:
    """Totals for the current data dir (read from the file on first use or when the data dir changes)."""
    global _state, _state_path, _dirty
    path = _path()
    if _state is None or _state_path != path:
        data = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        _state = data if isinstance(data, dict) else {"total_usd": 0.0, "by_provider": {}, "books": {}}
        _state_path, _dirty = path, False
    return _state


def total_usd() -> float:
    with _lock:
        return float(_load().get("total_usd", 0.0))


def _write(data: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:  # spend is also in usage.jsonl and the logs; never fail a job over bookkeeping
        log.warning("could not write upload spend: %s", type(e).__name__)


def flush() -> None:
    """Write the totals if they changed (safe to call from a worker thread)."""
    global _dirty
    with _lock:
        if _dirty and _state is not None and _state_path is not None:
            _write(dict(_state), _state_path)
            _dirty = False


def add(book_id: str, provider: str, usd: float, flush_now: bool = True) -> float:
    """Add `usd` to the totals; returns the new cumulative total."""
    global _dirty
    with _lock:
        data = _load()
        data["total_usd"] = round(float(data.get("total_usd", 0.0)) + usd, 6)
        by = data.setdefault("by_provider", {})
        by[provider] = round(float(by.get(provider, 0.0)) + usd, 6)
        books = data.setdefault("books", {})
        books[book_id] = round(float(books.get(book_id, 0.0)) + usd, 6)
        data["updated"] = datetime.now(timezone.utc).isoformat()
        _dirty = True
        total = data["total_usd"]
        if flush_now:
            _write(dict(data), _state_path)
            _dirty = False
        return total
