"""Face box for the head-and-shoulders crop (#109). The Gemini call is always a fake here: no live calls."""
import asyncio
import json

import pytest

from app import deps, face_detect
from app.config import settings
from app.ratelimit import TokenBucket
from tests.test_upload import client  # noqa: F401  (client is a fixture)

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeGemini:
    def __init__(self, answer="", fail=False):
        self.calls, self.answer, self.fail = [], answer, fail

    async def __call__(self, image, mime):
        self.calls.append((image, mime))
        if self.fail:
            raise RuntimeError("boom")
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


@pytest.fixture
def gem(monkeypatch):
    """Gemini on (a fake), with room in the buckets, the daily cap and the budget; spend recorded in `added`."""
    fake = FakeGemini({"found": True, "x": 0.4, "y": 0.2, "w": 0.2, "h": 0.25})
    added = []
    monkeypatch.setattr(face_detect, "_gemini", fake)
    monkeypatch.setattr(settings, "gcp_project", "test-project")
    monkeypatch.setattr(face_detect.spend, "add", lambda key, provider, usd, **k: added.append(usd) or 0.0)
    monkeypatch.setattr(face_detect.spend, "total_usd", lambda: 0.0)
    monkeypatch.setattr(face_detect, "_llm_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(face_detect, "_daily", {"day": "", "n": 0})
    fake.added = added
    return fake


def detect(data=PNG, ext="png"):
    return asyncio.run(face_detect.detect(data, ext))


def test_a_face_box_is_returned_and_the_estimate_reserved_first(gem):
    assert detect() == {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.25}
    assert gem.calls == [(PNG, "image/png")]
    assert len(gem.added) >= 1 and gem.added[0] > 0


@pytest.mark.parametrize("answer", ["not json", "{", "[]", '{"found": true}', '{"found": true, "x": "a", "y": 0, "w": 1, "h": 1}'])
def test_bad_json_means_no_face(gem, answer):
    gem.answer = answer
    assert detect() is None


def test_no_face(gem):
    gem.answer = {"found": False, "x": 0, "y": 0, "w": 0, "h": 0}
    assert detect() is None


def test_a_failed_call_means_no_face(gem):
    gem.fail = True
    assert detect() is None


@pytest.mark.parametrize("box, want", [
    ((-0.1, 0.2, 0.5, 0.3), (0.0, 0.2, 0.4, 0.3)),     # off the left edge
    ((0.8, 0.9, 0.5, 0.5), (0.8, 0.9, 0.2, 0.1)),      # off the right and bottom edges
    ((0.1, 0.1, 1.5, 1.5), (0.1, 0.1, 0.9, 0.9)),      # too big
])
def test_out_of_range_boxes_are_clamped(gem, box, want):
    gem.answer = dict(found=True, **dict(zip("xywh", box)))
    got = detect()
    assert [round(got[k], 6) for k in "xywh"] == list(want)


def test_a_per_mille_box_is_scaled():
    # Gemini's native box scale is 0..1000; a box clearly on that scale is read as such
    assert face_detect.clamp_box(250, 100, 500, 600) == {"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.6}


def test_an_empty_or_inverted_box_is_no_face():
    assert face_detect.clamp_box(0.5, 0.5, 0, 0.2) is None
    assert face_detect.clamp_box(0.5, 0.5, -0.2, 0.2) is None
    assert face_detect.clamp_box(1.2, 0.5, 0.2, 0.2) is None   # wholly outside


def test_no_call_without_a_gcp_project(gem, monkeypatch):
    monkeypatch.setattr(settings, "gcp_project", "")
    assert detect() is None and gem.calls == []


def test_no_call_when_the_budget_cannot_cover_the_estimate(gem, monkeypatch):
    monkeypatch.setattr(face_detect.spend, "total_usd", lambda: settings.upload_total_cap_usd - 1e-9)
    assert detect() is None and gem.calls == []


def test_a_daily_cap_on_calls(gem, monkeypatch):
    monkeypatch.setattr(face_detect, "DAILY_LLM_CALLS", 2)
    for _ in range(3):
        detect()
    assert len(gem.calls) == 2


def test_a_global_bucket(gem, monkeypatch):
    monkeypatch.setattr(face_detect, "_llm_bucket", TokenBucket(1, 60))
    detect(), detect()
    assert len(gem.calls) == 1


def test_endpoint(client, gem):  # noqa: F811
    r = client.post("/api/faces/detect", files={"image": ("me.png", PNG)})
    assert r.status_code == 200 and r.json() == {"face": {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.25}}
    gem.answer = {"found": False, "x": 0, "y": 0, "w": 0, "h": 0}
    assert client.post("/api/faces/detect", files={"image": ("me.png", PNG)}).json() == {"face": None}
    assert not (settings.data_dir / "portraits").exists()   # nothing stored


def test_endpoint_validation_gating_and_rate_limit(client, gem, monkeypatch):  # noqa: F811
    assert client.post("/api/faces/detect", files={"image": ("a.gif", b"GIF89a...")}).status_code == 400
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)
    assert client.post("/api/faces/detect", files={"image": ("a.png", big)}).status_code == 400
    monkeypatch.setattr(deps, "face_detect_bucket", TokenBucket(1, 60))
    assert client.post("/api/faces/detect", files={"image": ("a.png", PNG)}).status_code == 200
    assert client.post("/api/faces/detect", files={"image": ("a.png", PNG)}).status_code == 429
    monkeypatch.setattr(settings, "access_token", "sesame")
    assert client.post("/api/faces/detect", files={"image": ("a.png", PNG)}).status_code == 401
