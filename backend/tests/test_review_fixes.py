"""Review fixes for #119/#120: the trailing-punctuation scan is linear, and translation uses the Gemini region."""
import re
import time

from app import ingest_upload, multilingual
from app.config import settings


def test_trail_punct_len_matches_the_old_regex():
    old = re.compile(r"[\s,.;:]+$")
    for t in ["", "x", "x.", "x, ", " .", "...", "a;:\n", "a b", ".a", "x "]:
        m = old.search(t)
        assert ingest_upload._trail_punct_len(t) == (len(m.group()) if m else 0), t


def test_trail_punct_scan_is_linear_on_a_crafted_span():
    t0 = time.monotonic()
    assert ingest_upload._trail_punct_len("." * 400_000 + "a") == 0
    assert ingest_upload._trail_punct_len("a" + "." * 400_000) == 400_000
    assert time.monotonic() - t0 < 1.0


def test_translation_client_uses_the_gemini_region(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

    import google.genai as genai
    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(settings, "gcp_project", "p")
    monkeypatch.setattr(settings, "gemini_location", "global")
    monkeypatch.setattr(multilingual, "_client", None)
    import asyncio
    try:
        asyncio.run(multilingual.translate("¿Qué pasa?"))
    except Exception:
        pass   # the fake has no models API; only the constructor matters
    assert seen.get("location") == "global"
