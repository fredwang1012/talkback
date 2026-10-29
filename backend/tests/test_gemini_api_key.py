"""Gemini through a Google AI Studio API key (#137): which endpoint is chosen, that every Gemini use goes through the
chosen client, and that the key never shows up in health, logs or errors. The SDK client is always a fake."""
import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import google.genai as genai
import httpx
import pytest
from google.genai import errors as genai_errors

from app import deps, face_detect, gemini, indexes, multilingual, pacer, public_client, retrying, translate
from app.config import settings
from app.enrich import equations
from app.enrich.llm import GeminiProvider, make_llm
from app.ratelimit import TokenBucket
from app.routes import talk as talk_routes

from .test_answer import LeakyIndex, lines
from .test_api import env  # noqa: F401  (env fixture)

KEY = "AIzaSy-test-key-never-shown-0123456789"
REAL_ADC_CHECK = gemini.gcp_credentials_usable


class FakeModels:
    def __init__(self, text='{"items": []}', fail=None):
        self.text, self.fail, self.configs = text, fail, []

    async def generate_content(self, model, contents, config):
        self.configs.append((model, config))
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(text=self.text, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5,
                                                              thoughts_token_count=0))

    async def generate_content_stream(self, model, contents, config):
        self.configs.append((model, config))
        if self.fail is not None:
            raise self.fail

        async def gen():
            yield SimpleNamespace(text="It is a gas.", usage_metadata=None, candidates=[])
        return gen()


@pytest.fixture
def made(monkeypatch):
    """genai.Client replaced by a recorder: `made` lists the kwargs of every client built; each serves `made.models`."""
    calls: list[dict] = []
    models = FakeModels()

    class FakeClient:
        def __init__(self, **kw):
            calls.append(kw)
            self.aio = SimpleNamespace(models=models)

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(multilingual, "_client", None)
    monkeypatch.setattr(translate._InteractiveGemini, "_llm", None)
    REAL_ADC_CHECK.cache_clear()
    yield SimpleNamespace(calls=calls, models=models)
    REAL_ADC_CHECK.cache_clear()


def use(monkeypatch, project="", key="", adc=False):
    monkeypatch.setattr(settings, "gcp_project", project)
    monkeypatch.setattr(settings, "gemini_api_key", key)
    probes = []
    monkeypatch.setattr(gemini, "gcp_credentials_usable", lambda: probes.append(1) or adc)
    return probes


AI_STUDIO = {"api_key": KEY, "vertexai": False}


# ---------- which endpoint ----------

@pytest.mark.parametrize("project,key,adc,expected", [
    ("", "", False, None),
    ("p", "", False, "vertex"),       # today's behaviour: GCP_PROJECT alone is Vertex, ADC not even probed
    ("p", "", True, "vertex"),
    ("p", KEY, True, "vertex"),       # both: the current deploy keeps Vertex
    ("p", KEY, False, "ai_studio"),   # a project whose credentials do not load: the key
    ("", KEY, False, "ai_studio"),
])
def test_mode_for_each_setting(monkeypatch, project, key, adc, expected):
    probes = use(monkeypatch, project, key, adc)
    assert gemini.mode() == expected and gemini.available() == (expected is not None)
    if not key:
        assert probes == []


def test_the_client_matches_the_mode(monkeypatch, made):
    use(monkeypatch, "", KEY)
    gemini.client()
    use(monkeypatch, "proj", "", True)
    monkeypatch.setattr(settings, "gemini_location", "global")
    gemini.client()
    assert made.calls == [AI_STUDIO, {"vertexai": True, "project": "proj", "location": "global"}]


def test_health_reports_the_mode_and_answer_path_never_the_key(env, monkeypatch):  # noqa: F811
    client, _ = env
    monkeypatch.setattr(settings, "retrieval_backend", "local")
    monkeypatch.setattr(settings, "answer_backend", None)
    use(monkeypatch)
    h = client.get("/api/health").json()
    assert h["llm"] is None and h["answer_backend"] == "realtime"
    use(monkeypatch, "", KEY)
    r = client.get("/api/health")
    assert r.json()["llm"] == "ai_studio" and r.json()["answer_backend"] == "gemini"   # local retrieval + Gemini answers
    assert KEY not in r.text
    use(monkeypatch, "p", KEY, True)
    assert client.get("/api/health").json()["llm"] == "vertex"
    assert KEY not in repr(settings) and KEY not in str(settings)


def test_a_key_is_a_local_llm_so_the_public_service_is_not_used(monkeypatch):
    use(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "public_api_url", "https://pub.example")
    assert public_client.active()
    use(monkeypatch, "", KEY)
    assert not public_client.active()


# ---------- every Gemini use goes through the chosen client ----------

def test_answers_with_web_search_go_through_ai_studio(monkeypatch, made):
    use(monkeypatch, "", KEY)
    from app.gcp.llm import GeminiAnswerer

    async def go():
        return [s async for s in GeminiAnswerer().stream_sentences("sys", "q", web_search=True)]
    assert asyncio.run(go()) == ["It is a gas."]
    assert made.calls == [AI_STUDIO]
    model, config = made.models.configs[0]
    assert model == "gemini-2.5-flash" and config.tools[0].google_search is not None   # grounding is offered there too


def test_processing_translation_equations_and_face_detect_go_through_ai_studio(monkeypatch, made):
    use(monkeypatch, "", KEY)
    monkeypatch.setattr(settings, "upload_enrich", True)
    # upload processing / enrichment
    out = asyncio.run(make_llm("gemini").complete_json("s", "u", equations.Readings, "fast"))
    assert out.items == []
    # equations
    assert equations.available()
    # bilingual reading
    made.models.text = json.dumps({"pieces": ["Hola."]})
    assert translate.default_translator() is translate.gemini_translate
    assert asyncio.run(translate.gemini_translate(["Hello."], "Spanish")) == ["Hola."]
    # question translation for search
    made.models.text = "Who?"
    assert asyncio.run(multilingual.translate("¿Quién?")) == "Who?"
    # face detect
    made.models.text = json.dumps({"found": False})
    assert asyncio.run(face_detect._gemini(b"img", "image/png")) == json.dumps({"found": False})
    assert made.calls and all(c == AI_STUDIO for c in made.calls)
    assert len(made.calls) == 4   # enrichment, translation, search translation, face detect
    models = {m for m, _ in made.models.configs}
    assert models <= {"gemini-2.5-flash", "gemini-2.5-flash-lite"}


def test_without_any_gemini_nothing_builds_a_client(monkeypatch, made):
    use(monkeypatch)
    monkeypatch.setattr(settings, "upload_enrich", True)
    assert not equations.available()
    with pytest.raises(translate.Unavailable):
        translate.default_translator()
    with pytest.raises(multilingual.Unavailable):
        asyncio.run(multilingual.translate("¿Quién?"))
    assert asyncio.run(face_detect.detect(b"img", "png")) is None
    assert made.calls == []


def test_vertex_is_unchanged(monkeypatch, made):
    use(monkeypatch, "proj", "")
    monkeypatch.setattr(settings, "gemini_location", "")
    monkeypatch.setattr(settings, "gcp_region", "us-central1")
    GeminiProvider()
    assert made.calls == [{"vertexai": True, "project": "proj", "location": "us-central1"}]


# ---------- quota errors and the key ----------

def ai_studio_429(per_day=False, delay="2s") -> genai_errors.ClientError:
    """Shaped like Google AI Studio's quota answer: the same 'billing details' wording for per-minute and per-day."""
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else \
        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "You exceeded your current quota, please check your plan and billing details.",
                      "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                   "violations": [{"quotaId": quota}]},
                                  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}
    return genai_errors.ClientError(429, body, httpx.Response(429, request=httpx.Request("POST", "https://x")))


def test_ai_studio_per_minute_429_is_retried_per_day_is_not():
    assert retrying.is_transient(ai_studio_429())
    assert not retrying.is_transient(ai_studio_429(per_day=True))
    assert not retrying.is_transient(RuntimeError("insufficient_quota: billing"))   # unchanged


def test_rate_limited_answers_retry_and_never_show_the_key(env, monkeypatch, made, caplog):  # noqa: F811
    client, _ = env
    use(monkeypatch, "", KEY)
    monkeypatch.setattr(settings, "retrieval_backend", "local")
    monkeypatch.setattr(settings, "answer_backend", None)
    monkeypatch.setattr(deps, "answer_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(indexes, "_index_for", lambda book_id: LeakyIndex())
    talk_routes.get_answerer.cache_clear()
    made.models.fail = ai_studio_429(delay="0.01s")
    from app.gcp.llm import GeminiAnswerer

    async def no_sleep(s):
        return None
    monkeypatch.setattr(GeminiAnswerer, "_sleep", staticmethod(no_sleep))
    caplog.set_level(logging.DEBUG)
    try:
        r = client.post("/api/answer", json={"book_id": "demo", "query": "What happened?", "reader_para": 1,
                                             "speaker_id": "harry"})
    finally:
        talk_routes.get_answerer.cache_clear()
    out = lines(r)
    assert out[-1]["type"] == "error" and KEY not in r.text
    assert made.calls == [AI_STUDIO]
    assert len(made.models.configs) > 1   # the per-minute 429 went through the retry path
    assert KEY not in caplog.text
    assert "rate limited" in caplog.text   # the shared pacer saw the 429


# ---------- review fixes (PR #144) ----------

def test_the_ai_studio_client_never_follows_the_vertex_env(monkeypatch, made):
    """GOOGLE_GENAI_USE_VERTEXAI=true in the environment must not send the key to Vertex express mode."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    use(monkeypatch, "", KEY)
    gemini.client()
    assert made.calls == [{"api_key": KEY, "vertexai": False}]


def test_a_per_day_quota_does_not_throttle_the_shared_pacer():
    async def go(err):
        p = pacer.Pacer(start=4, ceiling=8)

        async def fail():
            raise err
        with pytest.raises(genai_errors.ClientError):
            await p.call(fail, priority="interactive", attempts=1)
        return p
    p = asyncio.run(go(ai_studio_429(per_day=True, delay="3600s")))
    assert p.limit == 4 and p.hold_until == 0.0           # final: no cut, no hour-long hold for every queued call
    p = asyncio.run(go(ai_studio_429(delay="0.01s")))
    assert p.limit == 2 and p.hold_until > 0              # a per-minute 429 still throttles, as before


def test_a_wrapped_per_minute_429_is_still_retried():
    """A wrapper with no status that repeats the 429's text ("...billing details...") is not a final quota error."""
    inner = ai_studio_429()
    try:
        try:
            raise inner
        except genai_errors.ClientError as e:
            raise RuntimeError(str(e)) from e
    except RuntimeError as wrapped:
        assert retrying.is_transient(wrapped)
    day = ai_studio_429(per_day=True)
    try:
        try:
            raise day
        except genai_errors.ClientError as e:
            raise RuntimeError(str(e)) from e
    except RuntimeError as wrapped:
        assert not retrying.is_transient(wrapped) and retrying.is_daily_quota(wrapped)
    assert not retrying.is_daily_quota(ai_studio_429())
    assert retrying.is_quota_or_auth(RuntimeError("check your plan and billing details"))   # no 429 anywhere: final


def test_public_client_uses_the_shared_adc_check(monkeypatch):
    """public_client calls gemini.gcp_credentials_usable through the module: one probe, one place to fake it."""
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    use(monkeypatch, "p", "", adc=True)
    assert public_client.local_llm_configured()
    use(monkeypatch, "p", "", adc=False)
    assert not public_client.local_llm_configured()


def test_the_adc_probe_runs_off_the_event_loop_and_never_on_cloud_run(monkeypatch):
    threads = []

    def probe():
        threads.append(threading.current_thread())
        return False
    monkeypatch.setattr(gemini, "gcp_credentials_usable", probe)

    async def go():
        loop_thread = threading.current_thread()
        await gemini.resolve()
        return loop_thread
    monkeypatch.setattr(settings, "gcp_project", "p")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    asyncio.run(go())
    assert threads == [] and gemini.mode() == "vertex"     # the Cloud Run deploy: GCP_PROJECT alone, no probe at all
    monkeypatch.setattr(settings, "gemini_api_key", KEY)
    loop_thread = asyncio.run(go())
    assert len(threads) == 1 and threads[0] is not loop_thread
