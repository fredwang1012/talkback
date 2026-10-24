"""Gemini prices follow the model actually used (Flash vs Flash-Lite), in estimates and in the spend ledger."""
import asyncio

import pytest

from app.config import settings
from app.enrich import llm


def test_flash_and_flash_lite_have_their_own_vertex_prices():
    assert llm.gemini_prices("gemini-2.5-flash") == (0.30, 2.50)
    assert llm.gemini_prices("gemini-2.5-flash-lite") == (0.10, 0.40)
    assert llm.gemini_prices("publishers/google/models/gemini-2.5-flash-lite-001") == (0.10, 0.40)
    assert llm.gemini_prices("some-future-model") == (0.30, 2.50)   # unknown: priced as Flash, never as free


def test_price_usd_uses_the_model():
    lite = llm.price_usd("gemini", "fast", 1_000_000, 1_000_000, model="gemini-2.5-flash-lite")
    flash = llm.price_usd("gemini", "fast", 1_000_000, 1_000_000, model="gemini-2.5-flash")
    assert lite == pytest.approx(0.50) and flash == pytest.approx(2.80)


def test_estimates_follow_the_configured_enrichment_model(monkeypatch):
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash")
    monkeypatch.setattr(settings, "enrich_gemini_model", "gemini-2.5-flash-lite")
    assert llm.estimate_usd("gemini", "smart", 3.5e6, 1e6) == pytest.approx(0.50)
    monkeypatch.setattr(settings, "enrich_gemini_model", "")
    assert llm.estimate_usd("gemini", "smart", 3.5e6, 1e6) == pytest.approx(2.80)


def test_claude_prices_unchanged():
    assert llm.price_usd("claude", "fast", 1_000_000, 0) == pytest.approx(0.10)
    assert llm.price_usd("claude", "smart", 0, 1_000_000) == pytest.approx(10.00)


def test_spend_ledger_is_charged_at_the_model_that_answered(monkeypatch):
    class Fake:
        name = "gemini"

        async def complete(self, system, user, schema, tier, max_tokens, effort):
            return llm.Raw(text='{"text": "x", "in_tokens": 0, "out_tokens": 0, "model": "m"}',
                           in_tokens=1_000_000, out_tokens=1_000_000, model="gemini-2.5-flash-lite")

    seen = []
    m = llm.LLM("gemini", providers={"gemini": Fake()}, on_usage=lambda *a: seen.append(a), fallback=False)
    asyncio.run(m.complete_json("s", "u", llm.Raw))
    assert seen[0][1] == "gemini-2.5-flash-lite" and seen[0][4] == pytest.approx(0.50)
