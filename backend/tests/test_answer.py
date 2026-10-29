import json

import pytest

from app import deps, indexes, tts_cache
from app.routes import talk as talk_routes
from app.personas import build_answer_prompt

from .test_api import book_with_chunk, env  # noqa: F401  (env fixture)


class FakeAnswerer:
    def __init__(self, sentences, fail_after=None):
        self.sentences, self.fail_after, self.prompts = sentences, fail_after, []

    async def stream_sentences(self, system, user, on_usage=None, **kw):  # kw: web_search, on_sources
        self.prompts.append((system, user))
        for i, s in enumerate(self.sentences):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("boom: secret-token-xyz")
            yield s


def lines(r):
    return [json.loads(x) for x in r.text.splitlines()]


def post(client, **kw):
    body = {"book_id": "demo", "query": "What happened?", "reader_para": 1, "speaker_id": "harry"}
    body.update(kw)
    return client.post("/api/answer", json=body)


def test_answer_streams_sentences_then_done(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["He smiled.", "Then he left."])
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: fake)
    r = post(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    out = lines(r)
    assert [o["type"] for o in out] == ["sentence", "sentence", "done"]
    assert out[0]["text"] == "He smiled." and out[1]["text"] == "Then he left."
    assert out[2]["passages"] == 1 and isinstance(out[2]["retrieve_ms"], int) and isinstance(out[2]["first_sentence_ms"], int)
    assert fake.prompts[0][1] == "What happened?"


class LeakyIndex:
    mode = "fake"

    async def asearch(self, query, reader_para, k=4, *, spoiler_safe=True):
        from app.retrieval import Chunk
        return [Chunk(id="ok", para_start=0, para_end=0, text="ELIGIBLE-TEXT-ALPHA"),
                Chunk(id="bad", para_start=2, para_end=2, text="FUTURE-TEXT-OMEGA")]


def test_answer_prompt_never_contains_future_paragraphs(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["Ok."])
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: fake)
    monkeypatch.setattr(indexes, "_index_for", lambda book_id: LeakyIndex())
    out = lines(post(client, query="King arrived", reader_para=1))
    system, _ = fake.prompts[0]
    assert "ELIGIBLE-TEXT-ALPHA" in system
    assert "FUTURE-TEXT-OMEGA" not in system
    assert out[-1]["type"] == "done"


def test_answer_requires_access_token_and_is_rate_limited(env, monkeypatch):
    from app.config import settings
    from app.ratelimit import TokenBucket
    client, _ = env
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: FakeAnswerer(["x."]))
    monkeypatch.setattr(settings, "access_token", "letmein")
    assert post(client).status_code == 401
    assert client.post("/api/answer", headers={"x-talkback-token": "letmein"},
                       json={"book_id": "demo", "query": "q", "reader_para": 1}).status_code == 200
    monkeypatch.setattr(settings, "access_token", "")
    monkeypatch.setattr(deps, "answer_bucket", TokenBucket(1, 60))
    assert post(client).status_code == 200
    r = post(client)
    assert r.status_code == 429 and "Too many questions" in r.json()["detail"]


def test_speak_requires_access_token(env, monkeypatch):
    from app.config import settings
    client, _ = env
    monkeypatch.setattr(settings, "access_token", "letmein")
    assert client.post("/api/speak", json={"text": "Hi.", "voice": "marcus"}).status_code == 401


def test_answer_checks_match_retrieve(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: FakeAnswerer(["x."]))
    assert post(client, speaker_id="king", reader_para=1).status_code == 403
    assert post(client, speaker_id="ghost").status_code == 404
    assert post(client, book_id="nope").status_code == 404
    assert post(client, query="x" * 501).status_code == 422


def test_answer_error_mid_stream_is_a_clean_error_line(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: FakeAnswerer(["One.", "Two."], fail_after=1))
    out = lines(post(client))
    assert [o["type"] for o in out] == ["sentence", "error"]
    assert "secret" not in out[1]["message"] and "boom" not in out[1]["message"]


def test_answer_error_before_first_sentence(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: FakeAnswerer(["One."], fail_after=0))
    out = lines(post(client))
    assert [o["type"] for o in out] == ["error"]
    assert out[0]["message"] == "The answer could not be generated; please try again."


def test_build_answer_prompt_reuses_template_wording():
    system, user = build_answer_prompt(book_with_chunk(), "harry", "", [], "Why?")
    assert "Speak as Harry" in system and "spoken" in system and user == "Why?"


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
    monkeypatch.setattr(deps, "tts_bucket", type("B", (), {"allow": lambda s, k: pytest.fail("bucket used")})())
    r = client.post("/api/speak", json={"text": "Hi.", "voice": "marcus"})
    assert r.status_code == 200 and r.json()["cached"] is True


def test_speak_is_rate_limited_on_misses(env, monkeypatch):
    from app.ratelimit import TokenBucket
    client, _ = env
    monkeypatch.setattr(deps, "tts_bucket", TokenBucket(1, 60))
    assert client.post("/api/speak", json={"text": "One.", "voice": "marcus"}).status_code == 200
    assert client.post("/api/speak", json={"text": "Two.", "voice": "marcus"}).status_code == 429


def test_build_answer_prompt_with_history_keeps_system_prompt_and_adds_turns():
    hist = [{"role": "user", "text": "Who are you?"}, {"role": "character", "text": "I am Harry."}]
    system, user = build_answer_prompt(book_with_chunk(), "harry", "", [], "And your age?", history=hist)
    plain, _ = build_answer_prompt(book_with_chunk(), "harry", "", [], "And your age?")
    assert system == plain  # story facts still come only from the reader-position prompt
    assert "Reader: Who are you?" in user and "You: I am Harry." in user and user.endswith("And your age?")


def test_group_round_history_names_the_other_characters():
    # #79: another character's reply in the same round is shown under their name (one line), this character's as "You"
    hist = [{"role": "character", "text": "I said yes."}, {"role": "character", "name": "The\nCat: x", "text": "I grinned."}]
    _, user = build_answer_prompt(book_with_chunk(), "harry", "", [], "And you?", history=hist)
    assert "You: I said yes." in user and "The Cat x: I grinned." in user


def test_answer_endpoint_takes_named_history_turns(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["One."])
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: fake)
    r = post(client, history=[{"role": "character", "name": "Alice", "text": "Curiouser."}])
    assert r.status_code == 200
    assert "Alice: Curiouser." in fake.prompts[0][1]
    assert post(client, history=[{"role": "character", "name": "x" * 81, "text": "t"}]).status_code == 422


def test_answer_endpoint_passes_history_to_the_model(env, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["One."])
    monkeypatch.setattr(talk_routes, "get_answerer", lambda: fake)
    r = post(client, history=[{"role": "user", "text": "Earlier question"}])
    assert r.status_code == 200
    assert "Reader: Earlier question" in fake.prompts[0][1]
    assert post(client, history=[{"role": "user", "text": "x"}] * 9).status_code == 422


# ---------- non-English answers (#135) ----------

def test_language_codes_are_the_documented_102():
    from app import tts_lang
    assert len(tts_lang.LANGUAGES) == 102 and "spa" in tts_lang.LANGUAGES and "fra" in tts_lang.LANGUAGES
    assert tts_lang.valid("fra") and not tts_lang.valid("fr") and not tts_lang.valid("xxx") and not tts_lang.valid("")
    assert tts_lang.is_english("eng") and not tts_lang.is_english("fra")
    assert tts_lang.has_timestamps("eng") and not tts_lang.has_timestamps("spa") and not tts_lang.has_timestamps("cmn")
    assert not tts_lang.has_timestamps("fra")


def test_speak_rejects_unknown_language(env):
    client, _ = env
    r = client.post("/api/speak", json={"text": "Bonjour.", "voice": "marcus", "language": "fr"})
    assert r.status_code == 422
    assert client.post("/api/speak", json={"text": "Bonjour.", "voice": "marcus", "language": "zzz"}).status_code == 422


def _capture(monkeypatch, audio=b"RAWMP3"):
    import httpx
    from app.boson import BosonClient
    bodies = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("timestamps"):
            import base64
            return httpx.Response(200, json={"audio": base64.b64encode(audio).decode(), "timestamps": None})
        return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(deps, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return bodies


def test_speak_french_sends_language_no_timestamps_and_normalizes(env, monkeypatch):
    client, _ = env
    bodies = _capture(monkeypatch)
    r = client.post("/api/speak", json={"text": "Il a 3 chats.", "voice": "marcus", "language": "fra"})
    assert r.status_code == 200
    out = r.json()
    assert out["timestamps"] is None and out["cached"] is False
    assert len(bodies) == 1
    b = bodies[0]
    assert b["language"] == "fra" and not b.get("timestamps") and b.get("normalize", True) is True
    from app.config import settings
    assert (settings.data_dir / "cache" / "tts" / out["audio_url"].rsplit("/", 1)[1]).read_bytes() == b"RAWMP3"


def test_speak_english_keeps_timestamps_and_spanish_normalizes(env, monkeypatch):
    client, _ = env
    bodies = _capture(monkeypatch)
    client.post("/api/speak", json={"text": "Hello there.", "voice": "marcus"})
    client.post("/api/speak", json={"text": "Hello again.", "voice": "marcus", "language": "eng"})
    client.post("/api/speak", json={"text": "El 3 de mayo.", "voice": "marcus", "language": "spa"})
    assert [bool(b.get("timestamps")) for b in bodies] == [True, True, False]
    assert "language" not in bodies[0] and "normalize" not in bodies[0]  # English path unchanged
    assert "language" not in bodies[1]
    assert bodies[2]["language"] == "spa" and bodies[2]["normalize"] is True


def test_cache_key_includes_language_and_leaves_english_unchanged():
    base = tts_cache.cache_key("Hi.", "v")
    assert base == tts_cache.cache_key("Hi.", "v", language=None)
    assert base == tts_cache.cache_key("Hi.", "v", language="eng")
    assert tts_cache.cache_key("Hi.", "v", timestamps=False, language="fra") != tts_cache.cache_key("Hi.", "v", timestamps=False, language="deu")
    assert tts_cache.cache_key("Hi.", "v", timestamps=False, language="fra") != base


def test_speak_french_cached_second_time(env, monkeypatch):
    client, _ = env
    bodies = _capture(monkeypatch)
    body = {"text": "Bonjour tout le monde.", "voice": "marcus", "language": "fra"}
    assert client.post("/api/speak", json=body).json()["cached"] is False
    assert client.post("/api/speak", json=body).json()["cached"] is True
    assert len(bodies) == 1
