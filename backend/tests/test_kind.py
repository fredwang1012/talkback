"""Auto-detected book kind (novel / textbook), the kind-change endpoint and its enrichment hand-off."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import books, ingest_upload, main
from app.config import settings
from app.enrich import detect as detect_mod
from app.enrich import pipeline, spend
from app.enrich.llm import LLM, Raw
from app.ratelimit import TokenBucket
from app.schema import Book

from .test_novel_pipeline import FakeProvider, story

DIALOGUE = "\n\n".join(
    '"Where are you going?" asked Alice, pulling her shawl tight. "Home," said Bob. "It is late."\n\n'
    'She laughed and the rain kept falling on the road. "Wait for me," she cried, and ran after him.'
    for _ in range(40))

TEXTBOOK = "\n\n".join(
    f"{c}.{s} Cell membranes\n\nLearning objectives: after reading this section you can describe the membrane. "
    f"Figure {c}.{s} shows the lipid bilayer. Table {c}.{s} lists the main transport proteins and their roles in the cell.\n\n"
    "Review questions\n\n1. Define osmosis. 2. Explain diffusion across a membrane.\n\nChapter summary: membranes regulate transport."
    for c in range(1, 4) for s in range(1, 5))


def test_heuristic_novel_vs_textbook():
    assert detect_mod.heuristic(DIALOGUE)[0] == "novel"
    kind, conf = detect_mod.heuristic(TEXTBOOK)
    assert kind == "textbook" and 0.5 <= conf <= 0.9
    assert detect_mod.heuristic(story())[0] == "novel"
    assert detect_mod.heuristic("")[0] == "novel"  # tie-break


def test_sample_covers_start_middle_end():
    text = "A" * 5000 + "B" * 5000 + "C" * 5000
    s = detect_mod.sample(text)
    assert len(s) < 3100 and s.startswith("A") and s.endswith("C") and "B" in s
    assert detect_mod.sample("short") == "short"


class KindProvider(FakeProvider):
    def __init__(self, kind="textbook", confidence=0.93, fail=False, **kw):
        super().__init__(**kw)
        self.kind, self.confidence, self.fail = kind, confidence, fail
        self.tier = None

    async def complete(self, system, user, schema, tier, max_tokens, effort):
        if schema["title"] != "KindGuess":
            return await super().complete(system, user, schema, tier, max_tokens, effort)
        self.calls.append("KindGuess")
        self.users.append(("KindGuess", user))
        self.tier = tier
        if self.fail:
            raise RuntimeError("boom")
        return Raw(text=json.dumps({"kind": self.kind, "confidence": self.confidence}), in_tokens=900, out_tokens=12,
                   model="fake")


def llm_for(fake):
    return LLM("claude", {"claude": fake})


def test_llm_path_uses_fast_tier_and_sample():
    fake = KindProvider("textbook", 0.9)
    out = asyncio.run(detect_mod.detect_kind(DIALOGUE, llm=llm_for(fake)))
    assert out == ("textbook", 0.9, "llm")  # the model wins over the heuristic
    assert fake.tier == "fast" and len(fake.users[0][1]) < 3100


def test_llm_failure_falls_back_to_heuristic():
    out = asyncio.run(detect_mod.detect_kind(TEXTBOOK, llm=llm_for(KindProvider(fail=True))))
    assert out[0] == "textbook" and out[2] == "heuristic"


def test_enrichment_off_uses_heuristic_without_any_llm(monkeypatch):
    monkeypatch.setattr(settings, "upload_enrich", False)
    monkeypatch.setattr(detect_mod, "make_llm", lambda *a, **k: pytest.fail("no LLM call expected"))
    out = asyncio.run(detect_mod.detect_kind(DIALOGUE))
    assert (out[0], out[2]) == ("novel", "heuristic")


def test_spent_budget_skips_the_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(settings, "upload_total_cap_usd", 0.0)
    monkeypatch.setattr(detect_mod, "make_llm", lambda *a, **k: pytest.fail("no LLM call expected"))
    assert asyncio.run(detect_mod.detect_kind(TEXTBOOK))[2] == "heuristic"


# ---------- API ----------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(settings, "upload_confirm", False)
    main._upload_status.clear()
    monkeypatch.setattr(main, "_ingest_guard", main.IngestGuard())
    monkeypatch.setattr(main, "upload_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "_enrich_lock", asyncio.Lock())
    main._index_for.cache_clear()
    fake = KindProvider("textbook", 0.9)
    monkeypatch.setattr(pipeline, "make_llm", lambda *a, **k: LLM("claude", {"claude": fake}))
    monkeypatch.setattr(detect_mod, "make_llm", lambda *a, **k: LLM("claude", {"claude": fake}))
    c = TestClient(main.app)
    c.fake = fake
    return c


def upload(client, kind="auto", raw=None):
    return client.post("/api/books/upload", files={"file": ("s.txt", (raw or story()).encode())},
                       data={"title": "Story", "author": "Me", "kind": kind})


def test_auto_is_the_default_and_stores_kind_and_detection(client):
    r = client.post("/api/books/upload", files={"file": ("s.txt", story().encode())}, data={"title": "Story"})
    assert r.status_code == 202
    book_id = r.json()["book_id"]
    st = client.get(f"/api/books/{book_id}/status").json()
    det = {"kind": "textbook", "confidence": 0.9, "method": "llm"}
    assert st == {"status": "ready", "kind": "textbook", "kind_detected": det}  # textbook: no enrichment
    book = client.get(f"/api/books/{book_id}").json()
    assert book["kind"] == "textbook" and book["kind_detected"] == det
    listed = next(b for b in client.get("/api/books").json() if b["id"] == book_id)
    assert listed["kind"] == "textbook" and listed["kind_detected"] == det
    assert client.fake.calls == ["KindGuess"]
    assert spend.total_usd() > 0  # the detection call counts towards the caps


def test_auto_novel_by_heuristic_when_llm_fails_starts_enrichment(client):
    client.fake.fail = True
    book_id = upload(client, raw=DIALOGUE).json()["book_id"]
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["kind"] == "novel" and st["kind_detected"]["method"] == "heuristic"
    assert st["enrich"]["status"] == "done"
    assert "MainCast" in client.fake.calls


def test_explicit_kind_skips_detection(client):
    book_id = upload(client, kind="textbook").json()["book_id"]
    assert "KindGuess" not in client.fake.calls
    book = client.get(f"/api/books/{book_id}").json()
    assert book["kind"] == "textbook" and book["kind_detected"] is None


def test_old_books_without_kind_detected_still_load(client):
    book_id = upload(client, kind="novel").json()["book_id"]
    path = settings.data_dir / "books" / book_id / "book.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("kind_detected")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Book.model_validate_json(path.read_text(encoding="utf-8")).kind_detected is None


# ---------- kind change ----------

def test_change_kind_gating_and_builtin_refusal(client, monkeypatch):
    assert client.post("/api/books/hp1-sorcerers-stone/kind", json={"kind": "textbook"}).status_code == 403
    assert client.post("/api/books/u-nope-12345678/kind", json={"kind": "textbook"}).status_code == 404
    book_id = upload(client, kind="textbook").json()["book_id"]
    assert client.post(f"/api/books/{book_id}/kind", json={"kind": "bogus"}).status_code == 422
    monkeypatch.setattr(settings, "access_token", "sesame")
    assert client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"}).status_code in (401, 403)
    ok = client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"}, headers={"X-Talkback-Token": "sesame"})
    assert ok.status_code == 202


def test_textbook_to_novel_starts_enrichment(client):
    book_id = upload(client, kind="textbook").json()["book_id"]
    assert not client.fake.calls
    r = client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"})
    assert r.status_code == 202
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["kind"] == "novel" and st["enrich"]["status"] == "done"
    book = client.get(f"/api/books/{book_id}").json()
    assert book["kind"] == "novel" and book["characters"] and book["summaries"]


def test_textbook_to_novel_respects_caps_and_confirm(client, monkeypatch):
    book_id = upload(client, kind="textbook").json()["book_id"]
    monkeypatch.setattr(settings, "upload_book_cap_usd", 0.00001)
    client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"})
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["enrich"] == {"status": "refused", "message": pipeline.TOO_LONG} and not client.fake.calls
    assert client.get(f"/api/books/{book_id}").json()["kind"] == "novel"  # still moved, narrator only

    other = upload(client, kind="textbook").json()["book_id"]
    monkeypatch.setattr(settings, "upload_book_cap_usd", 4.0)
    monkeypatch.setattr(settings, "upload_confirm", True)
    client.post(f"/api/books/{other}/kind", json={"kind": "novel"})
    st = client.get(f"/api/books/{other}/status").json()
    assert st["enrich"]["status"] == "awaiting_confirmation" and st["enrich"]["estimate_usd"] > 0
    assert not client.fake.calls
    assert client.post(f"/api/books/{other}/enrich").status_code == 202


def test_novel_to_textbook_keeps_characters(client):
    book_id = upload(client, kind="novel").json()["book_id"]
    assert client.get(f"/api/books/{book_id}").json()["characters"]
    calls = len(client.fake.calls)
    assert client.post(f"/api/books/{book_id}/kind", json={"kind": "textbook"}).status_code == 202
    book = client.get(f"/api/books/{book_id}").json()
    assert book["kind"] == "textbook" and book["characters"]
    st = client.get(f"/api/books/{book_id}/status").json()
    assert st["status"] == "ready" and "enrich" not in st and len(client.fake.calls) == calls
    # and back: the data is still there, so no new LLM work
    client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"})
    assert len(client.fake.calls) == calls and client.get(f"/api/books/{book_id}").json()["kind"] == "novel"


def test_change_kind_refused_while_enrichment_runs(client):
    book_id = upload(client, kind="textbook").json()["book_id"]
    main._upload_status[book_id] = ingest_upload.write_status(
        book_id, "ready", enrich={"status": "running", "stage": "segment", "progress": 0.3})
    r = client.post(f"/api/books/{book_id}/kind", json={"kind": "novel"})
    assert r.status_code == 409
    assert books.load_book(book_id).kind == "textbook"
