"""Append-only JSONL log of billable Google API usage (data/cache/usage.jsonl)."""
import asyncio
import json
import logging
import threading
from datetime import datetime, timezone

from ..config import settings

log = logging.getLogger("talkback.usage")
_file_lock = threading.Lock()
_offload = True  # tests switch this off so a record is on disk when the call returns


def _log_path():
    return settings.data_dir / "cache" / "usage.jsonl"


def record(kind: str, model: str, **counts) -> None:
    """Append one usage line and also log it (Cloud Logging keeps it if file appends are lost). Never raises."""
    try:
        line = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "model": model, **counts}
        text = json.dumps(line, ensure_ascii=False)
    except Exception:
        return
    try:
        log.info(text)
    except Exception:
        pass
    if not settings.usage_log_file:
        return  # Cloud Run: the log line above is the record; a gcsfuse append rewrites the whole object
    try:
        loop = asyncio.get_running_loop() if _offload else None
    except RuntimeError:
        loop = None
    if loop is None:
        _append(text)
        return
    try:
        loop.run_in_executor(None, _append, text)  # never block the event loop on file I/O
    except Exception:
        _append(text)


def _append(text: str) -> None:
    try:
        with _file_lock:
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
    except Exception:
        pass
