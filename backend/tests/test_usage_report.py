"""Usage report math plus the call sites that feed it (tts_cache, preprocess/llm). No network."""
import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "preprocess"))
import usage as report  # noqa: E402
import llm  # noqa: E402

from app import tts_cache  # noqa: E402
from app.config import settings  # noqa: E402

LINES = [
    {"ts": "2026-10-18T10:00:00+00:00", "kind": "embed", "model": "gemini-embedding-001", "chars": 4_000_000},
    {"ts": "2026-10-18T11:00:00+00:00", "kind": "llm", "model": "gemini-2.5-flash", "prompt_tokens": 1_000_000, "output_tokens": 100_000},
    {"ts": "2026-10-19T09:00:00+00:00", "kind": "llm", "model": "gemini-2.5-flash-lite", "prompt_tokens": 2_000_000, "output_tokens": 1_000_000},
    {"ts": "2026-10-19T09:30:00+00:00", "kind": "llm", "model": "gemini-9-ultra", "prompt_tokens": 5, "output_tokens": 5},
    {"ts": "2026-10-19T10:00:00+00:00", "kind": "anthropic", "model": "claude-haiku-5-5", "input_tokens": 10, "output_tokens": 5, "usd": 1.25},
    {"ts": "2026-10-19T10:05:00+00:00", "kind": "anthropic", "model": "claude-sonnet-5-5", "input_tokens": 1_000_000, "output_tokens": 0},  # no usd: falls back to prices
    {"ts": "2026-10-19T11:00:00+00:00", "kind": "boson_tts", "model": "higgs-tts-3", "chars": 2000},
    {"ts": "2026-10-19T11:01:00+00:00", "kind": "boson_realtime_session", "model": "boson-realtime", "sessions": 1},
    {"ts": "2026-10-19T11:02:00+00:00", "kind": "boson_realtime_session", "model": "boson-realtime", "sessions": 1},
]


@pytest.fixture
def usage_file(tmp_path):
    p = tmp_path / "usage.jsonl"
    body = [json.dumps(x) for x in LINES]
    body.insert(3, "{this is not json")
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def test_load_skips_corrupt_and_handles_missing(usage_file, tmp_path):
    rows, skipped = report.load(usage_file)
    assert len(rows) == len(LINES) and skipped == 1
    assert report.load(tmp_path / "nope.jsonl") == ([], 0)


def test_cost_math_and_totals(usage_file):
    rows, _ = report.load(usage_file)
    s = report.summarize(rows, {"claude-sonnet-5-5": (2.0, 10.0)})
    # embed: 4M chars = 1M tokens * 0.15 = 0.15; flash: 0.30 + 0.25 = 0.55; lite: 0.20 + 0.40 = 0.60; unknown model adds nothing
    assert s["gcp"]["cost"] == pytest.approx(0.15 + 0.55 + 0.60)
    assert s["gcp"]["unknown"] == 1 and s["gcp"]["calls"] == 4
    assert s["gcp"]["models"][("llm", "gemini-9-ultra")]["cost"] == 0 and s["gcp"]["models"][("llm", "gemini-9-ultra")]["unknown"] == 1
    assert s["anthropic"]["cost"] == pytest.approx(1.25 + 2.0)
    assert s["boson"]["cost"] == pytest.approx(0.03)
    assert s["boson"]["models"][("boson_realtime_session", "boson-realtime")]["sessions"] == 2
    assert s["gcp"]["days"]["2026-10-18"]["cost"] == pytest.approx(0.70)
    assert set(s["gcp"]["days"]) == {"2026-10-18", "2026-10-19"}


def test_render_shows_unknown_remaining_and_disclaimer(usage_file, capsys):
    assert report.main(["--file", str(usage_file), "--gcp-budget", "50", "--anthropic-budget", "77.33"]) == 0
    out = capsys.readouterr().out
    assert "gemini-9-ultra" in out and "unknown" in out
    assert "$48.7000" not in out and "$48.70" in out  # 50 - 1.30
    assert "estimates" in out and "billing console" in out
    assert "skipped 1 unreadable" in out
    boson_line = next(x for x in out.splitlines() if x.strip().startswith("Boson") and "$" in x)
    assert boson_line.rstrip().endswith("-")  # no boson budget: no remaining figure


def test_boson_budget_remaining(usage_file, capsys):
    report.main(["--file", str(usage_file), "--boson-budget", "10"])
    assert "$9.97" in capsys.readouterr().out


def test_empty_file_prints_message(tmp_path, capsys):
    report.main(["--file", str(tmp_path / "none.jsonl")])
    assert "No usage recorded" in capsys.readouterr().out


# ---------- call sites ----------

def lines(tmp_path):
    p = tmp_path / "cache" / "usage.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class FakeBoson:
    def __init__(self):
        self.calls = 0

    async def create_speech(self, text, voice, **kw):
        self.calls += 1
        body = {"audio": base64.b64encode(b"mp3").decode(), "timestamps": None}
        return SimpleNamespace(json=lambda: body)


def test_tts_cache_records_on_miss_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    client = FakeBoson()
    first = asyncio.run(tts_cache.get_or_create(client, "Hello there.", "oliver", "Hello there."))
    second = asyncio.run(tts_cache.get_or_create(client, "Hello there.", "oliver", "Hello there."))
    assert first["cached"] is False and second["cached"] is True and client.calls == 1
    recs = lines(tmp_path)
    assert len(recs) == 1
    assert recs[0]["kind"] == "boson_tts" and recs[0]["model"] == "higgs-tts-3" and recs[0]["chars"] == len("Hello there.")


class Out(BaseModel):
    x: int


class FakeAnthropic:
    def __init__(self):
        self.messages = self
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=200), stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text='{"x": 1}')])


def test_llm_records_real_calls_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(llm, "CACHE", tmp_path / "llmcache")
    llm.CACHE.mkdir()
    fake = FakeAnthropic()
    monkeypatch.setattr(llm, "client", lambda: fake)
    a = asyncio.run(llm.call_structured("claude-haiku-5-5", "sys", "user", Out))
    b = asyncio.run(llm.call_structured("claude-haiku-5-5", "sys", "user", Out))  # cache hit
    assert a == b and fake.calls == 1
    recs = lines(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["kind"] == "anthropic" and r["model"] == "claude-haiku-5-5"
    assert r["input_tokens"] == 1000 and r["output_tokens"] == 200
    assert r["usd"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)


def test_realtime_token_mint_records_a_session(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from app import main
    from app.boson import BosonClient
    from app.ratelimit import TokenBucket

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "boson_api_key", "k")
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(main, "token_bucket", TokenBucket(5, 60))
    handler = lambda req: httpx.Response(200, json={"value": "bai-eph-1", "expires_at": 1})  # noqa: E731
    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    assert TestClient(main.app).post("/api/realtime/token").status_code == 200
    recs = lines(tmp_path)
    assert len(recs) == 1 and recs[0]["kind"] == "boson_realtime_session" and recs[0]["sessions"] == 1
