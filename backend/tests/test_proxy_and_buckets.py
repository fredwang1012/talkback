from types import SimpleNamespace

from app import deps
from app.config import settings
from app.ratelimit import TokenBucket


def _req(xff=None, host="10.0.0.1"):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_trust_proxy_uses_rightmost_xff_and_ignores_spoofed_left(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    assert deps.client_ip(_req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    assert deps.client_ip(_req("1.2.3.4, ")) == "1.2.3.4"


def test_trust_proxy_without_header_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    assert deps.client_ip(_req()) == "10.0.0.1"
    assert deps.client_ip(_req("")) == "10.0.0.1"


def test_trust_proxy_off_ignores_header(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", False)
    assert deps.client_ip(_req("6.6.6.6, 1.2.3.4")) == "10.0.0.1"


def test_bucket_state_is_bounded_and_evicts_oldest():
    b = TokenBucket(1, 60, max_keys=3)
    for k in ("a", "b", "c"):
        assert b.allow(k)
    assert not b.allow("a")  # touches a: now b is oldest
    assert b.allow("d")      # evicts b
    assert len(b._state) == 3 and "b" not in b._state and "a" in b._state
    for i in range(50):
        b.allow(f"k{i}")
    assert len(b._state) == 3
