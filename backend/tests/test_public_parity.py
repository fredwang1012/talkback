"""Public API parity (server side): question translation, kind detection, title/author, face box and equation
readings for a clone with only a Boson key. Every route is capped, rate limited, validated and never logs content."""
import asyncio
import base64
import json
import logging
import re

import pytest

from app import public_api
from app.config import settings
from app.enrich import spend
from app.enrich.llm import LLM, Raw
from app.ratelimit import TokenBucket

from .test_public_api import MARKER, enrich_body, pub, wait_done  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
B64 = base64.b64encode(PNG).decode()


class ParityFake:
    """Answers each schema like a model would; records what it was sent."""
    name = "fake"

    def __init__(self, fail=False, delay=0.0, tokens=(500, 50), english="Who is Alice?", found=True):
        self.fail, self.delay, self.tokens, self.english, self.found = fail, delay, tokens, english, found
        self.calls: list[str] = []
        self.users: list = []

    async def complete(self, system, user, schema, tier, max_tokens, effort):
        title = schema["title"]
        self.calls.append(title)
        self.users.append(user)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"boom {MARKER}")
        if title == "Translation":
            out = {"english": self.english}
        elif title == "KindGuess":
            out = {"kind": "textbook", "confidence": 0.93}
        elif title == "Guess":
            out = {"title": "Alice's Adventures", "authors": ["Lewis Carroll"]}
        elif title == "FaceBox":
            out = {"found": self.found, "x": 0.2, "y": 0.1, "w": 0.5, "h": 0.6}
        elif title == "Readings":
            ids = [m.group(1) for part in user for m in [re.match(r"Picture (\w+) ", getattr(part, "text", None) or "")]
                   if m]
            out = {"items": [{"id": i, "latex": f"x_{{{i}}}", "spoken": f"x {i}"} for i in ids]}
        else:
            raise AssertionError(title)
        return Raw(text=json.dumps(out), in_tokens=self.tokens[0], out_tokens=self.tokens[1], model="gemini-test")


@pytest.fixture
def par(pub, monkeypatch):  # noqa: F811
    fake = ParityFake()
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": fake}, fallback=False))
    monkeypatch.setattr(public_api, "small_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(public_api, "face_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(public_api, "face_calls", public_api.DailyCount(public_api.FACE_CALLS_FILE))
    monkeypatch.setattr(public_api, "eq_start_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(public_api, "eq_bucket", TokenBucket(1000, 60))
    monkeypatch.setattr(public_api, "_tickets", {})
    monkeypatch.setattr(public_api, "_eq_running", 0)
    pub.fake = fake
    return pub


def use(monkeypatch, fake):
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": fake}, fallback=False))


# ---------- routes ----------

def test_the_new_routes_exist_and_nothing_else(par):
    paths = {r.path for r in public_api.app.routes if hasattr(r, "methods")}
    assert paths == {"/api/health", "/api/public/enrich", "/api/public/enrich/{job_id}", "/api/public/answer",
                     "/api/public/translate", "/api/public/detect", "/api/public/describe",
                     "/api/public/faces/detect", "/api/public/equations", "/api/public/equations/{ticket}"}


# ---------- translate (#12) ----------

def test_translate_a_foreign_question(par):
    r = par.post("/api/public/translate", json={"text": "¿Quién es la reina y por qué está enfadada?"})
    assert r.status_code == 200 and r.json() == {"text": "Who is Alice?"}
    assert par.fake.calls == ["Translation"]
    assert public_api.answer_budget.spent() > 0 and public_api.answer_budget.reserved == 0   # counted on the daily cap


def test_translate_an_english_question_makes_no_call(par):
    r = par.post("/api/public/translate", json={"text": "Who is the queen and why is she angry?"})
    assert r.json() == {"text": "Who is the queen and why is she angry?"} and par.fake.calls == []
    assert public_api.answer_budget.spent() == 0


def test_translate_caps_limits_and_failures(par, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    q = {"text": f"¿Quién es la reina y por qué está {MARKER}?"}
    assert par.post("/api/public/translate", json={"text": "x" * 501}).status_code == 422
    assert par.post("/api/public/translate", json={}).status_code == 422
    use(monkeypatch, ParityFake(fail=True))
    r = par.post("/api/public/translate", json=q)
    assert r.status_code == 502 and r.json()["code"] == "failed" and MARKER not in r.text
    use(monkeypatch, ParityFake(delay=5))
    monkeypatch.setattr(public_api, "TRANSLATE_SECONDS", 0.2)
    r = par.post("/api/public/translate", json=q)
    assert r.status_code == 504 and r.json()["code"] == "timeout"
    monkeypatch.setattr(settings, "public_answer_daily_cap_usd", 0.0)
    r = par.post("/api/public/translate", json=q)
    assert r.status_code == 402 and r.json()["code"] == "daily_cap"
    monkeypatch.setattr(public_api, "small_bucket", TokenBucket(0, 60))
    assert par.post("/api/public/translate", json=q).status_code == 429
    assert MARKER not in caplog.text


# ---------- kind detection and title/author ----------

def test_detect_and_describe(par):
    r = par.post("/api/public/detect", json={"text": "Chapter 1. Definitions. Exercise 1.2"})
    assert r.status_code == 200 and r.json() == {"kind": "textbook", "confidence": 0.93}
    r = par.post("/api/public/describe", json={"text": "ALICE'S ADVENTURES IN WONDERLAND by Lewis Carroll"})
    assert r.status_code == 200 and r.json() == {"title": "Alice's Adventures", "authors": ["Lewis Carroll"]}
    assert par.fake.calls == ["KindGuess", "Guess"] and public_api.answer_budget.spent() > 0


def test_detect_and_describe_size_limits(par):
    assert par.post("/api/public/detect", json={"text": "x" * (public_api.DETECT_MAX_CHARS + 1)}).status_code == 422
    assert par.post("/api/public/describe", json={"text": "x" * (public_api.DESCRIBE_MAX_CHARS + 1)}).status_code == 422
    assert par.post("/api/public/describe", json={"text": ""}).status_code == 422
    json_h = {"content-type": "application/json"}
    big = b"x" * (public_api.SMALL_BODY_LIMIT + 1)
    for path in ("/api/public/translate", "/api/public/detect", "/api/public/describe"):
        assert par.post(path, content=big, headers=json_h).status_code == 413


# ---------- face box (#109) ----------

def test_face_detect(par):
    r = par.post("/api/public/faces/detect", json={"image": B64})
    assert r.status_code == 200 and r.json() == {"face": {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.6}}
    assert par.fake.calls == ["FaceBox"] and public_api.answer_budget.spent() > 0


def test_face_detect_no_face_and_failure(par, monkeypatch):
    use(monkeypatch, ParityFake(found=False))
    assert par.post("/api/public/faces/detect", json={"image": B64}).json() == {"face": None}
    use(monkeypatch, ParityFake(fail=True))
    r = par.post("/api/public/faces/detect", json={"image": B64})
    assert r.status_code == 502 and MARKER not in r.text


def test_face_detect_validates_the_image(par):
    for bad in ("not base64!!", base64.b64encode(b"GIF89a" + b"\x00" * 20).decode(), ""):
        r = par.post("/api/public/faces/detect", json={"image": bad})
        assert r.status_code == 422 and r.json()["code"] == "invalid", bad
    json_h = {"content-type": "application/json"}
    assert par.post("/api/public/faces/detect", content=b"x" * (public_api.FACE_BODY_LIMIT + 1),
                    headers=json_h).status_code == 413
    assert par.fake.calls == []


def test_face_detect_daily_call_cap_is_persisted(par, monkeypatch):
    monkeypatch.setattr(public_api, "FACE_DAILY_CALLS", 1)
    assert par.post("/api/public/faces/detect", json={"image": B64}).status_code == 200
    r = par.post("/api/public/faces/detect", json={"image": B64})
    assert r.status_code == 402 and r.json()["code"] == "face_cap"
    monkeypatch.setattr(public_api, "face_calls", public_api.DailyCount(public_api.FACE_CALLS_FILE))  # a restart
    assert par.post("/api/public/faces/detect", json={"image": B64}).status_code == 402
    assert len(par.fake.calls) == 1


def test_face_detect_rate_limit(par, monkeypatch):
    monkeypatch.setattr(public_api, "face_bucket", TokenBucket(1, 3600))
    assert par.post("/api/public/faces/detect", json={"image": B64}).status_code == 200
    assert par.post("/api/public/faces/detect", json={"image": B64}).status_code == 429
    assert public_api.FACE_DAILY_CALLS <= 200


# ---------- equation readings (#41) ----------

def items(*ids):
    return [{"id": i, "png": B64, "hint": "y = b x"} for i in ids]


def start(c, n, headers=None):
    return c.post("/api/public/equations", json={"count": n}, headers=headers or {})


def test_equations_ticket_then_readings(par):
    r = start(par, 3)
    assert r.status_code == 200
    t = r.json()
    assert t["accepted"] == 3 and t["estimate_usd"] > 0 and t["batch"] == public_api.EQ_ITEMS_PER_REQUEST
    assert public_api._reserved_usd() == pytest.approx(t["estimate_usd"])      # reserved against the total cap
    assert public_api.ip_budget.spent("testclient") == pytest.approx(t["estimate_usd"])
    r = par.post(f"/api/public/equations/{t['ticket']}", json={"items": items("e1", "m2")})
    assert r.status_code == 200
    assert r.json()["readings"] == {"e1": {"latex": "x_{e1}", "spoken": "x e1"}, "m2": {"latex": "x_{m2}", "spoken": "x m2"}}
    assert "stopped" not in r.json() and spend.total_usd() > 0
    r = par.post(f"/api/public/equations/{t['ticket']}", json={"items": items("e3")})
    assert r.status_code == 200 and t["ticket"] not in public_api._tickets              # all pictures sent: closed
    assert public_api._reserved_usd() == 0
    settled = max(spend.total_usd(), public_api.EQ_MIN_TICKET_USD)                 # real spend, at least the minimum
    assert public_api.ip_budget.spent("testclient") == pytest.approx(settled)
    assert par.post(f"/api/public/equations/{t['ticket']}", json={"items": items("e4")}).status_code == 404


def test_equations_fit_under_the_book_cap(par, monkeypatch):
    from app.enrich import equations
    monkeypatch.setattr(settings, "public_book_cap_usd", equations.estimate(10))
    t = start(par, 100).json()
    assert 0 < t["accepted"] <= 10
    monkeypatch.setattr(settings, "public_book_cap_usd", 0.0)
    r = start(par, 5)
    assert r.status_code == 402 and r.json()["code"] == "book_cap"


def test_equations_total_and_ip_caps(par, monkeypatch):
    spend.add("earlier", "gemini", settings.public_total_cap_usd)
    r = start(par, 5)
    assert r.status_code == 402 and r.json()["code"] == "cap"
    monkeypatch.setattr(spend, "_state", None)
    monkeypatch.setattr(settings, "data_dir", settings.data_dir / "fresh")
    monkeypatch.setattr(settings, "public_ip_daily_cap_usd", 0.0)
    r = start(par, 5)
    assert r.status_code == 402 and r.json()["code"] == "ip_cap"


def test_equations_count_and_item_validation(par):
    assert start(par, 0).status_code == 422
    assert start(par, public_api.EQ_MAX_PICTURES + 1).status_code == 422
    t = start(par, 30).json()["ticket"]
    url = f"/api/public/equations/{t}"
    too_many = items(*[f"e{i}" for i in range(public_api.EQ_ITEMS_PER_REQUEST + 1)])
    assert par.post(url, json={"items": too_many}).status_code == 422
    assert par.post(url, json={"items": [{"id": "../x", "png": B64, "hint": ""}]}).status_code == 422
    assert par.post(url, json={"items": [{"id": "e1", "png": "!!!", "hint": ""}]}).status_code == 422
    not_png = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 20).decode()
    assert par.post(url, json={"items": [{"id": "e1", "png": not_png, "hint": ""}]}).status_code == 422
    assert par.post(url, json={"items": items("e1", "e1")}).status_code == 422
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * public_api.EQ_PNG_MAX).decode()
    assert par.post(url, json={"items": [{"id": "e1", "png": big, "hint": ""}]}).status_code in (413, 422)
    assert par.fake.calls == []
    json_h = {"content-type": "application/json"}
    assert par.post(url, content=b"x" * (public_api.EQ_BODY_LIMIT + 1), headers=json_h).status_code == 413


def test_equations_ticket_holds_only_its_count_and_its_address(par, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    a, b = {"x-forwarded-for": "1.1.1.1"}, {"x-forwarded-for": "2.2.2.2"}
    t = start(par, 2, a).json()["ticket"]
    url = f"/api/public/equations/{t}"
    assert par.post(url, json={"items": items("e1")}, headers=b).status_code == 404        # not this address's ticket
    r = par.post(url, json={"items": items("e1", "e2", "e3")}, headers=a)
    assert r.status_code == 422 and r.json()["code"] == "too_many"
    assert par.post(url, json={"items": items("e1", "e2")}, headers=a).status_code == 200


def test_equations_stop_at_the_book_cap_and_keep_what_was_read(par, monkeypatch):
    fake = ParityFake(tokens=(5_000_000, 0))      # each call costs the whole per-book cap
    use(monkeypatch, fake)
    t = start(par, 40).json()
    r = par.post(f"/api/public/equations/{t['ticket']}", json={"items": items(*[f"e{i}" for i in range(24)])})
    body = r.json()
    assert r.status_code == 200 and body["stopped"] == "book_cap" and len(body["readings"]) <= 8
    assert t["ticket"] not in public_api._tickets                    # a stopped ticket is closed and settled
    assert public_api._reserved_usd() == 0


def test_equations_busy_and_expired_tickets(par, monkeypatch):
    t = start(par, 4).json()["ticket"]
    monkeypatch.setattr(public_api, "_eq_running", public_api.MAX_EQ_REQUESTS)
    r = par.post(f"/api/public/equations/{t}", json={"items": items("e1")})
    assert r.status_code == 429 and r.json()["code"] == "busy"
    monkeypatch.setattr(public_api, "_eq_running", 0)
    public_api._tickets[t].used -= public_api.EQ_TICKET_TTL + 5
    assert start(par, 1).status_code == 200                           # purging settles the idle ticket
    assert t not in public_api._tickets


def test_one_open_ticket_per_address_and_a_large_global_limit(par, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    a, b = {"x-forwarded-for": "1.1.1.1"}, {"x-forwarded-for": "2.2.2.2"}
    assert start(par, 2, a).status_code == 200
    r = start(par, 2, a)
    assert r.status_code == 429 and r.json()["code"] == "ticket_open"   # one address cannot hold every slot
    assert start(par, 2, b).status_code == 200
    assert public_api.MAX_TICKETS >= 16 and public_api.MAX_TICKETS_PER_IP == 1
    monkeypatch.setattr(public_api, "MAX_TICKETS", 2)
    r = start(par, 2, {"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 429 and r.json()["code"] == "busy"


def test_a_ticket_without_pictures_expires_quickly(par):
    t = start(par, 4).json()["ticket"]
    assert public_api.EQ_TICKET_START_TTL <= 180
    public_api._tickets[t].used -= public_api.EQ_TICKET_START_TTL + 1
    public_api._purge_tickets()
    assert t not in public_api._tickets
    t2 = start(par, 4).json()["ticket"]                                # used once: the longer idle limit applies
    assert par.post(f"/api/public/equations/{t2}", json={"items": items("e1")}).status_code == 200
    public_api._tickets[t2].used -= public_api.EQ_TICKET_START_TTL + 1
    public_api._purge_tickets()
    assert t2 in public_api._tickets


def test_each_ticket_charges_a_minimum_to_the_address(par, monkeypatch):
    monkeypatch.setattr(settings, "public_ip_daily_cap_usd", public_api.EQ_MIN_TICKET_USD * 3)
    for _ in range(3):
        t = start(par, 1).json()
        assert t["estimate_usd"] >= public_api.EQ_MIN_TICKET_USD
        public_api._close_ticket(public_api._tickets[t["ticket"]])     # abandoned: still costs the minimum
    r = start(par, 1)
    assert r.status_code == 402 and r.json()["code"] == "ip_cap"


def test_one_request_at_a_time_per_ticket_and_spend_adds_up(par, monkeypatch):
    import httpx
    use(monkeypatch, ParityFake(delay=0.3))
    t = start(par, 20).json()["ticket"]
    url = f"/api/public/equations/{t}"

    async def go():
        transport = httpx.ASGITransport(app=public_api.app, client=("testclient", 50000))  # the ticket's address
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await asyncio.gather(c.post(url, json={"items": items("e1", "e2")}),
                                        c.post(url, json={"items": items("e3", "e4")}))
    codes = sorted(r.status_code for r in asyncio.run(go()))
    assert codes == [200, 429]
    assert public_api._tickets[t].usd == pytest.approx(spend.total_usd())
    use(monkeypatch, ParityFake())
    assert par.post(url, json={"items": items("e5")}).status_code == 200
    assert public_api._tickets[t].usd == pytest.approx(spend.total_usd())   # sequential requests add, never overwrite


def test_a_busy_ticket_is_never_purged_or_settled(par):
    t = start(par, 4).json()["ticket"]
    tk = public_api._tickets[t]
    tk.busy = True
    tk.used -= public_api.EQ_TICKET_TTL + 5
    public_api._purge_tickets()
    assert t in public_api._tickets
    tk.busy = False


def test_equation_failures_never_leak_or_log(par, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    use(monkeypatch, ParityFake(fail=True))
    t = start(par, 1).json()["ticket"]
    r = par.post(f"/api/public/equations/{t}",
                 json={"items": [{"id": "e1", "png": B64, "hint": f"secret {MARKER}"}]})
    assert r.status_code == 200 and r.json()["readings"] == {} and MARKER not in r.text
    assert MARKER not in caplog.text


# ---------- novel results carry the series ----------

def test_enrich_result_has_series_fields(par, monkeypatch):
    from .test_novel_pipeline import FakeProvider
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": FakeProvider()}))
    job = par.post("/api/public/enrich", json=enrich_body()).json()["job_id"]
    view, _ = wait_done(par, job)
    assert "series" in view["result"] and "series_index" in view["result"]


def test_starting_an_enrichment_purges_abandoned_tickets(par):
    """An abandoned ticket must not keep inflating the reserved spend for enrich admissions (review of #133)."""
    t = start(par, 4).json()["ticket"]
    public_api._tickets[t].used -= public_api.EQ_TICKET_START_TTL + 1
    assert par.post("/api/public/enrich", json=enrich_body()).status_code == 202
    assert t not in public_api._tickets
