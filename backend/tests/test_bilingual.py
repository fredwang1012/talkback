"""Bilingual reading (B3): each paragraph translated lazily with Gemini (cached per book + paragraph + language, per
account), then voiced in the same voice with Boson's `language` parameter."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, mp3dur, translate, users
from app.boson import BosonClient
from app.config import settings
from app.ratelimit import TokenBucket
from app.schema import Book, Character, Paragraph, Section, TtsChunk
from app.ttschunks import MAX_CHARS, split_translation


def _book():
    p0 = "Alice was tired. “Oh dear!” said the Rabbit."
    return Book(
        id="demo", title="Demo", author="A", kind="novel", license="PD",
        sections=[Section(id="s1", title="I.", start_para=0)],
        paragraphs=[
            Paragraph(idx=0, section_id="s1", text=p0, tts_chunks=[
                TtsChunk(id="p0c0", speaker="narrator", voice="marcus", tts_text="Alice was tired.",
                         char_start=0, char_end=16),
                TtsChunk(id="p0c1", speaker="rabbit", voice="jake",
                         tts_text="<|prosody:pitch_high|><|emotion:fear|><|sfx:laughter|>“Oh dear!” said the Rabbit.",
                         char_start=17, char_end=len(p0))]),
            Paragraph(idx=1, section_id="s1", text="x = 1", kind="code", tts_chunks=[
                TtsChunk(id="p1c0", speaker="narrator", voice="marcus", tts_text="Code listing skipped.",
                         char_start=0, char_end=5)]),
        ],
        characters=[Character(id="rabbit", name="The White Rabbit", first_para=0, voice="jake",
                              voice_tags="<|prosody:pitch_high|>", persona="Late.")],
    )


class FakeTranslator:
    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn or (lambda pieces, lang: [f"[{lang}] {x}" for x in pieces])

    async def __call__(self, pieces, lang):
        self.calls.append((list(pieces), lang))
        return self.fn(pieces, lang)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


# ---------- translation cache ----------

def test_paragraph_is_translated_once_and_cached_per_language(data):
    book, fake = _book(), FakeTranslator()
    p = book.paragraphs[0]
    a, cached_a = asyncio.run(translate.paragraph_pieces(book, p, "spa", translator=fake))
    b, cached_b = asyncio.run(translate.paragraph_pieces(book, p, "spa", translator=fake))
    assert a == b == ["[Spanish] Alice was tired.", "[Spanish] “Oh dear!” said the Rabbit."]
    assert (cached_a, cached_b) == (False, True)
    assert len(fake.calls) == 1
    asyncio.run(translate.paragraph_pieces(book, p, "zho", translator=fake))
    assert len(fake.calls) == 2 and fake.calls[1][1] == "Chinese"
    assert translate.cache_path("demo", "spa", 0).exists()
    assert translate.cache_path("demo", "spa", 0) != translate.cache_path("demo", "zho", 0)


def test_concurrent_requests_share_one_translation(data):
    book = _book()
    gate = asyncio.Event()

    class Slow(FakeTranslator):
        async def __call__(self, pieces, lang):
            await gate.wait()
            return await super().__call__(pieces, lang)

    fake = Slow()

    async def run():
        t = [asyncio.create_task(translate.paragraph_pieces(book, book.paragraphs[0], "fra", translator=fake)) for _ in range(3)]
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(*t)

    out = asyncio.run(run())
    assert len(fake.calls) == 1 and out[0][0] == out[2][0]


def test_changed_paragraph_text_is_translated_again(data):
    book, fake = _book(), FakeTranslator()
    asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "spa", translator=fake))
    book.paragraphs[0].tts_chunks[0].char_end = 10   # re-chunked: the pieces changed
    asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "spa", translator=fake))
    assert len(fake.calls) == 2


def test_misaligned_answer_falls_back_to_one_call_per_piece(data):
    book = _book()
    fake = FakeTranslator(lambda pieces, lang: ["merged"] if len(pieces) > 1 else [f"one:{pieces[0]}"])
    out, _ = asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "deu", translator=fake))
    assert out == ["one:Alice was tired.", "one:“Oh dear!” said the Rabbit."]
    assert len(fake.calls) == 3


def test_blank_translation_keeps_the_original_piece(data):
    book = _book()
    fake = FakeTranslator(lambda pieces, lang: ["", "  "] if len(pieces) > 1 else [""])
    out, _ = asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "ita", translator=fake))
    assert out == ["Alice was tired.", "“Oh dear!” said the Rabbit."]


def test_cache_is_per_account_with_per_user(data, monkeypatch):
    monkeypatch.setattr(settings, "per_user", True)
    book = _book()
    alice, bob = users.Identity("a@x.com", users.ns_for("a@x.com")), users.Identity("b@x.com", users.ns_for("b@x.com"))
    fake = FakeTranslator()

    async def as_user(ident):
        token = users._current.set(ident)
        try:
            return await translate.paragraph_pieces(book, book.paragraphs[0], "spa", translator=fake)
        finally:
            users._current.reset(token)

    asyncio.run(as_user(alice))
    asyncio.run(as_user(alice))
    asyncio.run(as_user(bob))
    assert len(fake.calls) == 2   # bob does not read alice's cache
    pa, pb = translate.cache_path("demo", "spa", 0, alice), translate.cache_path("demo", "spa", 0, bob)
    assert pa != pb and pa.exists() and pb.exists()
    assert "users" in pa.parts and users.ns_for("a@x.com") in pa.parts
    assert translate.cache_path("demo", "spa", 0, users.LOCAL) == data / "cache" / "translations" / "demo" / "spa" / "p0.json"


def test_unavailable_without_gemini_or_public_service(data):
    book = _book()
    with pytest.raises(translate.Unavailable):
        asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "spa"))


def test_public_service_translates_on_a_clone(data, monkeypatch):
    from app import public_client
    sent = {}

    async def fake_passage(pieces, language):
        sent["args"] = (pieces, language)
        return [p.upper() for p in pieces]

    monkeypatch.setattr(public_client, "active", lambda: True)
    monkeypatch.setattr(public_client, "translate_passage", fake_passage)
    book = _book()
    out, _ = asyncio.run(translate.paragraph_pieces(book, book.paragraphs[0], "spa"))
    assert out[0] == "ALICE WAS TIRED." and sent["args"][1] == "spa"


def test_gemini_prompt_keeps_names_and_asks_for_the_language():
    s = translate.system_prompt("Japanese")
    assert "Japanese" in s and "names" in s.lower() and "same number" in s.lower()


# ---------- TTS input for a translated piece ----------

def test_lead_tags_keep_voice_emotion_style_and_drop_sfx():
    tags = translate.lead_tags("<|prosody:pitch_high|><|style:whispering|><|emotion:fear|><|sfx:laughter|>Ha! Run.")
    assert tags == "<|prosody:pitch_high|><|style:whispering|><|emotion:fear|>"
    assert translate.lead_tags("Plain text.") == ""
    assert translate.lead_tags("Hello <|sfx:cough|> there") == ""


def test_tts_inputs_carry_the_tags_onto_every_piece():
    long = "Una frase bastante larga para el lector. " * 12
    out = translate.tts_inputs("<|emotion:joy|>Original.", long.strip(), "spa")
    assert len(out) >= 2
    assert all(x.startswith("<|emotion:joy|>") and len(x) - len("<|emotion:joy|>") <= MAX_CHARS for x in out)


def test_split_translation_cjk_punctuation_and_limit():
    zh = "爱丽丝很累。" * 60    # 爱丽丝很累。 x60 = 360 chars, no spaces
    parts = split_translation(zh, "zho")
    assert len(parts) == 2 and all(len(x) <= MAX_CHARS for x in parts)
    assert all(x.endswith("。") for x in parts)          # cut at a sentence end
    assert "".join(parts) == zh
    assert split_translation("Hola. Adiós.", "spa") == ["Hola. Adiós."]
    blob = "あ" * 600                                     # no punctuation at all: hard cut
    assert all(len(x) <= MAX_CHARS for x in split_translation(blob, "jpn"))
    es = ("Esta es una oración mediana. " * 15).strip()
    assert all(len(x) <= MAX_CHARS for x in split_translation(es, "spa"))


# ---------- mp3 joining (a translation longer than one TTS chunk) ----------

def _frame(xing=False):
    hdr = bytes([0xFF, 0xF3, 0x84, 0xC4])        # MPEG2 layer III 64 kbit/s 24 kHz mono: 72*64000/24000 = 192 bytes
    body = bytearray(192 - 4)
    if xing:
        body[9:13] = b"Info"
    return hdr + bytes(body)


def test_mp3_join_drops_info_frames_and_id3():
    a = b"ID3\x03\x00\x00\x00\x00\x00\x02xx" + _frame(True) + _frame() + _frame()
    b = _frame(True) + _frame()
    joined = mp3dur.join([a, b])
    assert joined == _frame() * 3
    assert mp3dur.join([a]) == a


# ---------- routes ----------

@pytest.fixture
def env(data, monkeypatch):
    monkeypatch.setattr(settings, "boson_api_key", "k")
    monkeypatch.setattr(settings, "access_token", "")
    (data / "cache").mkdir()
    d = data / "books" / "demo"
    d.mkdir(parents=True)
    (d / "book.json").write_text(_book().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(main, "tts_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "translate_bucket", TokenBucket(1000, 60))
    fake = FakeTranslator()
    monkeypatch.setattr(translate, "default_translator", lambda: fake)
    bodies = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("timestamps"):
            return httpx.Response(200, json={"audio": "QUFB", "timestamps": None})
        return httpx.Response(200, content=b"AUDIO" + str(len(bodies)).encode(), headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return TestClient(main.app), fake, bodies


def test_translate_route_returns_pieces_per_chunk(env):
    client, fake, _ = env
    r = client.post("/api/translate", json={"book_id": "demo", "para": 0, "language": "spa"})
    assert r.status_code == 200
    out = r.json()
    assert out["chunk_ids"] == ["p0c0", "p0c1"] and out["pieces"][0] == "[Spanish] Alice was tired."
    client.post("/api/translate", json={"book_id": "demo", "para": 0, "language": "spa"})
    assert len(fake.calls) == 1


def test_translate_route_rejects_english_unknown_code_and_listing(env):
    client, _, _ = env
    assert client.post("/api/translate", json={"book_id": "demo", "para": 0, "language": "eng"}).status_code == 422
    assert client.post("/api/translate", json={"book_id": "demo", "para": 0, "language": "es"}).status_code == 422
    assert client.post("/api/translate", json={"book_id": "demo", "para": 1, "language": "spa"}).status_code == 400
    assert client.post("/api/translate", json={"book_id": "demo", "para": 9, "language": "spa"}).status_code == 404


def test_translate_route_503_when_unavailable(env, monkeypatch):
    client, _, _ = env

    def none():
        raise translate.Unavailable("no GCP_PROJECT")

    monkeypatch.setattr(translate, "default_translator", none)
    r = client.post("/api/translate", json={"book_id": "demo", "para": 0, "language": "fra"})
    assert r.status_code == 503


def test_translated_tts_uses_the_same_voice_and_tags_with_language(env):
    client, _, bodies = env
    original = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c1"}).json()
    english = bodies[-1]["input"]
    r = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c1", "language": "spa"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["voice"] == original["voice"] and out["timestamps"] is None and out["text"].startswith("[Spanish]")
    b = bodies[-1]
    assert b["voice"] == original["voice"] and b["language"] == "spa" and b["normalize"] is True and not b.get("timestamps")
    lead = english[:english.index("<|sfx")]          # the same delivery tags as the English chunk (cast applied)
    assert "<|emotion:fear|>" in lead and b["input"] == lead + out["text"]
    assert "sfx" not in b["input"]
    again = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c1", "language": "spa"}).json()
    assert again["cached"] is True and len(bodies) == 2


def test_translated_tts_keeps_the_book_voice_and_pitch_without_a_cast(env, monkeypatch):
    from app import cast
    monkeypatch.setattr(cast, "voice_for", lambda book, speaker: None)
    monkeypatch.setattr(cast, "tags_for", lambda book, speaker: None)
    client, _, bodies = env
    r = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c1", "language": "kor"})
    assert r.json()["voice"] == "jake"
    assert bodies[-1]["input"].startswith("<|prosody:pitch_high|><|emotion:fear|>[Korean]")


def test_translated_tts_voice_override_and_speed(env):
    client, _, bodies = env
    r = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c0", "language": "zho",
                                                 "voice_override": "nora", "speed": "slow"})
    assert r.status_code == 200
    assert bodies[-1]["voice"] == "nora" and bodies[-1]["language"] == "zho"
    assert bodies[-1]["input"].startswith("<|prosody:speed_slow|>")


def test_translated_tts_cached_only_refuses_a_miss(env):
    client, _, bodies = env
    r = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c0", "language": "spa",
                                                 "cached_only": True})
    assert r.status_code == 409 and not bodies


def test_translated_tts_long_piece_is_split_and_joined(env, monkeypatch):
    client, fake, bodies = env
    fake.fn = lambda pieces, lang: [("Una frase bastante larga para el lector. " * 10).strip() for _ in pieces]
    r = client.post("/api/tts/translated", json={"book_id": "demo", "chunk_id": "p0c0", "language": "spa"})
    assert r.status_code == 200
    assert len(bodies) >= 2 and all(len(b["input"]) <= MAX_CHARS for b in bodies)
    path = settings.data_dir / "cache" / "tts" / r.json()["audio_url"].rsplit("/", 1)[1]
    assert path.read_bytes() == b"".join(b"AUDIO" + str(i + 1).encode() for i in range(len(bodies)))
