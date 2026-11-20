"""A resume after a restart counts as an attempt (no crash loop) and near-cap spend reaches disk (#141 review).
Fake providers only: no network, no credit."""
import asyncio

from app import ingest_upload, processing
from app.config import settings
from app.enrich import pipeline
from tests.test_novel_pipeline import client, data_dir, make_book_on_disk, run_pipeline  # noqa: F401
from tests.test_resume_enrichment import BID, rig, st, stale  # noqa: F401


# ---------- a resume is an attempt: no crash loop across restarts (#141 review) ----------

def test_resume_counts_as_an_attempt_before_running(rig, monkeypatch):
    stale(BID)
    seen = []

    async def fake_run(book_id, resume=False):
        seen.append(ingest_upload.read_status(book_id).get("enrich_retries"))   # bumped BEFORE the job runs
    monkeypatch.setattr(processing, "_run_enrichment", fake_run)
    asyncio.run(processing._resume_enrichments())
    assert seen == [1]


def test_resume_stops_after_max_attempts(rig, monkeypatch):
    stale(BID)
    started = []

    async def crash(book_id, resume=False):   # the process "dies" mid-job: status stays running
        started.append(book_id)
    monkeypatch.setattr(processing, "_run_enrichment", crash)
    for _ in range(processing.MAX_ENRICH_RETRIES + 2):   # restart after restart
        processing._upload_status.clear()
        asyncio.run(processing._resume_enrichments())
    assert len(started) == processing.MAX_ENRICH_RETRIES
    e = st(BID)
    assert e["status"] == "error" and "several times" in e["message"]
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 409   # and no user retry past the limit either


def test_spend_flushed_every_call_near_the_book_cap(data_dir, monkeypatch):
    from app.enrich import spend
    make_book_on_disk(data_dir)
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1000.0)
    monkeypatch.setattr(settings, "upload_total_cap_usd", 100000.0)
    spend.add(BID, "fake", 0.9 * 1000.0 * pipeline.BOOK_CAP_MARGIN)   # most of the allowance already used
    flushes = []
    real = spend.flush
    monkeypatch.setattr(spend, "flush", lambda: (flushes.append(1), real())[1])
    ok, _, llm = run_pipeline(BID)
    assert ok and llm.calls < 25
    assert len(flushes) >= llm.calls   # every call reaches disk: a crash cannot lose the near-cap spend
