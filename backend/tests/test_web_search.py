"""Web search (Grounding with Google Search) on Talk answers: only for textbooks and for novels with spoilers on.

A novel with spoilers off must never get the search tool: the web would leak what happens later."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main, public_api, public_client, websearch
from app.config import settings
from app.personas import ANSWER_EMOTION_RULE, WEB_SEARCH_RULE, build_answer_prompt
from app.ratelimit import TokenBucket
from app.schema import Book, Paragraph, Section

from .test_api import book_with_chunk, env  # noqa: F401  (env fixture)
from .test_answer import LeakyIndex, lines

SOURCES = [{"uri": "https://example.org/a", "title": "example.org"},
           {"uri": "javascript:alert(1)", "title": "bad"},
           {"uri": "https://example.org/a", "title": "dup"},
           {"uri": "http://other.example/b", "title": "<b>other</b>"}]


# shaped like Google's search_entry_point.rendered_content (a style block and a chip carousel; no script)
SUGGESTIONS = ('<style>.container { padding: 0 12px; } @media (prefers-color-scheme: dark) { .chip { color: #fff; } }'
               '</style><div class="container"><div class="carousel"><a class="chip" '
               'href="https://www.google.com/search?q=nitrogen&amp;client=app-vertex-grounding&amp;safesearch=active">'
               'nitrogen</a></div></div>')


class GroundedFake:
    """Records whether the search tool was asked for; reports sources like the real answerer."""

    def __init__(self, sentences=("It is a gas.", "Outside the book, it was named in 1772."), sources=SOURCES,
                 suggestions=SUGGESTIONS):
        self.sentences, self.sources, self.suggestions, self.calls = sentences, sources, suggestions, []

    async def stream_sentences(self, system, user, on_usage=None, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        for s in self.sentences:
            yield s
        if kw.get("web_search") and kw.get("on_sources"):
            kw["on_sources"](self.sources)
        if kw.get("web_search") and kw.get("on_suggestions") and self.suggestions is not None:
            kw["on_suggestions"](self.suggestions)
        if on_usage:
            on_usage(1000, 100)


def textbook():
    return Book(id="tb", title="Chemistry", author="A", kind="textbook", license="PD",
                sections=[Section(id="s1", title="Gases", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text="Nitrogen is a gas."),
                            Paragraph(idx=1, section_id="s1", text="It is inert.")])


@pytest.fixture
def app_env(env, monkeypatch):  # noqa: F811
    client, _ = env
    d = settings.data_dir / "books" / "tb"
    d.mkdir(parents=True)
    (d / "book.json").write_text(textbook().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(main, "answer_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "_index_for", lambda book_id: LeakyIndex())
    monkeypatch.setattr(settings, "web_search", True)
    monkeypatch.setattr(settings, "grounded_daily_cap", 100)
    monkeypatch.setattr(main, "_public_answers", lambda: False)
    fake = GroundedFake()
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    return client, fake


def ask(client, **kw):
    body = {"book_id": "demo", "query": "What happened?", "reader_para": 1, "speaker_id": "harry"}
    body.update(kw)
    return lines(client.post("/api/answer", json=body))


# ---------- the rule ----------

def test_allowed_only_for_textbooks_and_spoilers_on(monkeypatch):
    monkeypatch.setattr(settings, "web_search", True)
    assert websearch.allowed("textbook", False) and websearch.allowed("textbook", True)
    assert websearch.allowed("novel", True)
    assert not websearch.allowed("novel", False)
    monkeypatch.setattr(settings, "web_search", False)
    assert not websearch.allowed("textbook", False) and not websearch.allowed("novel", True)


def test_clean_sources_keeps_only_http_links_deduped_and_capped():
    out = websearch.clean_sources(SOURCES + [{"uri": f"https://x{i}.example/", "title": "t"} for i in range(20)])
    assert out[0] == {"uri": "https://example.org/a", "title": "example.org"}
    assert all(s["uri"].startswith(("https://", "http://")) for s in out)
    assert len({s["uri"] for s in out}) == len(out) and len(out) <= websearch.MAX_SOURCES
    assert websearch.clean_sources(None) == [] and websearch.clean_sources([{"uri": 5}, "x", {}]) == []
    long = websearch.clean_sources([{"uri": "https://a.example/" + "x" * 5000, "title": "y" * 500}])
    assert long == [] or (len(long[0]["uri"]) <= websearch.MAX_URI and len(long[0]["title"]) <= websearch.MAX_TITLE)


# ---------- the local /api/answer ----------

def test_novel_with_spoilers_off_never_gets_web_search(app_env):
    client, fake = app_env
    out = ask(client)
    assert not fake.calls[0].get("web_search")
    assert WEB_SEARCH_RULE not in fake.calls[0]["system"]
    assert "sources" not in [o["type"] for o in out]


def test_textbook_gets_web_search_and_sources_event(app_env):
    client, fake = app_env
    out = ask(client, book_id="tb", speaker_id="narrator", query="What is nitrogen?")
    assert fake.calls[0]["web_search"] is True and WEB_SEARCH_RULE in fake.calls[0]["system"]
    types = [o["type"] for o in out]
    assert types == ["sentence", "sentence", "sources", "done"]
    src = out[2]["sources"]
    assert src == [{"uri": "https://example.org/a", "title": "example.org"},
                   {"uri": "http://other.example/b", "title": "<b>other</b>"}]   # text only: the browser never renders it as HTML


def test_novel_with_spoilers_on_gets_web_search(app_env):
    client, fake = app_env
    out = ask(client, spoilers=True)
    assert fake.calls[0]["web_search"] is True
    assert "sources" in [o["type"] for o in out]


def test_no_sources_event_when_nothing_came_back(app_env, monkeypatch):
    client, _ = app_env
    fake = GroundedFake(sources=[], suggestions=None)
    monkeypatch.setattr(main, "get_answerer", lambda: fake)
    out = ask(client, book_id="tb", speaker_id="narrator")
    assert fake.calls[0]["web_search"] is True and [o["type"] for o in out] == ["sentence", "sentence", "done"]


def test_web_search_false_turns_it_off(app_env, monkeypatch):
    client, fake = app_env
    monkeypatch.setattr(settings, "web_search", False)
    ask(client, book_id="tb", speaker_id="narrator")
    ask(client, spoilers=True)
    assert not any(c.get("web_search") for c in fake.calls)
    assert all(WEB_SEARCH_RULE not in c["system"] for c in fake.calls)


def test_daily_cap_falls_back_to_an_ungrounded_answer_and_is_persisted(app_env, monkeypatch):
    client, fake = app_env
    monkeypatch.setattr(settings, "grounded_daily_cap", 2)
    for _ in range(3):
        out = ask(client, book_id="tb", speaker_id="narrator")
        assert out[-1]["type"] == "done"
    assert [bool(c.get("web_search")) for c in fake.calls] == [True, True, False]
    assert WEB_SEARCH_RULE not in fake.calls[2]["system"]
    saved = json.loads((settings.data_dir / "usage" / websearch.LOCAL_FILE).read_text(encoding="utf-8"))
    assert saved["n"] == 2
    monkeypatch.setattr(websearch, "local_count", websearch.DailyCount(websearch.LOCAL_FILE))  # a restart reads the file
    ask(client, book_id="tb", speaker_id="narrator")
    assert not fake.calls[3].get("web_search")


def test_spoilers_off_novel_does_not_spend_the_grounded_cap(app_env, monkeypatch):
    client, _ = app_env
    monkeypatch.setattr(settings, "grounded_daily_cap", 1)
    ask(client)
    assert not (settings.data_dir / "usage" / websearch.LOCAL_FILE).exists()


# ---------- prompt rules ----------

def test_prompt_rule_keeps_book_primary_voice_short_and_marks_outside_facts():
    book = book_with_chunk()
    system, _ = build_answer_prompt(book, "harry", "", [], "Who wrote this?", spoilers=True, web_search=True)
    low = WEB_SEARCH_RULE.lower()
    assert "main source" in low and "outside the book" in low and "character" in low and "short" in low
    assert system.index(WEB_SEARCH_RULE) < system.index(ANSWER_EMOTION_RULE)   # the emotion rule stays last
    assert system.rstrip().endswith(ANSWER_EMOTION_RULE)
    plain, _ = build_answer_prompt(book, "harry", "", [], "Who wrote this?", spoilers=True)
    assert WEB_SEARCH_RULE not in plain


def test_prompt_rule_keeps_the_language_rule():
    book = book_with_chunk()
    system, _ = build_answer_prompt(book, "harry", "", [], "¿Quién escribió este libro y cuándo se publicó?",
                                    spoilers=True, web_search=True)
    assert system.startswith("The reader wrote in Spanish") and WEB_SEARCH_RULE in system


def test_prompt_rule_survives_a_long_prompt():
    book = textbook()
    passages = [{"para_start": i, "para_end": i, "text": "x" * 2000} for i in range(12)]
    system, _ = build_answer_prompt(book, "narrator", "", passages, "What is it?", web_search=True)
    assert WEB_SEARCH_RULE in system


# ---------- the Gemini answerer ----------

class FakeModels:
    def __init__(self, chunks, fail_grounded=False):
        self.chunks, self.fail_grounded, self.configs = chunks, fail_grounded, []

    async def generate_content_stream(self, model, contents, config):
        self.configs.append(config)
        if self.fail_grounded and config.tools:
            raise RuntimeError("grounding is not available")

        async def gen():
            for c in self.chunks:
                yield c
        return gen()


def chunk(text, uris=(), usage=None, entry=None):
    gm = SimpleNamespace(grounding_chunks=[SimpleNamespace(web=SimpleNamespace(uri=u, title=f"t{i}", domain=None))
                                           for i, u in enumerate(uris)],
                         search_entry_point=SimpleNamespace(rendered_content=entry) if entry else None) if uris or entry else None
    return SimpleNamespace(text=text, usage_metadata=usage, candidates=[SimpleNamespace(grounding_metadata=gm)])


def run_answerer(models, **kw):
    from app.gcp import llm
    a = llm.GeminiAnswerer(client=SimpleNamespace(aio=SimpleNamespace(models=models)), model="gemini-2.5-flash")

    async def go():
        return [s async for s in a.stream_sentences("sys", "q", **kw)]
    return asyncio.run(go())


def test_answerer_adds_the_search_tool_only_when_asked(monkeypatch):
    from app.gcp import usage
    recs = []
    monkeypatch.setattr(usage, "record", lambda kind, model, **c: recs.append(c))
    u = SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
    models = FakeModels([chunk("A gas. "), chunk("Named in 1772.", uris=["https://e.org/1", "https://e.org/2"], usage=u,
                                                 entry=SUGGESTIONS)])
    got, sugg = [], []
    out = run_answerer(models, web_search=True, on_sources=got.append, on_suggestions=sugg.append)
    assert sugg == [SUGGESTIONS]     # passed on unmodified
    assert out == ["A gas.", "Named in 1772."]
    tools = models.configs[0].tools
    assert tools and tools[0].google_search is not None
    assert [s["uri"] for s in got[0]] == ["https://e.org/1", "https://e.org/2"]
    assert recs[-1]["grounded"] is True and recs[-1]["sources"] == 2

    models = FakeModels([chunk("Plain.", usage=u)])
    run_answerer(models)
    assert not models.configs[0].tools
    assert "grounded" not in recs[-1]


def test_answerer_falls_back_to_ungrounded_when_the_tool_fails(monkeypatch):
    from app.gcp import usage
    recs = []
    monkeypatch.setattr(usage, "record", lambda kind, model, **c: recs.append(c))
    u = SimpleNamespace(prompt_token_count=10, candidates_token_count=5)
    models = FakeModels([chunk("Plain.", usage=u)], fail_grounded=True)
    got = []
    assert run_answerer(models, web_search=True, on_sources=got.append) == ["Plain."]
    assert models.configs[0].tools and not models.configs[-1].tools
    assert got == [] and "grounded" not in recs[-1]


# ---------- the public service ----------

@pytest.fixture
def pub(tmp_path, monkeypatch):
    from app.enrich import spend
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "trust_proxy", False)
    monkeypatch.setattr(settings, "web_search", True)
    monkeypatch.setattr(settings, "public_grounded_daily_cap", 50)
    monkeypatch.setattr(public_api, "answer_bucket", TokenBucket(100, 60))
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())
    monkeypatch.setattr(public_api, "grounded_calls", websearch.DailyCount(websearch.PUBLIC_FILE))
    monkeypatch.setattr(public_api, "_streams", {})
    monkeypatch.setattr(public_api, "_started_slots", {})
    monkeypatch.setattr(spend, "_state", None)
    fake = GroundedFake()
    monkeypatch.setattr(public_api, "get_public_answerer", lambda: fake)
    return TestClient(public_api.app), fake


def pub_ask(client, **kw):
    body = {"title": "Demo", "kind": "novel", "query": "Who wrote this?", "spoilers": False}
    body.update(kw)
    r = client.post("/api/public/answer", json=body)
    return [json.loads(x) for x in r.text.splitlines()]


def test_public_novel_with_spoilers_off_never_gets_web_search(pub):
    client, fake = pub
    out = pub_ask(client)
    assert not fake.calls[0].get("web_search") and WEB_SEARCH_RULE not in fake.calls[0]["system"]
    assert [o["type"] for o in out] == ["sentence", "sentence", "done"]


def test_public_textbook_and_spoilers_on_get_web_search_and_sources(pub):
    client, fake = pub
    out = pub_ask(client, kind="textbook")
    out2 = pub_ask(client, spoilers=True)
    assert fake.calls[0]["web_search"] is True and fake.calls[1]["web_search"] is True
    for o in (out, out2):
        assert [x["type"] for x in o] == ["sentence", "sentence", "sources", "done"]
        assert all(s["uri"].startswith("http") for s in o[2]["sources"])


def test_public_client_can_opt_out_and_server_setting_wins(pub, monkeypatch):
    client, fake = pub
    pub_ask(client, kind="textbook", web_search=False)
    monkeypatch.setattr(settings, "web_search", False)
    pub_ask(client, kind="textbook")
    assert not any(c.get("web_search") for c in fake.calls)


def test_public_grounded_sub_cap_and_answer_budget(pub, monkeypatch):
    client, fake = pub
    monkeypatch.setattr(settings, "public_grounded_daily_cap", 1)
    pub_ask(client, kind="textbook")
    spent_one = public_api.answer_budget.spent()
    pub_ask(client, kind="textbook")
    assert [bool(c.get("web_search")) for c in fake.calls] == [True, False]
    spent_two = public_api.answer_budget.spent() - spent_one
    # a grounded answer that returned sources is charged its search fee on the daily answer budget
    assert spent_one >= spent_two + public_api.GROUNDED_USD - 1e-9


def test_public_grounded_reservation_needs_room_for_the_search_fee(pub, monkeypatch):
    client, fake = pub
    worst = public_api.price_usd("gemini", "fast", 10_000, public_api.MAX_OUTPUT_TOKENS)
    monkeypatch.setattr(settings, "public_answer_daily_cap_usd", worst)   # room for an answer, not for its search fee
    out = pub_ask(client, kind="textbook")
    assert out[-1]["type"] == "done" and not fake.calls[0].get("web_search")   # fell back to a plain answer


# ---------- the clone, answering through the public service ----------

def test_public_client_passes_sources_on_and_the_clone_cleans_them(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    monkeypatch.setattr(main, "answer_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "_public_answers", lambda: True)
    monkeypatch.setattr(main, "get_answerer", lambda: pytest.fail("the local Gemini must not be used"))
    seen = []

    async def fake_stream(payload, client=None):
        seen.append(payload)
        yield {"type": "sentence", "text": "He smiled."}
        yield {"type": "sources", "sources": SOURCES}

    monkeypatch.setattr(public_client, "stream_answer", fake_stream)
    out = ask(client, spoilers=True)
    assert [o["type"] for o in out] == ["sentence", "sources", "done"]
    assert [s["uri"] for s in out[1]["sources"]] == ["https://example.org/a", "http://other.example/b"]
    assert "web_search" not in seen[0]       # the service decides; the clone only opts out
    monkeypatch.setattr(settings, "web_search", False)
    ask(client, spoilers=True)
    assert seen[1]["web_search"] is False


def test_stream_answer_yields_sources_lines():
    import httpx

    def handler(req):
        body = "\n".join(json.dumps(x) for x in [{"type": "sentence", "text": "Hi."},
                                                  {"type": "sources", "sources": SOURCES[:1]}, {"type": "done"}])
        return httpx.Response(200, text=body + "\n")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pub.example") as c:
            return [m async for m in public_client.stream_answer({"query": "q"}, client=c)]
    msgs = asyncio.run(go())
    assert [m["type"] for m in msgs] == ["sentence", "sources"]


# ---------- Google Search Suggestions (search_entry_point.rendered_content) ----------

def test_search_suggestions_ride_on_the_sources_event_unmodified(app_env):
    client, _ = app_env
    out = ask(client, book_id="tb", speaker_id="narrator")
    ev = next(o for o in out if o["type"] == "sources")
    assert ev["search_suggestions"] == SUGGESTIONS


def test_search_suggestions_alone_still_send_the_event(app_env, monkeypatch):
    client, _ = app_env
    monkeypatch.setattr(main, "get_answerer", lambda: GroundedFake(sources=[]))
    out = ask(client, book_id="tb", speaker_id="narrator")
    ev = next(o for o in out if o["type"] == "sources")
    assert ev["sources"] == [] and ev["search_suggestions"] == SUGGESTIONS


def test_odd_or_oversized_suggestions_are_dropped():
    assert websearch.clean_suggestions(SUGGESTIONS) == SUGGESTIONS
    assert websearch.clean_suggestions(None) is None and websearch.clean_suggestions(5) is None
    assert websearch.clean_suggestions("   ") is None
    assert websearch.clean_suggestions("x" * (websearch.MAX_SUGGESTIONS + 1)) is None


def test_no_suggestions_for_a_spoiler_safe_novel(app_env):
    client, _ = app_env
    out = ask(client)
    assert not any("search_suggestions" in o for o in out)


def test_public_route_sends_suggestions(pub):
    client, _ = pub
    out = pub_ask(client, kind="textbook")
    ev = next(o for o in out if o["type"] == "sources")
    assert ev["search_suggestions"] == SUGGESTIONS


def test_clone_passes_the_services_suggestions_on(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    monkeypatch.setattr(main, "answer_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(main, "_public_answers", lambda: True)

    async def fake_stream(payload, client=None):
        yield {"type": "sentence", "text": "He smiled."}
        yield {"type": "sources", "sources": [], "search_suggestions": SUGGESTIONS}

    monkeypatch.setattr(public_client, "stream_answer", fake_stream)
    out = ask(client, spoilers=True)
    assert out[1] == {"type": "sources", "sources": [], "search_suggestions": SUGGESTIONS}
