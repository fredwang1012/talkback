"""A persisted call count for the current UTC day (data/usage/<file>, written atomically), for small daily caps."""
import json
import threading
from datetime import datetime, timezone

from .config import settings
from .enrich import spend


class DailyCount:
    """Calls of the current UTC day in data/usage/<file> (written atomically), for a small daily call cap."""

    def __init__(self, file: str):
        self.file, self.day, self.n, self.path = file, None, 0, None
        self.lock = threading.Lock()

    def take(self, limit: int) -> bool:
        with self.lock:
            path = settings.data_dir / "usage" / self.file
            today = datetime.now(timezone.utc).date().isoformat()
            if self.path != path:
                self.path, self.day, self.n = path, None, 0
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self.day, self.n = data.get("day"), int(data.get("n", 0))
                except (OSError, ValueError, TypeError):
                    pass
            if self.day != today:
                self.day, self.n = today, 0
            if self.n >= limit:
                return False
            self.n += 1
            spend._write({"day": self.day, "n": self.n}, self.path)
            return True
