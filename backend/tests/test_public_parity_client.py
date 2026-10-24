"""Public API parity (clone side): translation for search, kind detection, title/author, face box and equation
readings go through the public service only when public_client.active(), and fall back when it is closed or capped."""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app import face_detect, main, multilingual, public_api, public_client
from app.config import settings
from app.enrich import cast as cast_mod
from app.enrich import describe, detect, equations, spend
from app.enrich.llm import LLM
from app.enrich.minor import MINOR_IDS
from app.ratelimit import TokenBucket

from .test_novel_pipeline import data_dir, make_book_on_disk  # noqa: F401  (fixture)
from .test_public_parity import PNG, ParityFake

FOREIGN = "¿Quién es la reina y por qué está enfadada?"


@pytest.fixture
def clone(data_dir, monkeypatch):  # noqa: F811
    """A clone with only a Boson key and PUBLIC_API_URL; the service is the real public app, in-process."""
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    monkeypatch.setattr(settings, "upload_enrich", True)
    monkeypatch.setattr(public_client, "_probe", None)
    fake = ParityFake()
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": fake}, fallback=False))
    for name in ("small_bucket", "face_bucket", "eq_start_bucket", "eq_bucket"):
        monkeypatch.setattr(public_api, name, TokenBucket(1000, 60))
    monkeypatch.setattr(public_api, "face_calls", public_api.DailyCount(public_api.FACE_CALLS_FILE))
    monkeypatch.setattr(public_api, "answer_budget", public_api.DailyBudget())
    monkeypatch.setattr(public_api, "ip_budget", public_api.IpBudget())
    monkeypatch.setattr(public_api, "_tickets", {})
    monkeypatch.setattr(public_api, "_eq_running", 0)
    monkeypatch.setattr(spend, "_state", None)
    monkeypatch.setattr(multilingual, "_cache", type(multilingual._cache)())
    transport = httpx.ASGITransport(app=public_api.app)
    monkeypatch.setattr(public_client, "_new_client",
                        lambda timeout=None: httpx.AsyncClient(transport=transport, base_url="http://public",
                                                               timeout=timeout))
    assert public_client.active()
    return fake


def serve(monkeypatch, handler):
    """Replace the service by a handler (closed, capped, malformed...)."""
    monkeypatch.setattr(public_client, "_new_client",
                        lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                               base_url="http://public"))


# ---------- novel results: series and minor voices ----------

def test_apply_result_keeps_series_and_minor_stand_ins(data_dir):  # noqa: F811
    book = make_book_on_disk(data_dir)
    target = next(p for p in book.paragraphs if '"' in p.text)
    chars = [{"id": f"c{i}", "name": f"C{i}", "first_para": 0, "voice": "nora", "persona": "P."} for i in range(8)]
    chars += [{"id": "minor_m", "name": "Other speaker (lower voice)", "first_para": 0, "voice": "oliver",
               "voice_tags": "<|prosody:pitch_low|>", "persona": "A minor speaker outside the cast.", "main": False},
              {"id": "minor_f", "name": "x", "first_para": 0, "voice": "evil", "persona": "x", "main": False}]
    quote = target.text[target.text.index('"'):]
    segs = [{"speaker": "narrator", "text": target.text[:target.text.index('"')]}, {"speaker": "minor_m", "text": quote}]
    segs = [s for s in segs if s["text"].strip()]
    result = {"characters": chars, "narrator": {"voice": "nora"}, "segments": {str(target.idx): segs},
              "summaries": [], "series": "The Harry Potter series", "series_index": 3}
    public_client.apply_result(book, result)
    assert book.series == "harry-potter" and book.series_index == 3
    main_ids = [c.id for c in book.characters if c.main is not False]
    minor = {c.id: c for c in book.characters if c.main is False}
    assert len(main_ids) == cast_mod.MAIN_MAX and set(minor) == MINOR_IDS
    assert minor["minor_f"].voice == "chloe" and minor["minor_m"].voice_tags == "<|prosody:pitch_low|>"  # fixed voices
    assert "minor_m" in {s.speaker for s in book.paragraphs[target.idx].segments}
    bad = {"characters": chars[:1], "narrator": {"voice": "nora"}, "segments": {}, "summaries": [],
           "series": "x" * 500, "series_index": 10_000}
    book.series = None
    public_client.apply_result(book, bad)
    assert book.series_index is None and len(book.series) <= 60


# ---------- translate for search (#12) ----------

def test_a_foreign_question_is_searched_in_english_through_the_service(clone):
    assert asyncio.run(multilingual.search_query(FOREIGN)) == "Who is Alice?"
    assert clone.calls == ["Translation"]
    assert asyncio.run(multilingual.search_query(FOREIGN)) == "Who is Alice?" and len(clone.calls) == 1   # cached


def test_an_english_question_never_calls_the_service(clone):
    assert asyncio.run(multilingual.search_query("Who is the queen?")) == "Who is the queen?" and clone.calls == []


@pytest.mark.parametrize("status", [403, 402, 429, 500])
def test_a_closed_or_capped_service_searches_the_original(clone, monkeypatch, status):
    serve(monkeypatch, lambda req: httpx.Response(status, json={"code": "x"}))
    assert asyncio.run(multilingual.search_query(FOREIGN)) == FOREIGN


def test_a_slow_service_never_holds_the_answer(clone, monkeypatch):
    async def slow(text, client=None):
        await asyncio.sleep(30)
    monkeypatch.setattr(public_client, "translate", slow)
    monkeypatch.setattr(multilingual, "TRANSLATE_TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    assert asyncio.run(multilingual.search_query(FOREIGN)) == FOREIGN
    assert time.monotonic() - t0 < 2


def test_a_known_closed_service_is_not_asked(clone, monkeypatch):
    public_client.mark_unavailable()
    serve(monkeypatch, lambda req: pytest.fail("no request while the service is known closed"))
    with pytest.raises(public_client.PublicFailure):
        asyncio.run(public_client.translate(FOREIGN))


def test_without_the_service_translation_is_unavailable(data_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "public_api_url", "")
    with pytest.raises(multilingual.Unavailable):
        asyncio.run(multilingual.translate(FOREIGN))


# ---------- kind detection and title/author ----------

def test_detect_kind_uses_the_service(clone):
    kind, conf, method = asyncio.run(detect.detect_kind("Chapter 1. Definitions and exercises. " * 50))
    assert (kind, method) == ("textbook", "llm") and clone.calls == ["KindGuess"]


def test_detect_kind_falls_back_to_the_heuristic_when_closed(clone, monkeypatch):
    serve(monkeypatch, lambda req: httpx.Response(403))
    assert asyncio.run(detect.detect_kind("Some text. " * 50))[2] == "heuristic"


def test_describe_uses_the_service_for_a_file_without_metadata(clone):
    out = asyncio.run(describe.describe(b"CHAPTER ONE\n\nThe opening lines of an untitled story...", ".txt"))
    assert out == {"title": "Alice's Adventures", "author": "Lewis Carroll", "source": "llm"}


def test_describe_keeps_what_the_file_says_when_closed(clone, monkeypatch):
    serve(monkeypatch, lambda req: httpx.Response(402, json={"code": "daily_cap"}))
    out = asyncio.run(describe.describe(b"Title: Alice\n\nSome text.", ".txt"))
    assert out == {"title": "Alice", "author": "", "source": "metadata"}


# ---------- face box ----------

def test_face_detect_through_the_service(clone):
    assert asyncio.run(face_detect.detect(PNG, "png")) == {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.6}


def test_face_detect_fallback_and_untrusted_boxes(clone, monkeypatch):
    serve(monkeypatch, lambda req: httpx.Response(403))
    assert asyncio.run(face_detect.detect(PNG, "png")) is None
    assert public_client.known_unavailable()                 # closed: skipped for a minute (answers too)
    monkeypatch.setattr(public_client, "_probe", None)
    serve(monkeypatch, lambda req: httpx.Response(200, json={"face": {"x": -1, "y": 0.0, "w": 1.5, "h": 0.5}}))
    assert asyncio.run(face_detect.detect(PNG, "png")) == {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    serve(monkeypatch, lambda req: httpx.Response(200, json={"face": "nonsense"}))
    assert asyncio.run(face_detect.detect(PNG, "png")) is None


def test_face_detect_without_the_service_makes_no_call(data_dir, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "public_api_url", "")
    monkeypatch.setattr(public_client, "detect_face", lambda *a: pytest.fail("not active"))
    assert asyncio.run(face_detect.detect(PNG, "png")) is None


# ---------- equation readings (#41) ----------

def crops(prefix, n, png=PNG):
    return {f"{prefix}{i}": (png, "y = b x") for i in range(1, n + 1)}


def test_equations_through_the_service(clone):
    disp, inl, note = asyncio.run(public_client.transcribe_equations("u-b-00000000", crops("e", 3), crops("m", 2)))
    assert note is None
    assert disp == {f"e{i}": {"latex": f"x_{{e{i}}}", "spoken": f"x e{i}"} for i in range(1, 4)}
    assert set(inl) == {"m1", "m2"}
    assert public_api._tickets == {}                         # every picture was sent: the ticket closed


def test_equations_are_sent_in_small_requests(clone, monkeypatch):
    sizes = []
    real = public_client._new_client

    def counting(timeout=None):
        c = real(timeout)
        orig = c.post

        async def post(url, **kw):
            if "items" in (kw.get("json") or {}):
                sizes.append(len(kw["json"]["items"]))
            return await orig(url, **kw)
        c.post = post
        return c
    monkeypatch.setattr(public_client, "_new_client", counting)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (public_api.EQ_PNG_MAX + 10)
    found = {**crops("e", 30), "e99": (big, "")}            # a picture over the size limit is not sent
    disp, _, note = asyncio.run(public_client.transcribe_equations("u-b-00000000", found, {}))
    assert sizes == [24, 6] and len(disp) == 30 and "e99" not in disp and note is None


@pytest.mark.parametrize("status,code,note", [(403, None, public_client.EQUATIONS_UNAVAILABLE),
                                              (402, "ip_cap", public_client.EQUATIONS_LIMIT),
                                              (500, None, public_client.EQUATIONS_UNAVAILABLE)])
def test_equations_fall_back_to_pictures(clone, monkeypatch, status, code, note):
    serve(monkeypatch, lambda req: httpx.Response(status, json={"code": code}))
    assert asyncio.run(public_client.transcribe_equations("u-b-00000000", crops("e", 2), {})) == ({}, {}, note)


def test_equation_readings_are_not_trusted(clone, monkeypatch):
    def handler(req):
        if req.url.path == "/api/public/equations":
            return httpx.Response(200, json={"ticket": "t", "accepted": 2, "estimate_usd": 0.01, "batch": 24})
        return httpx.Response(200, json={"readings": {"e1": {"latex": "$a+b$", "spoken": "a plus b"},
                                                      "e2": {"latex": "x" * 5000, "spoken": "long"},
                                                      "e7": {"latex": "z", "spoken": "not asked"},
                                                      "m1": "nonsense"}, "stopped": "book_cap"})
    serve(monkeypatch, handler)
    disp, inl, note = asyncio.run(public_client.transcribe_equations("u-b-00000000", crops("e", 2), crops("m", 1)))
    assert disp == {"e1": {"latex": "a+b", "spoken": "a plus b"}} and inl == {}
    assert note == public_client.EQUATIONS_PARTIAL


def test_equations_job_uses_the_service_when_active(clone, data_dir, monkeypatch):  # noqa: F811
    from .test_equations import display, load, pdf, published
    data = pdf(display)
    book_id = published(data)
    seen = []

    async def fake(bid, found, inline, **kw):
        seen.append((list(found), list(inline)))
        return {"e1": {"latex": "y=x", "spoken": "y equals x"}}, {}, None
    monkeypatch.setattr(public_client, "transcribe_equations", fake)
    monkeypatch.setattr(equations, "transcribe", lambda *a, **k: pytest.fail("no local Gemini"))
    asyncio.run(main._equations_job(book_id, data))
    [m] = [p.math for p in load(book_id).paragraphs if p.math]
    assert seen == [(["e1"], [])] and m.latex == "y=x" and m.spoken == "y equals x"


def test_equations_job_without_any_llm_keeps_pictures(data_dir, monkeypatch):  # noqa: F811
    from .test_equations import display, load, pdf, published
    monkeypatch.setattr(settings, "gcp_project", "")
    monkeypatch.setattr(settings, "public_api_url", "")
    data = pdf(display)
    book_id = published(data)
    monkeypatch.setattr(public_client, "transcribe_equations", lambda *a, **k: pytest.fail("not active"))
    asyncio.run(main._equations_job(book_id, data))
    [m] = [p.math for p in load(book_id).paragraphs if p.math]
    assert m.image and not m.latex


def test_payload_is_base64_png(clone, monkeypatch):
    bodies = []

    def handler(req):
        body = json.loads(req.content or b"{}")
        bodies.append(body)
        if req.url.path == "/api/public/equations":
            return httpx.Response(200, json={"ticket": "t", "accepted": 1, "estimate_usd": 0.01, "batch": 24})
        return httpx.Response(200, json={"readings": {}})
    serve(monkeypatch, handler)
    asyncio.run(public_client.transcribe_equations("u-b-00000000", crops("e", 1), {}))
    assert bodies[0] == {"count": 1}
    assert base64.b64decode(bodies[1]["items"][0]["png"]) == PNG and "u-b-00000000" not in json.dumps(bodies)
