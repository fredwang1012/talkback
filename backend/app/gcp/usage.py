"""Append-only JSONL log of billable Google API usage (data/cache/usage.jsonl)."""
import json
from datetime import datetime, timezone

from ..config import settings


def record(kind: str, model: str, **counts) -> None:
    """Append one usage line. Never raises."""
    try:
        path = settings.data_dir / "cache" / "usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "model": model, **counts}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass
