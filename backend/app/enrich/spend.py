"""Cumulative upload-enrichment spend, persisted in data/usage/upload_spend.json (atomic writes)."""
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


def _path() -> Path:
    return settings.data_dir / "usage" / "upload_spend.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"total_usd": 0.0, "by_provider": {}, "books": {}}


def total_usd() -> float:
    with _lock:
        return float(_read().get("total_usd", 0.0))


def add(book_id: str, provider: str, usd: float) -> float:
    """Add `usd` to the totals; returns the new cumulative total."""
    with _lock:
        data = _read()
        data["total_usd"] = round(float(data.get("total_usd", 0.0)) + usd, 6)
        by = data.setdefault("by_provider", {})
        by[provider] = round(float(by.get(provider, 0.0)) + usd, 6)
        books = data.setdefault("books", {})
        books[book_id] = round(float(books.get(book_id, 0.0)) + usd, 6)
        data["updated"] = datetime.now(timezone.utc).isoformat()
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:  # spend is also in usage.jsonl and the logs; never fail a job over bookkeeping
            log.warning("could not write upload spend: %s", type(e).__name__)
        return data["total_usd"]
