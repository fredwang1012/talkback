"""Two-voice reading (issues #85, #86): every speaker of a novel reads in the male or the female voice by gender;
the narrator uses the one the user picks; textbooks keep their single narrator voice. Resolved at TTS time."""
import base64
import json

import httpx

from app import cast, genders, main
from app.boson import BosonClient
from app.config import settings
from app.schema import Book, Character, Narrator, Paragraph, Section, TtsChunk
from tests.test_api import env  # noqa: F401  (fixture)

TAG = "<|prosody:pitch_low|>"


def _chunk(i: int, speaker: str, voice: str, text: str, tags: str = "") -> Paragraph:
    return Paragraph(idx=i, section_id="s1", text=text, tts_chunks=[
        TtsChunk(id=f"p{i}c0", speaker=speaker, voice=voice, tts_text=tags + text, char_start=0, char_end=len(text))])


def novel(kind: str = "novel") -> Book:
    return Book(
        id="demo", title="Demo", author="A", kind=kind, license="PD",
        sections=[Section(id="s1", title="I.", start_para=0)],
        paragraphs=[_chunk(0, "narrator", "nora", "It was a dark night."),
                    _chunk(1, "harry", "oliver", "Hello there.", TAG),
                    _chunk(2, "figg", "jake", "Good evening."),
                    _chunk(3, "zed", "voice_custom", "Who goes there?"),
                    _chunk(4, "minor_f", "chloe", "Over here!", "<|prosody:pitch_high|>")],
        characters=[Character(id="harry", name="Harry", first_para=1, voice="oliver", voice_tags=TAG, persona="."),
                    Character(id="figg", name="Mrs. Figg", first_para=2, voice="jake", persona="."),  # name beats voice
                    Character(id="zed", name="Zed", first_para=3, voice="voice_custom", persona="."),  # unknown
                    Character(id="minor_f", name="Other speaker (higher voice)", first_para=4, voice="chloe",
                              voice_tags="<|prosody:pitch_high|>", persona=".", main=False)],
        narrator=Narrator(voice="nora"),
    )


def _install(book: Book, monkeypatch):
    (settings.data_dir / "books" / "demo" / "book.json").write_text(book.model_dump_json(), encoding="utf-8")
    sent: list[tuple[str, str]] = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        sent.append((body["voice"], body["input"]))
        return httpx.Response(200, json={"audio": base64.b64encode(b"x").decode(), "response_format": "mp3", "input": "x"})

    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    return sent


def _voice(client, chunk: str) -> str:
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": chunk})
    assert r.status_code == 200, r.text
    return r.json()["voice"]


def test_inferred_genders_use_names_pronouns_then_the_cast_voice():
    b = novel()
    assert genders.character_genders(b) == {"harry": "male", "figg": "female", "zed": None, "minor_f": "female"}
    assert genders.from_name("The Queen of Hearts") == "female" and genders.from_name("Uncle Vernon") == "male"
    assert genders.from_name("Gaoler's daughter") == "female" and genders.from_name("Toad") is None


def test_novel_reads_in_two_voices_by_default(env, monkeypatch):  # noqa: F811
    client, _ = env
    sent = _install(novel(), monkeypatch)
    got = client.get("/api/books/demo/cast").json()
    assert got["mode"] == "two"
    assert got["defaults"] == {"male": "jake", "female": "nora", "narrator_gender": "female"}  # narrator keeps nora
    assert got["auto_genders"]["figg"] == "female" and got["auto_genders"]["zed"] is None
    assert [_voice(client, f"p{i}c0") for i in range(5)] == ["nora", "jake", "nora", "nora", "nora"]
    # the characters' own pitch tags are dropped: exactly two voices
    assert sent[1] == ("jake", "Hello there.") and sent[4] == ("nora", "Over here!")


def test_user_picks_the_two_voices_the_narrator_and_a_gender(env, monkeypatch):  # noqa: F811
    client, _ = env
    _install(novel(), monkeypatch)
    body = {"male": {"voice": "marcus"}, "female": {"voice": "eleanor"}, "narrator_gender": "male",
            "genders": {"harry": "female"}}
    assert client.put("/api/books/demo/cast", json=body).status_code == 200
    # narrator and the unknown-gender speaker follow the narrator's pick; harry is overridden to the female voice
    assert [_voice(client, f"p{i}c0") for i in range(5)] == ["marcus", "eleanor", "eleanor", "marcus", "eleanor"]
    got = client.get("/api/books/demo/cast").json()
    assert got["genders"] == {"harry": "female"} and got["narrator_gender"] == "male"
    assert got["male"] == {"voice": "marcus", "tags": None, "face": None}


def test_full_cast_mode_keeps_the_per_character_voices(env, monkeypatch):  # noqa: F811
    client, _ = env
    sent = _install(novel(), monkeypatch)
    (settings.data_dir / "voices.json").write_text(json.dumps({"c": "voice_custom"}), encoding="utf-8")
    assert client.put("/api/books/demo/cast", json={"mode": "cast", "male": {"voice": "marcus"}}).status_code == 200
    assert [_voice(client, f"p{i}c0") for i in range(4)] == ["nora", "oliver", "jake", "voice_custom"]
    assert sent[1] == ("oliver", TAG + "Hello there.")


def test_textbook_has_one_voice(env, monkeypatch):  # noqa: F811
    client, _ = env
    book = novel("textbook")
    book.characters = []
    for p in book.paragraphs:
        p.tts_chunks[0].speaker, p.tts_chunks[0].voice = "narrator", "nora"
    _install(book, monkeypatch)
    assert {_voice(client, f"p{i}c0") for i in range(5)} == {"nora"}
    client.put("/api/books/demo/cast", json={"narrator": {"voice": "marcus"}, "male": {"voice": "jake"}})
    assert {_voice(client, f"p{i}c0") for i in range(5)} == {"marcus"}


def test_gender_of_resolution_order():
    b = novel()
    s = cast.CastSettings()
    assert cast.gender_of(b, s, "narrator") == "female"
    assert cast.gender_of(b, s, "zed") == "female"  # unknown: the narrator's voice
    s = cast.CastSettings(narrator_gender="male", genders={"figg": "male"})
    assert cast.gender_of(b, s, "zed") == "male" and cast.gender_of(b, s, "figg") == "male"
    assert cast.slot_voice(b, s, "male") == "jake" and cast.slot_voice(b, s, "female") == "nora"
