"""Bilingual reading on a clone (B3): POST /api/public/translate-passage translates one paragraph's pieces with
translate.py's prompt, capped like the other small calls; public_client.translate_passage calls it."""
import asyncio
import json
import logging

import httpx

from app import public_api, public_client
from app.enrich.llm import LLM, Raw
from app.ratelimit import TokenBucket

from .test_public_api import MARKER, pub  # noqa: F401  (fixture)


class PassageFake:
    name = "fake"

    def __init__(self, fail=False):
        self.fail, self.systems, self.users = fail, [], []

    async def complete(self, system, user, schema, tier, max_tokens, effort):
        assert schema["title"] == "Pieces"
        self.systems.append(system)
        self.users.append(user)
        if self.fail:
            raise RuntimeError(f"boom {MARKER}")
        pieces = json.loads(user)
        return Raw(text=json.dumps({"pieces": [f"ES {p}" for p in pieces]}), in_tokens=100, out_tokens=50, model="gemini-test")


def _use(monkeypatch, fake):
    monkeypatch.setattr(public_api, "make_llm", lambda *a, **k: LLM("gemini", {"gemini": fake}, fallback=False))
    monkeypatch.setattr(public_api, "small_bucket", TokenBucket(1000, 60))


def test_passage_is_translated_and_counted(pub, monkeypatch):  # noqa: F811
    fake = PassageFake()
    _use(monkeypatch, fake)
    r = pub.post("/api/public/translate-passage", json={"pieces": ["Alice was tired.", "She sat."], "language": "spa"})
    assert r.status_code == 200 and r.json() == {"pieces": ["ES Alice was tired.", "ES She sat."]}
    assert "Spanish" in fake.systems[0] and "names" in fake.systems[0]
    assert public_api.answer_budget.spent() > 0 and public_api.answer_budget.reserved == 0


def test_passage_validation_caps_and_no_logging(pub, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _use(monkeypatch, PassageFake())
    post = lambda body: pub.post("/api/public/translate-passage", json=body)  # noqa: E731
    assert post({"pieces": ["Hi."], "language": "eng"}).status_code == 422
    assert post({"pieces": ["Hi."], "language": "xx"}).status_code == 422
    assert post({"pieces": [], "language": "spa"}).status_code == 422
    assert post({"pieces": ["x" * 601], "language": "spa"}).status_code == 422
    assert post({"pieces": ["x"] * 41, "language": "spa"}).status_code == 422
    _use(monkeypatch, PassageFake(fail=True))
    r = post({"pieces": [f"secret {MARKER}"], "language": "fra"})
    assert r.status_code == 502 and MARKER not in r.text
    monkeypatch.setattr(public_api, "small_bucket", TokenBucket(0, 60))
    assert post({"pieces": ["Hi."], "language": "spa"}).status_code == 429
    assert MARKER not in caplog.text


def test_client_posts_pieces_and_language(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"pieces": ["Hola."]})

    client = httpx.AsyncClient(base_url="https://pub.example", transport=httpx.MockTransport(handler))
    out = asyncio.run(public_client.translate_passage(["Hello."], "spa", client))
    assert out == ["Hola."] and seen == {"path": "/api/public/translate-passage",
                                         "body": {"pieces": ["Hello."], "language": "spa"}}
