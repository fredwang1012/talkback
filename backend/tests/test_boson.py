import httpx
import pytest

from app import boson
from app.boson import BosonClient, BosonError
from app.config import settings


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "boson_api_key", "test-key")

    async def no_sleep(_):
        pass

    monkeypatch.setattr(boson.asyncio, "sleep", no_sleep)


def client(handler):
    return BosonClient(transport=httpx.MockTransport(handler))


async def test_rate_limit_is_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "slow"}})
        return httpx.Response(200, json={"value": "bai-eph-x", "expires_at": 1, "session": {"id": "s"}})

    out = await client(handler).mint_client_secret()
    assert len(calls) == 3
    assert out == {"value": "bai-eph-x", "expires_at": 1}  # session dropped


async def test_insufficient_quota_never_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"error": {"type": "insufficient_quota", "code": "monthly_cap_reached", "message": "cap"}})

    with pytest.raises(BosonError) as e:
        await client(handler).mint_client_secret()
    assert len(calls) == 1
    assert e.value.code == "monthly_cap_reached"


async def test_400_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad"}})

    with pytest.raises(BosonError):
        await client(handler).create_speech("hi")
    assert len(calls) == 1


async def test_create_voice_accepts_either_id_field():
    for payload in ({"voice": "voice_a"}, {"voice_id": "voice_a"}):
        c = client(lambda req, p=payload: httpx.Response(200, json=p))
        assert await c.create_voice("AAAA", "hello") == "voice_a"


async def test_missing_key_is_readable(monkeypatch):
    monkeypatch.setattr(settings, "boson_api_key", "")
    with pytest.raises(RuntimeError, match="BOSON_API_KEY"):
        await client(lambda r: httpx.Response(200)).mint_client_secret()
