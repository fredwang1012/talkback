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
    monkeypatch.setattr(usage, "_offload", False)


@pytest.fixture(autouse=True)
def _local_backends_unless_a_test_says_otherwise(monkeypatch):
    """Tests never reach Google Cloud because of a developer's .env (RETRIEVAL_BACKEND=vertex)."""
    monkeypatch.setattr(settings, "retrieval_backend", "local")
    monkeypatch.setattr(settings, "answer_backend", None, raising=False)


@pytest.fixture(autouse=True)
def _no_llm_enrichment_unless_a_test_says_otherwise(monkeypatch, tmp_path_factory):
    """Uploaded novels are enriched by an LLM; tests never reach one (or write the real spend file) by accident."""
    monkeypatch.setattr(settings, "upload_enrich", False, raising=False)
    monkeypatch.setattr(settings, "upload_confirm", False, raising=False)


@pytest.fixture(autouse=True)
def _public_api_off_unless_a_test_says_otherwise(monkeypatch):
    """A developer's .env (PUBLIC_API_URL, PUBLIC_API) never changes what the tests exercise."""
    from app import public_client
    monkeypatch.setattr(settings, "public_api_url", "", raising=False)
    monkeypatch.setattr(settings, "public_api", False, raising=False)
    monkeypatch.setattr(public_client, "_probe", None)


@pytest.fixture(autouse=True)
def _no_minor_voices_unless_a_test_says_otherwise(monkeypatch):
    """Pipeline tests assert exact casts and segments; the generic-voice pass (#70) is tested on its own."""
    monkeypatch.setattr(settings, "minor_voices", False, raising=False)
