"""Append-only JSONL log of billable Google API usage (data/cache/usage.jsonl)."""
import json
import logging
from datetime import datetime, timezone

from ..config import settings

log = logging.getLogger("talkback.usage")


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
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
