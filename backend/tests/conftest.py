from pathlib import Path

import pytest

from app.config import settings
from app.gcp import usage

REAL_DATA_DIR = Path(settings.data_dir).resolve()


@pytest.fixture(autouse=True)
def _never_write_real_usage_log(monkeypatch, tmp_path_factory):
    """Tests may read books from the real data dir, but must never append to the real usage log.

    If settings.data_dir still points at the real data dir, usage lines go to a throwaway file instead;
    tests that point settings.data_dir at their own tmp dir keep writing (and reading) there.
    """
    fallback = tmp_path_factory.mktemp("usage") / "usage.jsonl"

    def log_path():
        if Path(settings.data_dir).resolve() == REAL_DATA_DIR:
            return fallback
        return Path(settings.data_dir) / "cache" / "usage.jsonl"

    monkeypatch.setattr(usage, "_log_path", log_path)
