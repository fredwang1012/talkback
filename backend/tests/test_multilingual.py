"""Questions in other languages (#12): answered in the reader's language, the English book searched in English."""
import asyncio

import pytest

from app import main, multilingual, retrieval
from app.config import settings
from app.personas import LANGUAGE_RULE, build_answer_prompt, build_instructions

from .test_answer import FakeAnswerer, lines
from .test_api import book_with_chunk, env  # noqa: F401  (env fixture)


@pytest.mark.parametrize("text, code", [
    ("Who is Harry and why did he leave?", "en"),
    ("What does the Queen want?", "en"),
    ("Who is Madame Defarge?", "en"),                       # a French name in an English question
    ("¿Quién es el hombre que llegó con la reina?", "es"),
    ("Pourquoi est-ce qu'elle est partie dans la forêt ?", "fr"),
    ("Warum ist der König so wütend und was will er?", "de"),
    ("Por que ele não voltou para a casa?", "pt"),
    ("Perché il re non è contento della regina?", "it"),
    ("哈利为什么离开了？", "zh"),
    ("ハリーはなぜ去ったのですか", "ja"),
    ("Почему Гарри ушёл?", "ru"),
])
def test_detect_language(text, code):
    assert multilingual.detect_language(text)[0] == code


def test_too_little_to_tell_is_not_foreign():
    assert multilingual.detect_language("Hagrid?") is None
    assert multilingual.foreign_language("Hagrid?") is None
    assert multilingual.foreign_language("la reine") is None    # one common word is not enough
    assert multilingual.foreign_language("¿Quién es la reina?") == "Spanish"


@pytest.fixture
def translations(monkeypatch):
    multilingual._cache.clear()
    calls: list[str] = []

    async def fake(text):
        calls.append(text)
        return {"¿Quién es el hombre que llegó con la reina?": "Who is the man who came with the queen?",
                "¿Por qué sonrió el hombre?": '"Why did the man smile?"'}.get(text, "Why did he leave?")
    monkeypatch.setattr(multilingual, "translate", fake)
    return calls


def test_an_english_question_is_searched_as_is(translations):
    assert asyncio.run(multilingual.search_query("Who is the man?")) == "Who is the man?"
    assert translations == []


def test_a_foreign_question_is_searched_in_english_once(translations):
    q = "¿Quién es el hombre que llegó con la reina?"
    assert asyncio.run(multilingual.search_query(q)) == "Who is the man who came with the queen?"
    assert asyncio.run(multilingual.search_query(q)) == "Who is the man who came with the queen?"
    assert translations == [q]  # cached
    assert asyncio.run(multilingual.search_query("¿Por qué sonrió el hombre?")) == "Why did the man smile?"  # quotes trimmed


def test_a_failed_slow_or_unconfigured_translation_searches_the_original(monkeypatch):
    multilingual._cache.clear()
    q = "Pourquoi est-ce qu'elle est partie dans la forêt ?"
    monkeypatch.setattr(settings, "gcp_project", "")
    assert asyncio.run(multilingual.search_query(q)) == q   # no GCP: no translation, no error

    async def boom(text):
        raise RuntimeError("quota")
    monkeypatch.setattr(multilingual, "translate", boom)
    assert asyncio.run(multilingual.search_query(q)) == q

    async def slow(text):
        await asyncio.sleep(1)
        return "Why did she leave?"
    monkeypatch.setattr(multilingual, "translate", slow)
    monkeypatch.setattr(multilingual, "TRANSLATE_TIMEOUT_S", 0.01)
    assert asyncio.run(multilingual.search_query(q)) == q
    assert multilingual._cache == {}  # a failure is not remembered


def test_the_language_rule_leads_the_prompt_and_survives_the_cap():
    book = book_with_chunk()
    passages = [{"para_start": 0, "para_end": 0, "text": "word " * 4000}]
    system, user = build_answer_prompt(book, "harry", "", passages, "¿Quién es el hombre que llegó con la reina?")
    assert system.startswith(LANGUAGE_RULE.format(lang="Spanish"))
    assert user == "¿Quién es el hombre que llegó con la reina?"   # the model sees the reader's own words
    english, _ = build_answer_prompt(book, "harry", "", passages, "Who is the man?")
    assert "The reader wrote in" not in english
    short = build_instructions(book, "harry", "", passages, 300, reply_language="French")
    assert short.startswith("The reader wrote in French.") and len(short) == 300


def test_retrieve_searches_in_english_and_tells_realtime_the_language(env, translations, monkeypatch):
    client, _ = env
    seen = []
    real = retrieval.aretrieve

    async def spy(book, index, query, *a, **kw):
        seen.append(query)
        return await real(book, index, query, *a, **kw)
    monkeypatch.setattr(main.retrieval, "aretrieve", spy)
    r = client.post("/api/retrieve", json={"book_id": "demo", "query": "¿Por qué sonrió el hombre?", "reader_para": 1,
                                        "speaker_id": "harry"})
    assert r.status_code == 200
    body = r.json()
    assert seen == ["Why did the man smile?"] and body["search_query"] == "Why did the man smile?"
    assert body["instructions"].startswith("The reader wrote in Spanish.")
    english = client.post("/api/retrieve", json={"book_id": "demo", "query": "Why did he smile?", "reader_para": 1,
                                              "speaker_id": "harry"}).json()
    assert "search_query" not in english and not english["instructions"].startswith("The reader wrote in")


def test_answer_searches_in_english_and_answers_in_the_readers_language(env, translations, monkeypatch):
    client, _ = env
    fake = FakeAnswerer(["Sonrió porque estaba contento."])
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    seen = []
    real = retrieval.aretrieve

    async def spy(book, index, query, *a, **kw):
        seen.append(query)
        return await real(book, index, query, *a, **kw)
    monkeypatch.setattr(main.retrieval, "aretrieve", spy)
    r = client.post("/api/answer", json={"book_id": "demo", "query": "¿Por qué sonrió el hombre?", "reader_para": 1,
                                      "speaker_id": "harry"})
    assert [m["type"] for m in lines(r)] == ["sentence", "done"]
    system, user = fake.prompts[0]
    assert seen == ["Why did the man smile?"]
    assert system.startswith("The reader wrote in Spanish.") and user == "¿Por qué sonrió el hombre?"
