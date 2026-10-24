"""Emotion in character answers (#14): the model prefixes a sentence with [emotion]; the server strips and validates it,
the screen gets clean text and the TTS gets <|emotion:X|>."""
import asyncio
import json

import pytest

from app import emotion, main, multilingual, public_api, public_client, tts_cache
from app.personas import (ANSWER_EMOTION_RULE, LANGUAGE_RULE, REALTIME_EMOTION_RULE, READER_TONE_RULE,
                          build_answer_prompt, build_instructions)
from app.ratelimit import TokenBucket
from app.schema import EMOTIONS, Book, Character
from app.config import settings

from .test_api import env  # noqa: F401  (env fixture)
from .test_answer import FakeAnswerer, lines, post


# ---------- marker parsing ----------

@pytest.mark.parametrize("raw, want", [
    ("[sadness] I miss him.", ("I miss him.", "sadness")),
    ("[Sadness]  I miss him.", ("I miss him.", "sadness")),
    ("  [ anger ] How dare he!", ("How dare he!", "anger")),
    ("[neutral] It was a Tuesday.", ("It was a Tuesday.", None)),
    ("It was a Tuesday.", ("It was a Tuesday.", None)),
    ("[grumpiness] Go away.", ("Go away.", None)),            # unknown: dropped, never shown
    ("[arousal] Hm.", ("Hm.", None)),                          # Boson has it, the books do not use it
    ("[emotion:fear] Run!", ("Run!", "fear")),
    ("<|emotion:fear|> Run!", ("Run!", "fear")),              # a model copying the Boson tag form
    ("[tristeza] Lo extraño mucho.", ("Lo extraño mucho.", None)),  # translated marker: stripped, dropped
    ("[sadness] Lo extraño mucho.", ("Lo extraño mucho.", "sadness")),
    ("［surprise］本当に？", ("本当に？", "surprise")),           # full-width brackets
    ("【悲伤】我很想他。", ("我很想他。", None)),
    ("[sadness I miss him.", ("I miss him.", None)),           # malformed (unclosed): stripped, no emotion
    ("[] Well.", ("Well.", None)),
    ("[sadness]", ("", "sadness")),                            # marker only
    ("I was [anger] furious.", ("I was furious.", None)),     # a stray marker inside is removed
    ("[relief] Safe. [elation] Home!", ("Safe. Home!", "relief")),
])
def test_split_marker(raw, want):
    assert emotion.split_marker(raw) == want


def test_every_book_emotion_is_accepted():
    for e in EMOTIONS:
        assert emotion.split_marker(f"[{e}] Hi.") == ("Hi.", e)


async def _agen(items):
    for s in items:
        yield s


def _collect(items):
    async def go():
        return [x async for x in emotion.tag_sentences(_agen(items))]
    return asyncio.run(go())


def test_tag_sentences_drops_marker_only_sentences_and_keeps_order():
    assert _collect(["[sadness] I miss him.", "[bogus] Still.", "[relief]", "  ", "Fine."]) == [
        ("I miss him.", "sadness"), ("Still.", None), ("Fine.", None)]


def test_marker_split_across_stream_chunks_is_reassembled_by_the_sentence_splitter():
    from app.gcp.llm import GeminiAnswerer

    class Chunk:
        def __init__(self, text):
            self.text, self.usage_metadata = text, None

    class Models:
        async def generate_content_stream(self, **kw):
            async def gen():
                for t in ["[sad", "ness] I miss", " him. [an", "ger] He lied", "! [relief]"]:
                    yield Chunk(t)
            return gen()

    class Client:
        class aio:
            models = Models()

    async def go():
        g = GeminiAnswerer(client=Client(), model="m")
        return [x async for x in emotion.tag_sentences(g.stream_sentences("s", "u"))]
    assert asyncio.run(go()) == [("I miss him.", "sadness"), ("He lied!", "anger")]


# ---------- TTS tag placement ----------

def test_tts_input_puts_the_emotion_tag_first_and_neutral_adds_nothing():
    assert emotion.tts_input("I miss him.", "sadness") == "<|emotion:sadness|>I miss him."
    assert emotion.tts_input("I miss him.", None) == "I miss him."
    assert emotion.tts_input("I miss him.", "bogus") == "I miss him."


def test_speak_sends_the_emotion_tag_to_boson_and_caches_per_emotion(env, monkeypatch):  # noqa: F811
    client, _ = env
    seen = []
    real = tts_cache.get_or_create

    async def spy(boson, tts_text, voice, plain):
        seen.append((tts_text, plain))
        return await real(boson, tts_text, voice, plain)

    monkeypatch.setattr(tts_cache, "get_or_create", spy)
    a = client.post("/api/speak", json={"text": "I miss him.", "voice": "marcus", "emotion": "sadness"})
    assert a.status_code == 200 and a.json()["cached"] is False
    assert seen[-1] == ("<|emotion:sadness|>I miss him.", "I miss him.")   # timestamps align to the plain words
    b = client.post("/api/speak", json={"text": "I miss him.", "voice": "marcus"})
    assert b.json()["cached"] is False and seen[-1][0] == "I miss him."   # neutral: the plain text, as before
    c = client.post("/api/speak", json={"text": "I miss him.", "voice": "marcus", "emotion": "<|style:singing|>"})
    assert c.status_code == 200 and c.json()["cached"] is True and seen[-1][0] == "I miss him."  # ignored, never injected


# ---------- prompts ----------

def _book():
    return Book(id="demo", title="Demo", author="A", kind="novel", license="x", sections=[], paragraphs=[],
                characters=[Character(id="harry", name="Harry", first_para=0, voice="oliver", persona="Precise.")])


def test_answer_prompt_has_the_emotion_rule_after_the_language_rule():
    system, _ = build_answer_prompt(_book(), "harry", "", [], "¿Qué pasó con el perro de Harry?")
    lang = LANGUAGE_RULE.format(lang=multilingual.foreign_language("¿Qué pasó con el perro de Harry?"))
    assert system.startswith(lang)
    assert ANSWER_EMOTION_RULE in system and system.index(ANSWER_EMOTION_RULE) > len(lang)
    assert "sadness" in ANSWER_EMOTION_RULE and "English" in ANSWER_EMOTION_RULE
    system_full, _ = build_answer_prompt(_book(), "harry", "", [], "What now?", spoilers=True)
    assert ANSWER_EMOTION_RULE in system_full


def test_answer_prompt_keeps_the_emotion_rule_under_the_length_cap():
    long = [{"para_start": i, "para_end": i, "text": "x" * 3000} for i in range(20)]
    system, _ = build_answer_prompt(_book(), "harry", "y" * 9000, long, "What now?")
    assert system.endswith(ANSWER_EMOTION_RULE) or ANSWER_EMOTION_RULE in system[-1000:]


def test_character_prompts_react_to_the_readers_tone_and_keep_spoiler_rules():
    safe = build_instructions(_book(), "harry", "", [])
    full = build_instructions(_book(), "harry", "", [], spoilers=True)
    assert READER_TONE_RULE in safe and READER_TONE_RULE in full
    assert "Never reveal, hint at, guess or speculate about what happens later" in safe


def test_realtime_instructions_ask_for_feeling_in_the_voice_after_the_language_rule(env):  # noqa: F811
    client, _ = env
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "¿Qué pasó con el perro de Harry?",
                                           "reader_para": 1, "speaker_id": "harry"}).json()
    ins = r["instructions"]
    assert REALTIME_EMOTION_RULE in ins and ins.startswith("The reader wrote in")
    assert ins.index(REALTIME_EMOTION_RULE) > ins.index("Answer only in")
    assert ANSWER_EMOTION_RULE not in ins   # no [markers] in audio-only answers
    r2 = client.post("/api/retrieve", json={"book_id": "demo", "query": "What happened?", "reader_para": 1,
                                            "speaker_id": "harry"}).json()
    assert r2["instructions"].startswith(REALTIME_EMOTION_RULE)


# ---------- /api/answer streams ----------

def test_local_answer_strips_markers_and_reports_the_emotion(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(main, "get_answerer", lambda: FakeAnswerer(["[sadness] I miss him.", "[bogus] Still.",
                                                                    "[relief]", "Fine."]))
    out = lines(post(client))
    assert [o["type"] for o in out] == ["sentence", "sentence", "sentence", "done"]
    assert out[0] == {"type": "sentence", "text": "I miss him.", "emotion": "sadness"}
    assert out[1] == {"type": "sentence", "text": "Still."} and out[2] == {"type": "sentence", "text": "Fine."}
    assert not any("[" in o.get("text", "") for o in out)


def test_public_service_strips_markers_and_reports_the_emotion(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from .test_public_api import FakeAnswerer as PubFake
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "trust_proxy", False)
    monkeypatch.setattr(public_api, "answer_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())
    monkeypatch.setattr(public_api, "ip_budget", public_api.IpBudget())
    monkeypatch.setattr(public_api, "_streams", {})
    monkeypatch.setattr(public_api, "_started_slots", {})
    fake = PubFake()
    fake.sentences = ["[anger] He lied!", "[confusion] Why?", "[made_up] So."]
    monkeypatch.setattr(public_api, "get_public_answerer", lambda: fake)
    body = {"title": "Demo", "kind": "novel", "speaker_id": "harry", "speaker_name": "Harry", "persona": "Precise.",
            "story_so_far": "Things happened.", "passages": [], "query": "What now?", "spoilers": False}
    with TestClient(public_api.app) as c:
        out = [json.loads(x) for x in c.post("/api/public/answer", json=body).text.splitlines()]
    assert out[:3] == [{"type": "sentence", "text": "He lied!", "emotion": "anger"},
                       {"type": "sentence", "text": "Why?", "emotion": "confusion"},
                       {"type": "sentence", "text": "So."}]
    assert ANSWER_EMOTION_RULE in fake.prompts[0][0]


def test_proxy_of_the_public_service_revalidates_its_emotion_and_strips_old_markers(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    monkeypatch.setattr(main, "answer_bucket", TokenBucket(1000, 60))

    async def fake_stream(payload, client=None):
        yield {"type": "sentence", "text": "He smiled.", "emotion": "elation"}
        yield {"type": "sentence", "text": "Odd.", "emotion": "<|sfx:cough|>"}
        yield {"type": "sentence", "text": "[fear] Run!"}   # a service that did not strip the marker

    monkeypatch.setattr(public_client, "stream_answer", fake_stream)
    out = lines(post(client))
    assert out[:3] == [{"type": "sentence", "text": "He smiled.", "emotion": "elation"},
                       {"type": "sentence", "text": "Odd."},
                       {"type": "sentence", "text": "Run!", "emotion": "fear"}]


def test_tts_cache_key_differs_by_emotion():
    assert tts_cache.cache_key(emotion.tts_input("Hi.", "anger"), "v") != tts_cache.cache_key("Hi.", "v")


def test_closing_the_tagged_stream_closes_the_answerer_so_usage_is_still_recorded():
    closed = []

    async def answerer():
        try:
            yield "[anger] One."
            yield "Two."
        finally:
            closed.append(True)

    async def go():
        gen = emotion.tag_sentences(answerer())
        assert await gen.__anext__() == ("One.", "anger")
        await gen.aclose()
    asyncio.run(go())
    assert closed == [True]
