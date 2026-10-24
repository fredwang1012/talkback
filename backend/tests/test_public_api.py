"""The public processing API (server side): route allowlist, job lifecycle, caps, rate limits, size limits, logging."""
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest_upload, public_api
from app.config import settings
from app.enrich import pipeline, spend
from app.enrich.llm import LLM
from app.personas import build_answer_prompt
from app.ratelimit import TokenBucket
from app.schema import Book, Character

from .test_novel_pipeline import FakeProvider, story

MARKER = "ZXQ-SECRET-BOOK-TEXT-42"
BACKEND = Path(__file__).resolve().parents[1]


def enrich_body(raw: str | None = None, **kw) -> dict:
    book = ingest_upload.build_book("u-x-aaaaaaaa", "Test", "Me", "novel", raw or story(2, 8))
    return {"title": book.title, "author": book.author, "kind": "novel",
            "sections": [{"id": s.id, "title": s.title, "start_para": s.start_para} for s in book.sections],
            "paragraphs": [{"idx": p.idx, "section_id": p.section_id, "text": p.text} for p in book.paragraphs]} | kw


class FakeAnswerer:
    def __init__(self, sentences=("He smiled.", "Then he left."), fail=False):
        self.sentences, self.fail, self.prompts = sentences, fail, []

    async def stream_sentences(self, system, user, on_usage=None):
        self.prompts.append((system, user))
        if self.fail:
            raise RuntimeError(f"boom {MARKER}")
        for s in self.sentences:
            yield s
        if on_usage:
            on_usage(1000, 100)


@pytest.fixture
def pub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "trust_proxy", False)
    monkeypatch.setattr(public_api, "_jobs", {})
    monkeypatch.setattr(public_api, "enrich_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "poll_bucket", TokenBucket(10_000, 60))
    monkeypatch.setattr(public_api, "answer_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())
    monkeypatch.setattr(spend, "_state", None)
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": FakeProvider()}))
    monkeypatch.setattr(public_api, "get_public_answerer", lambda: FakeAnswerer())
    with TestClient(public_api.app) as c:
        yield c


def wait_done(c, job_id, timeout=20):
    end = time.time() + timeout
    seen = []
    while time.time() < end:
        v = c.get(f"/api/public/enrich/{job_id}").json()
        seen.append(v)
        if v["status"] != "running":
            return v, seen
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---------- route allowlist ----------

def test_only_the_public_routes_exist(pub):
    paths = {r.path for r in public_api.app.routes if hasattr(r, "methods")}
    assert paths == {"/api/health", "/api/public/enrich", "/api/public/enrich/{job_id}", "/api/public/answer"}
    assert pub.get("/api/health").json() == {"ok": True, "public": True}
    for path in ["/", "/index.html", "/api/books", "/api/books/demo", "/api/answer", "/api/retrieve", "/api/tts",
                 "/api/realtime/token", "/media/tts/a.mp3", "/media/avatar/a.mp4", "/portraits/a.png", "/docs",
                 "/redoc", "/openapi.json", "/api/voices", "/api/books/upload"]:
        assert pub.get(path).status_code == 404, path
        assert pub.post(path, json={}).status_code in (404, 405), path


def test_public_mode_never_imports_the_main_app():
    code = ("import sys; from app.asgi import app; "
            "print(sorted(r.path for r in app.routes if hasattr(r, 'methods')), 'app.main' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(BACKEND), timeout=90,
                         env={**os.environ, "PUBLIC_API": "true"})
    assert out.returncode == 0, out.stderr
    assert "/api/public/enrich" in out.stdout and "'/api/books" not in out.stdout and out.stdout.strip().endswith("False")


# ---------- enrich job lifecycle ----------

def test_enrich_job_lifecycle(pub):
    r = pub.post("/api/public/enrich", json=enrich_body())
    assert r.status_code == 202
    job = r.json()["job_id"]
    assert r.json()["estimate_usd"] > 0
    view, seen = wait_done(pub, job)
    assert view["status"] == "done" and view["progress"] == 1.0 and view["degraded"] is None
    prog = [v["progress"] for v in seen]
    assert prog == sorted(prog)
    res = view["result"]
    assert [c["id"] for c in res["characters"]] == ["alice", "bob"]
    assert all(c["main"] is True and "portrait" not in c for c in res["characters"])
    assert res["narrator"]["voice"] == "nora"
    assert res["summaries"] and any(len(v) > 1 for v in res["segments"].values())
    assert spend.total_usd() > 0                      # the cumulative cap sees this job
    assert (settings.data_dir / "usage" / "upload_spend.json").is_file()


def test_unknown_and_expired_jobs_are_404(pub):
    assert pub.get("/api/public/enrich/nope").status_code == 404
    job = pub.post("/api/public/enrich", json=enrich_body()).json()["job_id"]
    wait_done(pub, job)
    public_api._jobs[job].created -= public_api.JOB_TTL_SECONDS + 5
    r = pub.get(f"/api/public/enrich/{job}")
    assert r.status_code == 404 and r.json()["code"] == "unknown_job"


def test_gemini_failure_is_an_error_job_without_details(pub, monkeypatch):
    class Boom(FakeProvider):
        async def complete(self, *a, **k):
            raise RuntimeError(f"upstream {MARKER}")
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": Boom()}))
    job = pub.post("/api/public/enrich", json=enrich_body()).json()["job_id"]
    view, _ = wait_done(pub, job)
    assert view["status"] == "error" and view["code"] == "failed" and MARKER not in json.dumps(view)


def test_busy_when_too_many_jobs_run(pub, monkeypatch):
    monkeypatch.setattr(public_api, "MAX_RUNNING_JOBS", 0)
    r = pub.post("/api/public/enrich", json=enrich_body())
    assert r.status_code == 429 and r.json()["code"] == "busy"


# ---------- caps ----------

def test_per_book_cap(pub, monkeypatch):
    monkeypatch.setattr(settings, "public_book_cap_usd", 0.0000001)
    r = pub.post("/api/public/enrich", json=enrich_body())
    assert r.status_code == 402 and r.json()["code"] == "book_cap"


def test_cumulative_cap_refuses_at_the_door(pub):
    spend.add("earlier", "gemini", settings.public_total_cap_usd)
    r = pub.post("/api/public/enrich", json=enrich_body())
    assert r.status_code == 402 and r.json()["code"] == "cap"


def test_cumulative_cap_stops_a_running_job(pub, monkeypatch):
    monkeypatch.setattr(pipeline, "estimate", lambda book, provider=None: 0.0)  # the estimate passes, real spend does not
    monkeypatch.setattr(settings, "public_total_cap_usd", 0.00001)
    job = pub.post("/api/public/enrich", json=enrich_body()).json()["job_id"]
    view, _ = wait_done(pub, job)
    assert view["status"] == "error" and view["code"] == "cap" and "result" not in view


def test_daily_answer_cap_is_persisted_and_resets_next_day(pub, monkeypatch):
    body = {"title": "T", "speaker_id": "narrator", "query": "q"}
    assert pub.post("/api/public/answer", json=body).status_code == 200
    assert public_api.answer_budget.spent() > 0
    path = settings.data_dir / "usage" / "public_answer_spend.json"
    assert json.loads(path.read_text())["usd"] == public_api.answer_budget.spent()
    monkeypatch.setattr(settings, "public_answer_daily_cap_usd", 0.0000001)
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())  # a restarted instance reads the file
    r = pub.post("/api/public/answer", json=body)
    assert r.status_code == 402 and r.json()["code"] == "daily_cap"
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    path.write_text(json.dumps({"day": yesterday, "usd": 99}))
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())
    assert pub.post("/api/public/answer", json=body).status_code == 200


# ---------- rate limits ----------

def test_enrich_and_answer_are_rate_limited_per_ip(pub, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    monkeypatch.setattr(public_api, "enrich_bucket", TokenBucket(1, 3600))
    a, b = {"x-forwarded-for": "9.9.9.9, 1.1.1.1"}, {"x-forwarded-for": "9.9.9.9, 2.2.2.2"}
    assert pub.post("/api/public/enrich", json=enrich_body(), headers=a).status_code == 202
    r = pub.post("/api/public/enrich", json=enrich_body(), headers=a)
    assert r.status_code == 429 and r.json()["code"] == "rate"
    assert pub.post("/api/public/enrich", json=enrich_body(), headers=b).status_code == 202  # another address
    monkeypatch.setattr(public_api, "answer_bucket", TokenBucket(1, 60))
    body = {"title": "T", "query": "q"}
    assert pub.post("/api/public/answer", json=body).status_code == 200
    assert pub.post("/api/public/answer", json=body).status_code == 429


# ---------- size limits ----------

def test_size_limits(pub):
    big = enrich_body()
    big["paragraphs"][0]["text"] = "x" * 6000
    r = pub.post("/api/public/enrich", json=big)
    assert r.status_code == 422 and "xxxxx" not in r.text         # the error never echoes the text
    many = enrich_body()
    many["paragraphs"] = [{"idx": i, "section_id": "s", "text": "t"} for i in range(public_api.MAX_PARAGRAPHS + 1)]
    many["sections"] = []
    assert pub.post("/api/public/enrich", json=many).status_code == 422
    bad_idx = enrich_body()
    bad_idx["paragraphs"][1]["idx"] = 7
    assert pub.post("/api/public/enrich", json=bad_idx).status_code == 422
    json_h = {"content-type": "application/json"}
    assert pub.post("/api/public/enrich", content=b"x" * (public_api.ENRICH_BODY_LIMIT + 1), headers=json_h).status_code == 413
    assert pub.post("/api/public/answer", content=b"x" * (public_api.ANSWER_BODY_LIMIT + 1), headers=json_h).status_code == 413
    assert pub.post("/api/public/answer", json={"title": "T", "query": "q" * 501}).status_code == 422
    assert pub.post("/api/public/answer", json={"title": "T", "query": "q", "speaker_id": "../x"}).status_code == 422
    too_many = {"title": "T", "query": "q", "passages": [{"para_start": 0, "para_end": 0, "text": "t"}] * 13}
    assert pub.post("/api/public/answer", json=too_many).status_code == 422


def test_total_text_cap(pub, monkeypatch):
    monkeypatch.setattr(public_api, "MAX_TOTAL_CHARS", 100)
    assert pub.post("/api/public/enrich", json=enrich_body()).status_code == 422


# ---------- answers ----------

def test_answer_prompt_matches_the_local_prompt(pub, monkeypatch):
    fake = FakeAnswerer()
    monkeypatch.setattr(public_api, "get_public_answerer", lambda: fake)
    passages = [{"para_start": 0, "para_end": 1, "text": "He smiled."},
                {"book": "Book One", "para_start": 3, "para_end": 3, "text": "Old."}]
    body = {"title": "Demo", "kind": "novel", "speaker_id": "harry", "speaker_name": "Harry", "persona": "Precise.",
            "story_so_far": "Things happened.", "passages": passages, "query": "What now?", "spoilers": False}
    r = pub.post("/api/public/answer", json=body)
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert [x["type"] for x in lines] == ["sentence", "sentence", "done"]
    book = Book(id="demo", title="Demo", author="A", kind="novel", license="x", sections=[], paragraphs=[],
                characters=[Character(id="harry", name="Harry", first_para=0, voice="oliver", persona="Precise.")])
    assert fake.prompts[0] == build_answer_prompt(book, "harry", "Things happened.", passages, "What now?")
    body["spoilers"], body["speaker_id"] = True, "narrator"
    pub.post("/api/public/answer", json=body)
    assert fake.prompts[1] == build_answer_prompt(book, "narrator", "Things happened.", passages, "What now?", spoilers=True)


# ---------- never log request bodies ----------

def test_request_bodies_are_never_logged(pub, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    raw = story(2, 8).replace("m1x", MARKER)
    r = pub.post("/api/public/enrich", json=enrich_body(raw, title=f"T {MARKER}"))
    wait_done(pub, r.json()["job_id"])
    monkeypatch.setattr(public_api, "get_public_answerer", lambda: FakeAnswerer(fail=True))
    out = pub.post("/api/public/answer", json={"title": MARKER, "query": MARKER, "story_so_far": MARKER,
                                               "passages": [{"para_start": 0, "para_end": 0, "text": MARKER}]})
    assert out.status_code == 200 and "error" in out.text and MARKER not in out.text
    bad = enrich_body(raw)
    bad["paragraphs"][0]["idx"] = 5
    assert MARKER not in pub.post("/api/public/enrich", json=bad).text
    assert caplog.records and MARKER not in caplog.text
