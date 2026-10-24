"""A restart in the middle of character processing: resume on startup, and a retry for an interrupted/failed job.
Fake providers only: no network, no credit."""
import asyncio

import pytest

from app import ingest_upload, main, users
from app.config import settings
from app.enrich import pipeline
from tests.test_novel_pipeline import FakeProvider, client, data_dir, make_book_on_disk, make_llm, run_pipeline, saved  # noqa: F401
from tests.test_api import env  # noqa: F401
from tests.test_per_user import ALICE, BOB, h, pu, token  # noqa: F401

BID = "u-test-aaaaaaaa"
BID2 = "u-test-bbbbbbbb"


@pytest.fixture
def rig(client, tmp_path):  # noqa: F811
    """A client whose uploads are enriched by the shared fake provider (client.fake)."""
    return client


def stale(book_id, status="running", stage="segment", **book_kw):
    make_book_on_disk(settings.data_dir, book_id=book_id)
    ingest_upload.write_status(book_id, "ready", enrich={"status": status, "stage": stage, "progress": 0.3})
    main._upload_status.clear()


def st(book_id):
    return ingest_upload.read_status(book_id)["enrich"]


def test_startup_resumes_running_and_queued(rig):
    stale(BID, "running")
    stale(BID2, "queued")
    asyncio.run(main._resume_enrichments())
    assert st(BID)["status"] == "done" and st(BID2)["status"] == "done"
    assert saved(settings.data_dir, BID).characters and saved(settings.data_dir, BID2).summaries


def test_startup_leaves_other_states_alone(rig):
    for i, s in enumerate(("done", "refused", "awaiting_confirmation", "error")):
        bid = f"u-test-{i:08d}"
        make_book_on_disk(settings.data_dir, book_id=bid)
        ingest_upload.write_status(bid, "ready", enrich={"status": s, "message": "x"})
    asyncio.run(main._resume_enrichments())
    assert not rig.fake.calls
    assert st("u-test-00000003")["status"] == "error"


def test_resume_rechecks_caps(rig, monkeypatch):
    stale(BID)
    monkeypatch.setattr(settings, "upload_book_cap_usd", 0.00001)
    asyncio.run(main._resume_enrichments())
    assert st(BID) == {"status": "refused", "message": pipeline.TOO_LONG}
    assert not rig.fake.calls


def test_resume_does_not_double_start(rig):
    stale(BID)
    main._enrich_jobs.add(BID)   # a job is already queued or running for this book
    try:
        asyncio.run(main._resume_enrichments())
    finally:
        main._enrich_jobs.discard(BID)
    assert not rig.fake.calls
    stale(BID2)
    asyncio.run(main._resume_enrichments())
    n = len(rig.fake.calls)
    asyncio.run(main._resume_enrichments())   # now done: nothing to resume
    assert len(rig.fake.calls) == n


def test_resume_is_serialised_by_the_enrichment_slot(rig):
    stale(BID)
    stale(BID2)
    rig.fake.delay = 0.01
    asyncio.run(main._resume_enrichments())
    assert rig.fake.peak <= 8   # one job at a time: its own parallel calls only, never two jobs' worth
    assert st(BID)["status"] == st(BID2)["status"] == "done"


def test_resume_reuses_finished_stages(rig):
    stale(BID)
    first = asyncio.run(pipeline.enrich_book(BID, lambda **f: None, llm=make_llm(FakeProvider())))
    assert first
    # interrupted again after everything landed except the final status: nothing is paid twice
    ingest_upload.write_status(BID, "ready", enrich={"status": "running", "stage": "index"})
    main._upload_status.clear()
    asyncio.run(main._resume_enrichments())
    assert st(BID)["status"] == "done" and not rig.fake.calls


def test_resume_skips_cast_when_cast_checkpoint_exists(rig):
    stale(BID)
    # a first run that fails in summaries: cast and segment are checkpointed in book.json
    class Fail(FakeProvider):
        async def complete(self, system, user, schema, *a):
            if schema["title"] == "NoteResult":
                raise ValueError("boom")
            return await super().complete(system, user, schema, *a)
    asyncio.run(pipeline.enrich_book(BID, lambda **f: None, llm=make_llm(Fail())))
    assert saved(settings.data_dir, BID).characters
    ingest_upload.write_status(BID, "ready", enrich={"status": "running", "stage": "summaries"})
    main._upload_status.clear()
    asyncio.run(main._resume_enrichments())
    assert "MainCast" not in rig.fake.calls
    assert st(BID)["status"] == "done" and saved(settings.data_dir, BID).summaries


def test_per_user_owner_context_is_restored(rig, monkeypatch):
    monkeypatch.setattr(settings, "per_user", True)
    monkeypatch.setattr(settings, "iap_audience", "aud")
    monkeypatch.setattr(settings, "owner_emails", "")
    stale(BID)
    (settings.data_dir / "books" / BID / "owner.txt").write_text(users.ns_for(ALICE))
    seen = []

    async def fake_run(book_id, report, **kw):
        seen.append((book_id, users.current().ns))
        return True
    monkeypatch.setattr(pipeline, "enrich_book", fake_run)
    asyncio.run(main._resume_enrichments())
    assert seen == [(BID, users.ns_for(ALICE))]
    assert users.current().ns == ""   # the startup context itself is untouched


def test_per_user_unknown_owner_is_marked_resumable_not_run(rig, monkeypatch):
    monkeypatch.setattr(settings, "per_user", True)
    monkeypatch.setattr(settings, "iap_audience", "aud")
    monkeypatch.setattr(settings, "owner_emails", "")   # no owner.txt and no configured owner: cannot be determined
    stale(BID)
    asyncio.run(main._resume_enrichments())
    e = st(BID)
    assert e["status"] == "error" and "try again" in e["message"].lower()
    assert not rig.fake.calls


def test_stale_status_message_says_interrupted_and_retryable(rig):
    stale(BID)
    e = rig.get(f"/api/books/{BID}/status").json()["enrich"]
    assert e["status"] == "error" and "interrupted" in e["message"] and "try again" in e["message"].lower()


def test_failure_message_says_retryable():
    assert "try again" in pipeline.FAILED.lower()


# ---------- retry route ----------

def test_retry_after_error_runs_the_job(rig):
    make_book_on_disk(settings.data_dir, book_id=BID)
    ingest_upload.write_status(BID, "ready", enrich={"status": "error", "stage": "segment", "message": pipeline.FAILED})
    main._upload_status.clear()
    r = rig.post(f"/api/books/{BID}/enrich")
    assert r.status_code == 202
    assert rig.get(f"/api/books/{BID}/status").json()["enrich"]["status"] == "done"
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 409   # done: nothing to retry


def test_retry_after_interruption_without_restart_writeback(rig):
    stale(BID)   # status.json still says running (stale): the status route reports it as an error, the retry accepts it
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 202
    assert st(BID)["status"] == "done"


def test_retry_rechecks_caps(rig, monkeypatch):
    make_book_on_disk(settings.data_dir, book_id=BID)
    ingest_upload.write_status(BID, "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    monkeypatch.setattr(settings, "upload_book_cap_usd", 0.00001)
    r = rig.post(f"/api/books/{BID}/enrich")
    assert r.status_code == 202 and r.json()["enrich"]["status"] == "refused"
    assert not rig.fake.calls


def test_retry_refused_while_a_job_or_claim_runs(rig):
    make_book_on_disk(settings.data_dir, book_id=BID)
    ingest_upload.write_status(BID, "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    for s in (main._enrich_jobs, main._kind_claims):
        s.add(BID)
        try:
            assert rig.post(f"/api/books/{BID}/enrich").status_code == 409
        finally:
            s.discard(BID)
    assert not rig.fake.calls


def test_retry_running_book_is_409(rig):
    make_book_on_disk(settings.data_dir, book_id=BID)
    main._upload_status[BID] = {"status": "ready", "enrich": {"status": "running", "stage": "cast"}}
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 409


def test_retry_only_for_uploads(rig):
    make_book_on_disk(settings.data_dir, book_id="classic")
    ingest_upload.write_status("classic", "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    assert rig.post("/api/books/classic/enrich").status_code == 403


def test_retry_per_user_permissions(pu, monkeypatch):  # noqa: F811
    client, _ = pu
    monkeypatch.setattr(main, "_deleted", set())   # other tests' deleted ids
    started = []

    async def fake_run(book_id, resume=False):
        started.append((book_id, resume))
    monkeypatch.setattr(main, "_run_enrichment", fake_run)
    ingest_upload.write_status("u-alice-1", "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    assert client.post("/api/books/u-alice-1/enrich", headers=h(token(BOB))).status_code == 404   # not his book
    assert client.post("/api/books/u-alice-1/enrich").status_code in (401, 403, 404)           # anonymous
    assert not started
    r = client.post("/api/books/u-alice-1/enrich", headers=h(token(ALICE)))
    assert r.status_code == 202 and started == [("u-alice-1", True)]


# ---------- cap bypass: cap stops are final, the per-book cap is cumulative, retries are limited ----------

def test_cap_stop_is_recorded_as_refused_and_not_retryable(rig, monkeypatch):
    monkeypatch.setattr(settings, "upload_total_cap_usd", 0.0001)   # the first call pushes the total past the cap
    make_book_on_disk(settings.data_dir, book_id=BID)
    ingest_upload.write_status(BID, "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    monkeypatch.setattr(pipeline, "check_caps", lambda *a, **k: None)   # let the job start; the meter then stops it
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 202
    assert st(BID)["status"] == "refused" and st(BID)["message"] == pipeline.BUDGET_USED
    calls = len(rig.fake.calls)
    assert rig.post(f"/api/books/{BID}/enrich").status_code == 409   # refused: no retry
    assert len(rig.fake.calls) == calls


def test_per_book_cap_is_cumulative(data_dir, monkeypatch):
    from app.enrich import spend
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1.0)
    assert pipeline.check_caps(0.6, book_id=BID) is None
    spend.add(BID, "gemini", 0.7)
    assert pipeline.check_caps(0.6, book_id=BID) == pipeline.TOO_LONG   # 0.7 already spent + 0.6 > 1.0
    assert pipeline.check_caps(0.2, book_id=BID) is None
    assert pipeline.check_caps(0.6, book_id="u-other-cccccccc") is None   # other books are unaffected
    assert spend.book_usd(BID) == pytest.approx(0.7)


def test_meter_stops_on_cumulative_book_spend(data_dir, monkeypatch):
    from app.enrich import spend
    make_book_on_disk(data_dir)
    monkeypatch.setattr(settings, "upload_book_cap_usd", 1.0)
    spend.add(BID, "fake", 1.26)   # earlier runs already used the whole allowance (cap * margin = 1.25)
    ok, events, _ = run_pipeline(BID)
    assert not ok and events[-1]["status"] == "refused" and events[-1]["message"] == pipeline.TOO_LONG


def test_retry_and_resume_use_the_cumulative_check(rig, monkeypatch):
    from app.enrich import spend
    stale(BID)
    spend.add(BID, "fake", 3.999)   # cap 4.0: nothing left for this book's estimate
    asyncio.run(main._resume_enrichments())
    assert st(BID)["status"] == "refused" and not rig.fake.calls
    make_book_on_disk(settings.data_dir, book_id=BID2)
    ingest_upload.write_status(BID2, "ready", enrich={"status": "error", "message": pipeline.FAILED})
    spend.add(BID2, "fake", 3.999)
    main._upload_status.clear()
    r = rig.post(f"/api/books/{BID2}/enrich")
    assert r.json()["enrich"]["status"] == "refused" and not rig.fake.calls


def test_retries_are_limited_and_persisted(rig, monkeypatch):
    make_book_on_disk(settings.data_dir, book_id=BID)

    class Boom(FakeProvider):
        async def complete(self, system, user, schema, *a):
            raise ValueError("boom")
    monkeypatch.setattr(pipeline, "make_llm", lambda *a, **k: make_llm(Boom()))
    monkeypatch.setattr(settings, "upload_total_cap_usd", 1000.0)
    ingest_upload.write_status(BID, "ready", enrich={"status": "error", "message": pipeline.FAILED})
    main._upload_status.clear()
    for n in range(1, main.MAX_ENRICH_RETRIES + 1):
        r = rig.post(f"/api/books/{BID}/enrich")
        assert r.status_code == 202
        assert ingest_upload.read_status(BID)["enrich_retries"] == n   # survives the job's own status writes
        main._upload_status.clear()   # as after a restart: the count comes from status.json
        assert st(BID)["status"] == "error"
    r = rig.post(f"/api/books/{BID}/enrich")
    assert r.status_code == 409 and "several times" in r.json()["detail"]
