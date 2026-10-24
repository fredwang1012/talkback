import json

import pytest

from app import main, tts_cache
from app.personas import build_answer_prompt

from .test_api import book_with_chunk, env  # noqa: F401  (env fixture)


class FakeAnswerer:
    def __init__(self, sentences, fail_after=None):
        self.sentences, self.fail_after, self.prompts = sentences, fail_after, []

    async def stream_sentences(self, system, user):
        self.prompts.append((system, user))
        for i, s in enumerate(self.sentences):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("boom: secret-token-xyz")
            yield s


def lines(r):
    return [json.loads(x) for x in r.text.splitlines()]


def post(client, **kw):
    body = {"book_id": "demo", "query": "What happened?", "reader_para": 1, "speaker_id": "holmes"}
    body.update(kw)
    return client.post("/api/answer", json=body)


def test_answer_streams_sentences_then_done(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["He smiled.", "Then he left."])
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    r = post(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    out = lines(r)
    assert [o["type"] for o in out] == ["sentence", "sentence", "done"]
    assert out[0]["text"] == "He smiled." and out[1]["text"] == "Then he left."
    assert out[2]["passages"] >= 0 and isinstance(out[2]["retrieve_ms"], int) and isinstance(out[2]["first_sentence_ms"], int)
    assert fake.prompts[0][1] == "What happened?"


def test_answer_prompt_never_contains_future_paragraphs(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["Ok."])
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    post(client, query="King arrived", reader_para=1)
    system, _ = fake.prompts[0]
    assert "Later the King arrived" not in system
    assert "He smiled and left" in system or "Holmes" in system


def test_answer_checks_match_retrieve(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "get_answerer", lambda: FakeAnswerer(["x."]))
    assert post(client, speaker_id="king", reader_para=1).status_code == 403
    assert post(client, speaker_id="ghost").status_code == 404
    assert post(client, book_id="nope").status_code == 404
    assert post(client, query="x" * 501).status_code == 422


def test_answer_error_mid_stream_is_a_clean_error_line(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "get_answerer", lambda: FakeAnswerer(["One.", "Two."], fail_after=1))
    out = lines(post(client))
    assert [o["type"] for o in out] == ["sentence", "error"]
    assert "secret" not in out[1]["message"] and "boom" not in out[1]["message"]


def test_answer_error_before_first_sentence(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "get_answerer", lambda: FakeAnswerer(["One."], fail_after=0))
    assert [o["type"] for o in lines(post(client))] == ["error"]


def test_build_answer_prompt_reuses_template_wording():
    system, user = build_answer_prompt(book_with_chunk(), "holmes", "", [], "Why?")
    assert "Speak as Holmes" in system and "spoken" in system and user == "Why?"


def test_speak_rejects_unknown_voice_and_bad_text(env):
    client, calls = env
    assert client.post("/api/speak", json={"text": "Hi.", "voice": "nobody"}).status_code == 400
    assert client.post("/api/speak", json={"text": "x" * 401, "voice": "marcus"}).status_code == 422
    assert client.post("/api/speak", json={"text": "", "voice": "marcus"}).status_code == 422


def test_speak_returns_tts_shape_and_caches(env):
    client, calls = env
    a = client.post("/api/speak", json={"text": "Hello there.", "voice": "marcus"}).json()
    b = client.post("/api/speak", json={"text": "Hello there.", "voice": "marcus"}).json()
    assert {"audio_url", "timestamps", "cached", "voice"} <= set(a)
    assert a["cached"] is False and b["cached"] is True and b["voice"] == "marcus"


def test_speak_cached_path_does_not_call_boson(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(tts_cache, "is_cached", lambda t, v: True)

    async def fake(boson, t, v, plain):
        return {"audio_url": "/media/x.mp3", "timestamps": None, "cached": True}

    monkeypatch.setattr(tts_cache, "get_or_create", fake)
    monkeypatch.setattr(main, "tts_bucket", type("B", (), {"allow": lambda s, k: pytest.fail("bucket used")})())
    r = client.post("/api/speak", json={"text": "Hi.", "voice": "marcus"})
    assert r.status_code == 200 and r.json()["cached"] is True


def test_speak_is_rate_limited_on_misses(env, monkeypatch):
    from app.ratelimit import TokenBucket
    client, _ = env
    monkeypatch.setattr(main, "tts_bucket", TokenBucket(1, 60))
    assert client.post("/api/speak", json={"text": "One.", "voice": "marcus"}).status_code == 200
    assert client.post("/api/speak", json={"text": "Two.", "voice": "marcus"}).status_code == 429
