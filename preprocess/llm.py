"""Shared Anthropic helper for preprocess scripts (offline use only; never imported by the backend app).

- Structured outputs via output_config.format (JSON schema generated from a pydantic model).
- Every result cached to data/cache/llm/<sha>.json so reruns are free.
- Token usage and cost tracked per run; call print_usage() at the end of a script.
- No temperature/top_p/top_k and no prefill (Haiku/Sonnet 5.5 reject them). Content blocks are selected by type.
"""
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings  # noqa: E402
from app.gcp import usage as usage_log  # noqa: E402

CACHE = ROOT / "data" / "cache" / "llm"
CACHE.mkdir(parents=True, exist_ok=True)

# $ per 1M tokens (input, output) for prompts under 100K tokens
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

T = TypeVar("T", bound=BaseModel)
_client: AsyncAnthropic | None = None
_sem = asyncio.Semaphore(int(os.environ.get("LLM_CONCURRENCY", "8")))
usage = {"calls": 0, "cached": 0, "in": 0, "out": 0, "usd": 0.0}


class LLMRefusal(Exception):
    pass


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            sys.exit("ANTHROPIC_API_KEY is not set in talkback/.env")
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=5)
    return _client


def estimate_cost(model: str, input_chars: int, expected_output_tokens: int, calls: int = 1) -> float:
    pin, pout = PRICES[model]
    return calls * (input_chars / 4 * pin + expected_output_tokens * pout) / 1e6


def strict_schema(schema_model: type[BaseModel]) -> dict:
    return schema_model.model_json_schema()


async def call_structured(model: str, system: str, user: str, schema_model: type[T], *,
                          max_tokens: int = 8000, effort: str = "low") -> T:
    schema = strict_schema(schema_model)
    key = hashlib.sha256(json.dumps([model, system, user, schema, effort, max_tokens], sort_keys=True).encode()).hexdigest()
    path = CACHE / f"{key}.json"
    if path.exists():
        usage["cached"] += 1
        return schema_model.model_validate_json(path.read_text(encoding="utf-8"))

    async with _sem:
        resp = await client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],  # must end with a user turn: no prefill
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    usage["calls"] += 1
    usage["in"] += resp.usage.input_tokens
    usage["out"] += resp.usage.output_tokens
    pin, pout = PRICES[model]
    cost = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
    usage["usd"] += cost
    usage_log.record("anthropic", model, input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens, usd=round(cost, 6))  # real calls only, never cache hits

    if resp.stop_reason == "refusal":
        raise LLMRefusal(f"{model} refused: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"{model} hit max_tokens={max_tokens}; raise it")
    text = next(b.text for b in resp.content if b.type == "text")  # select by type, never by index
    result = schema_model.model_validate_json(text)
    path.write_text(result.model_dump_json(), encoding="utf-8")
    return result


def print_usage() -> None:
    print(f"LLM usage: {usage['calls']} calls ({usage['cached']} cached), "
          f"{usage['in']} in / {usage['out']} out tokens, ${usage['usd']:.4f}")
