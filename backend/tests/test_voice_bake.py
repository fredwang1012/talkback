"""Voice consistency (#69): bake preset + tags into a custom Boson voice used by read mode and Talkback."""
import base64
import json

import httpx
import pytest

from app import cast, main, voice_bake, voices
from app.boson import BosonClient
from app.config import settings
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment
from app.ttschunks import chunk_paragraph
from tests.test_api import env  # noqa: F401  (fixture)

TAG = "<|prosody:pitch_high|>"


def make_book() -> Book:
    p = Paragraph(idx=0, section_id="s1", text='"Dobby is free," said Dobby. He bowed.',
                  segments=[Segment(speaker="dobby", text='"Dobby is free,"'),
                            Segment(speaker="narrator", text="said Dobby. He bowed.")])
    b = Book(id="hp2", title="T", author="A", kind="novel", license="x", sections=[Section(id="s1", title="1", start_para=0)],
             paragraphs=[p], narrator=Narrator(voice="marcus"),
             characters=[Character(id="dobby", name="Dobby", first_para=0, voice="jake", voice_tags=TAG, persona="p"),
                         Character(id="minor", name="Minor", first_para=0, voice="nora", persona="p", main=False)])
    p.tts_chunks = chunk_paragraph(p, b)
    return b


def mock_client():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if req.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=b"RIFFwav")
        return httpx.Response(200, json={"voice": f"voice_{len(calls)}"})

    return BosonClient(transport=httpx.MockTransport(handler)), calls


@pytest.fixture(autouse=True)
def key(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(settings, "boson_api_key", "k")
    monkeypatch.setattr(settings, "data_dir", tmp_path_factory.mktemp("bake"))  # voices.json never hits real data


def test_sample_uses_preset_and_tags_and_is_about_ten_seconds():
    b = make_book()
    text = voice_bake.sample_text(b.characters[0])
    assert text.startswith(TAG) and 150 < len(text) < 260  # ~10 s at 15-20 chars/s


def test_voice_key_is_deterministic_and_sensitive():
    k = voice_bake.voice_key("hp2", "dobby", "jake", TAG)
    assert k == voice_bake.voice_key("hp2", "dobby", "jake", TAG)
    assert k != voice_bake.voice_key("hp2", "dobby", "jake", "")


@pytest.mark.asyncio
async def test_bake_renders_registers_and_applies():
    client, calls = mock_client()
    b = make_book()
    done = await voice_bake.bake_book(client, b)
    assert list(done) == ["dobby"]  # the minor character is not baked
    speech = [x for x in calls if x[0] == "/v1/audio/speech"]
    assert len(speech) == 1 and speech[0][1]["voice"] == "jake" and speech[0][1]["input"].startswith(TAG)
    assert speech[0][1]["response_format"] == "wav"
    create = [x for x in calls if x[0] == "/v1/audio/voices"][0][1]
    assert base64.b64decode(create["ref_audio"]) == b"RIFFwav" and create["description"].startswith("tb-hp2-dobby-")
    d = b.characters[0]
    assert d.voice == done["dobby"] and d.voice_tags == "" and (d.base_voice, d.base_voice_tags) == ("jake", TAG)
    assert voices.is_known_voice(d.voice)
    dobby_chunks = [c for c in b.paragraphs[0].tts_chunks if c.speaker == "dobby"]
    assert dobby_chunks and all(c.voice == d.voice and TAG not in c.tts_text for c in dobby_chunks)
    assert all(c.voice == "marcus" for c in b.paragraphs[0].tts_chunks if c.speaker == "narrator")


@pytest.mark.asyncio
async def test_rerun_is_idempotent_and_free():
    client, calls = mock_client()
    b = make_book()
    await voice_bake.bake_book(client, b)
    n = len(calls)
    assert await voice_bake.bake_book(client, b) == {}  # already baked
    fresh = make_book()  # e.g. a rebuilt book: the registered deterministic name is reused without paid calls
    again = await voice_bake.bake_book(client, fresh)
    assert len(calls) == n and again["dobby"] == fresh.characters[0].voice


@pytest.mark.asyncio
async def test_first_person_narrator_follows_the_baked_voice():
    client, _ = mock_client()
    b = make_book()
    b.narrator = Narrator(voice="jake", is_character="dobby")
    await voice_bake.bake_book(client, b)
    assert b.narrator.voice == b.characters[0].voice


def test_estimate_prices_tts_only():
    b = make_book()
    (e,) = voice_bake.estimate([b])
    assert e.characters == ["dobby"] and e.chars == len(voice_bake.sample_text(b.characters[0]))
    assert e.usd == round(e.chars / 1000 * 0.015, 4)
    voice_bake.apply_voice(b, b.characters[0], "voice_x")
    assert voice_bake.estimate([b])[0].characters == []


def test_tts_applies_the_cast_pitch_tag_override(env, monkeypatch):  # noqa: F811
    client, _ = env
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content)["input"])
        return httpx.Response(200, json={"audio": base64.b64encode(b"x").decode(), "response_format": "mp3", "input": "x"})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    p = settings.data_dir / "books" / "demo" / "book.json"
    book = json.loads(p.read_text(encoding="utf-8"))
    book["characters"][0]["voice_tags"] = TAG
    book["paragraphs"][1]["tts_chunks"] = [{"id": "p1c0", "speaker": "harry", "voice": "oliver", "tts_text": TAG + "He smiled.",
                                           "char_start": 0, "char_end": 9}]
    p.write_text(json.dumps(book), encoding="utf-8")
    main._index_for.cache_clear()
    # full cast is gone (#93): an old client's "cast" is two voices, where per-character pitch tags are dropped
    assert client.put("/api/books/demo/cast", json={"mode": "cast", "characters": {"harry": {"tags": "<|prosody:speed_slow|>"}}}).status_code == 200
    assert client.get("/api/books/demo/cast").json()["characters"]["harry"]["tags"] == "<|prosody:speed_slow|>"
    assert client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p1c0"}).status_code == 200
    assert sent[-1] == "He smiled."
    assert client.put("/api/books/demo/cast", json={"characters": {"harry": {"tags": "<|bogus|>"}}}).status_code == 400
    assert cast.VOICE_TAGS[0] == ""
