import base64
import io
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.boson import BosonClient
from app.config import settings
from app.ratelimit import TokenBucket
from app.schema import Book, Character, Paragraph, Section, TtsChunk
from app.tts_cache import align_timestamps, cache_key

REAL_KEY = "real-secret-key-123"


def book_with_chunk():
    text = "It was in March, 1888, that Harry spoke. He smiled."
    return Book(
        id="demo", title="Demo", author="A", kind="novel", license="PD",
        sections=[Section(id="s1", title="I.", start_para=0)],
        paragraphs=[
            Paragraph(idx=0, section_id="s1", text=text, tts_chunks=[
                TtsChunk(id="p0c0", speaker="narrator", voice="marcus",
                         tts_text="It was in March, eighteen eighty-eight, that Harry spoke.", char_start=0, char_end=40)]),
            Paragraph(idx=1, section_id="s1", text="He smiled and left."),
            Paragraph(idx=2, section_id="s1", text="Later the King arrived."),
        ],
        characters=[Character(id="harry", name="Harry", first_para=0, voice="oliver", persona="Precise."),
                    Character(id="king", name="The King", first_para=2, voice="jake", persona="Proud.")],
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "boson_api_key", REAL_KEY)
    (tmp_path / "cache").mkdir()
    d = tmp_path / "books" / "demo"
    d.mkdir(parents=True)
    (d / "book.json").write_text(book_with_chunk().model_dump_json(), encoding="utf-8")
    main._index_for.cache_clear()
    monkeypatch.setattr(main, "token_bucket", TokenBucket(3, 60))
    monkeypatch.setattr(main, "tts_bucket", TokenBucket(1000, 60))   # fresh limiter state per test
    monkeypatch.setattr(main, "clone_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "retrieve_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(settings, "access_token", "")
    calls = {"n": 0}

    def handler(req: httpx.Request):
        calls["n"] += 1
        if req.url.path == "/v1/realtime/client_secrets":
            return httpx.Response(200, json={"value": f"bai-eph-{calls['n']}", "expires_at": 1, "session": {"id": "s"}})
        if req.url.path == "/v1/audio/speech":
            return httpx.Response(200, json={
                "audio": base64.b64encode(b"MP3DATA").decode(), "response_format": "mp3", "input": "x",
                "timestamps": [{"word": "It", "start": 0.0, "end": 0.2}, {"word": "was", "start": 0.2, "end": 0.4}]})
        if req.url.path == "/v1/audio/voices":
            return httpx.Response(200, json={"voice": "voice_abc", "ref_text": "t"})
        return httpx.Response(404, json={"error": {"type": "invalid_request_error", "message": "no"}})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return TestClient(main.app), calls


def test_token_is_fresh_each_time_and_never_the_real_key(env):
    client, _ = env
    a, b = client.post("/api/realtime/token").json(), client.post("/api/realtime/token").json()
    assert a["value"] != b["value"] and a["value"].startswith("bai-eph-")
    assert REAL_KEY not in client.post("/api/realtime/token").text
    assert set(a) == {"value", "expires_at"}  # session object is dropped


def test_token_endpoint_is_rate_limited(env):
    client, _ = env
    codes = [client.post("/api/realtime/token").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_token_bucket_refills():
    t = [0.0]
    bucket = TokenBucket(2, 10, clock=lambda: t[0])
    assert bucket.allow("a") and bucket.allow("a") and not bucket.allow("a")
    assert bucket.allow("b")  # separate client
    t[0] = 6  # 1.2 tokens refilled
    assert bucket.allow("a")


def test_tts_second_call_is_a_cache_hit(env):
    client, calls = env
    first = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"}).json()
    second = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"}).json()
    assert first["cached"] is False and second["cached"] is True
    assert calls["n"] == 1, "Boson is called once"
    assert first["audio_url"] == second["audio_url"]
    assert (settings.data_dir / "cache" / "tts" / first["audio_url"].split("/")[-1]).read_bytes() == b"MP3DATA"
    assert first["timestamps"][0]["char_start"] == 0


def test_tts_voice_override_gets_its_own_cache_entry(env):
    client, calls = env
    client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"})
    client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "nora"})
    assert calls["n"] == 2


def test_tts_unknown_chunk_and_missing_key(env, monkeypatch):
    client, _ = env
    assert client.post("/api/tts", json={"book_id": "demo", "chunk_id": "nope"}).status_code == 404
    monkeypatch.setattr(settings, "boson_api_key", "")
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "nora"})
    assert r.status_code == 503 and "BOSON_API_KEY" in r.json()["detail"]


def test_cache_key_is_stable_and_sensitive():
    assert cache_key("hi", "marcus") == cache_key("hi", "marcus")
    assert cache_key("hi", "marcus") != cache_key("hi", "nora")
    assert cache_key("hi", "marcus") != cache_key("hi!", "marcus")
    assert cache_key("hi", "marcus", timestamps=False) != cache_key("hi", "marcus")


def test_retrieve_never_returns_future_paragraphs(env):
    client, _ = env
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "King arrived", "reader_para": 1, "speaker_id": "harry"})
    assert r.status_code == 200
    body = r.json()
    assert all(p["para_end"] <= 1 for p in body["passages"])
    assert "Later the King arrived" not in body["instructions"]
    assert "Harry" in body["instructions"] and body["persona"] == "Precise."


def test_retrieve_refuses_a_character_not_yet_met(env):
    client, _ = env
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "hi", "reader_para": 1, "speaker_id": "king"})
    assert r.status_code == 403
    assert client.post("/api/retrieve", json={"book_id": "demo", "query": "hi", "reader_para": 2, "speaker_id": "king"}).status_code == 200
    assert client.post("/api/retrieve", json={"book_id": "demo", "query": "hi", "reader_para": 2, "speaker_id": "ghost"}).status_code == 404


def silent_wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def clone(client, consent="true", audio=None, transcript="hello there"):
    return client.post("/api/voices/clone", data={"transcript": transcript, "name": "me", "consent": consent},
                       files={"audio": ("a.wav", audio if audio is not None else silent_wav(4), "audio/wav")})


def test_clone_requires_consent(env):
    client, calls = env
    assert clone(client, consent="false").status_code == 400
    assert clone(client, consent="").status_code == 400
    assert calls["n"] == 0, "Boson is never called without consent"


def test_clone_validates_length_and_transcript(env):
    client, _ = env
    assert "at least 3" in clone(client, audio=silent_wav(1)).json()["detail"]
    assert clone(client, transcript="  ").status_code == 400


def test_clone_success_is_stored(env):
    client, _ = env
    r = clone(client)
    assert r.status_code == 200 and r.json()["voice"] == "voice_abc"
    assert client.get("/api/voices").json() == {"me": "voice_abc"}


def test_align_handles_numbers_tags_and_null():
    plain = "It was in March, 1888, that Harry spoke."
    words = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(
        "It was in March eighteen eighty-eight that Harry spoke".split())]
    out = align_timestamps(words, plain)
    assert len(out) == len(words)
    assert plain[out[0]["char_start"]:out[0]["char_end"]] == "It"
    assert plain[out[4]["char_start"]:out[4]["char_end"]] == "1888,"  # spoken number maps onto the digits
    assert plain[out[7]["char_start"]:out[7]["char_end"]] == "Harry"
    assert all(a["char_start"] <= b["char_start"] for a, b in zip(out, out[1:])), "highlighting never moves backwards"
    assert align_timestamps(None, plain) is None and align_timestamps([], plain) is None
    tagged = [{"word": "<|emotion:anger|>", "start": 0, "end": 0}] + words[:2]
    assert len(align_timestamps(tagged, plain)) == 2  # tag token skipped


# ---------- hardening: cost abuse, resource bounds, access control ----------

def test_tts_rejects_unknown_voice_without_calling_boson(env):
    client, calls = env
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "../../evil"})
    assert r.status_code == 400 and "Unknown voice" in r.json()["detail"]
    assert calls["n"] == 0


def test_tts_accepts_a_registered_cloned_voice(env):
    client, calls = env
    assert clone(client).status_code == 200  # registers voice_abc
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "voice_abc"})
    assert r.status_code == 200 and r.json()["voice"] == "voice_abc"


def test_new_audio_is_rate_limited_but_cache_hits_are_free(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(main, "tts_bucket", TokenBucket(2, 60))
    voices = ["marcus", "nora", "jake"]
    codes = [client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": v}).status_code for v in voices]
    assert codes == [200, 200, 429]
    again = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0", "voice_override": "marcus"})
    assert again.status_code == 200 and again.json()["cached"] is True  # a cached chunk never consumes the budget


def test_access_token_gates_endpoints_that_spend_credit(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(settings, "access_token", "letmein")
    body = {"book_id": "demo", "chunk_id": "p0c0"}
    assert client.post("/api/tts", json=body).status_code == 401
    assert client.post("/api/realtime/token").status_code == 401
    assert clone(client).status_code == 401
    assert calls["n"] == 0
    h = {"X-Talkback-Token": "letmein"}
    assert client.post("/api/tts", json=body, headers=h).status_code == 200
    assert client.post("/api/realtime/token", headers=h).status_code == 200
    assert client.post("/api/tts", json=body, headers={"X-Talkback-Token": "wrong"}).status_code == 401
    assert client.get("/api/books").status_code == 200  # reading stays open


def test_clone_upload_is_read_with_a_bound_and_rejected_when_too_big(env, monkeypatch):
    client, calls = env
    from app import voices as v
    monkeypatch.setattr(v, "MAX_BYTES", 1000)
    r = clone(client, audio=b"x" * 5000)
    assert r.status_code == 400 and "10 MB" in r.json()["detail"]
    assert calls["n"] == 0


def test_clone_is_rate_limited(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "clone_bucket", TokenBucket(1, 60))
    assert clone(client).status_code == 200
    assert clone(client).status_code == 429


def test_retrieve_query_length_is_capped(env):
    client, _ = env
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "x" * 501, "reader_para": 1})
    assert r.status_code == 422


def test_cached_only_never_calls_boson_and_serves_prerendered(env):
    client, calls = env
    body = {"book_id": "demo", "chunk_id": "p0c0", "cached_only": True}
    r = client.post("/api/tts", json=body)
    assert r.status_code == 409 and "pre-render" in r.json()["detail"]
    assert calls["n"] == 0
    client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"})  # render once normally
    n = calls["n"]
    r = client.post("/api/tts", json=body)
    assert r.status_code == 200 and r.json()["cached"] is True
    assert calls["n"] == n


def test_health_tells_the_ui_when_an_access_code_is_needed_without_leaking_it(env, monkeypatch):
    client, _ = env
    assert client.get("/api/health").json()["access_required"] is False
    monkeypatch.setattr(settings, "access_token", "letmein")
    body = client.get("/api/health").json()
    assert body["access_required"] is True
    assert "letmein" not in client.get("/api/health").text


def test_align_tag_residue_merged_into_first_word():
    # Real Boson output (probe 2026-10-17): delivery tags arrive glued onto the first spoken word.
    plain = "To Harry Quill she is always the woman."
    words = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(
        ["emotioncontemplationprosodypitchlowTo", "Harry", "Quill", "she", "is", "always", "the", "woman"])]
    out = align_timestamps(words, plain)
    assert [plain[o["char_start"]:o["char_end"]] for o in out] == plain.rstrip(".").split() [:-1] + ["woman."]


def test_retrieve_is_gated_and_rate_limited(env, monkeypatch):
    client, _ = env
    body = {"book_id": "demo", "query": "King", "reader_para": 1, "speaker_id": "harry"}
    monkeypatch.setattr(settings, "access_token", "letmein")
    assert client.post("/api/retrieve", json=body).status_code == 401
    h = {"X-Talkback-Token": "letmein"}
    monkeypatch.setattr(main, "retrieve_bucket", TokenBucket(1, 60))
    assert client.post("/api/retrieve", json=body, headers=h).status_code == 200
    assert client.post("/api/retrieve", json=body, headers=h).status_code == 429


@pytest.mark.parametrize("retrieval,answer,expected", [
    ("local", None, "realtime"), ("vertex", None, "gemini"),
    ("vertex", "realtime", "realtime"), ("local", "gemini", "gemini"),
])
def test_health_reports_answer_backend(env, monkeypatch, retrieval, answer, expected):
    client, _ = env
    monkeypatch.setattr(settings, "retrieval_backend", retrieval)
    monkeypatch.setattr(settings, "answer_backend", answer)
    assert client.get("/api/health").json()["answer_backend"] == expected
