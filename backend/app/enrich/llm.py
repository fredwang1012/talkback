"""One small LLM interface for the upload pipeline and the preprocess scripts: Claude or Gemini 2.5 Flash.

`LLM.complete_json(system, user, schema_model, tier)` returns a validated pydantic object. Tiers: "fast" (Haiku /
Gemini Flash) and "smart" (Sonnet / Gemini Flash). With Claude, an out-of-credit error (insufficient_quota, billing,
402, "credit balance is too low"), auth errors (401/403) and persistent overload/5xx switch the rest of the job to
Gemini; Claude being unavailable never breaks an upload. Token usage is priced per provider and reported through `on_usage`.
"""
import asyncio
import logging
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel

from .. import pacer as pacing
from ..config import settings
from ..gcp import usage as usage_log

log = logging.getLogger("talkback.enrich")
T = TypeVar("T", bound=BaseModel)

# $ per 1M tokens (input, output). Claude prices as in preprocess/llm.py (prompts under 100K tokens).
# Gemini 2.5 Flash: $0.30 input / $2.50 output, the figures used by scripts/usage.py (estimates, not a billing source).
CLAUDE_MODELS = {"fast": "claude-haiku-5-5", "smart": "claude-sonnet-5-5"}
PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "claude": {"fast": (0.10, 0.50), "smart": (2.00, 10.00)},
    "gemini": {"fast": (0.30, 2.50), "smart": (0.30, 2.50)},
}
CHARS_PER_TOKEN = 3.5  # prose, measured roughly on the HP books (~3 to 4)


class LLMRefusal(Exception):
    """The model refused to answer."""


class ProviderUnavailable(Exception):
    """The provider cannot serve the job (credit, auth, persistent overload); with Claude the job moves to Gemini."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


class QuotaError(ProviderUnavailable):
    """The provider's credit or quota is exhausted (never retried)."""

    def __init__(self, detail: str = ""):
        super().__init__("out of credit", detail)


class SpendLimit(Exception):
    """A spend cap was reached; the job stops. Deliberately not a RuntimeError (callers fall back on those)."""


class Raw(BaseModel):
    text: str
    in_tokens: int
    out_tokens: int
    model: str


class Provider(Protocol):
    name: str

    async def complete(self, system: str, user: str, schema: dict, tier: str, max_tokens: int, effort: str) -> Raw: ...


def price_usd(provider: str, tier: str, in_tokens: int, out_tokens: int) -> float:
    pin, pout = PRICES[provider][tier]
    return (in_tokens * pin + out_tokens * pout) / 1e6


def estimate_usd(provider: str, tier: str, in_chars: float, out_tokens: float) -> float:
    """Cost of in_chars of input and out_tokens of output in total."""
    return price_usd(provider, tier, int(in_chars / CHARS_PER_TOKEN), int(out_tokens))


def unavailable_reason(exc: BaseException) -> str | None:
    """Why Claude is unusable for the rest of the job (None: an ordinary error, no switch).

    Out of credit, bad or unauthorised key (401/403), still rate limited (429) or overloaded / failing (5xx, 529,
    connection errors) after the SDK's own retries.
    """
    import anthropic
    if is_quota_error(exc):
        return "out of credit"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in (401, 403):
            return "authentication failed"
        if exc.status_code == 429 or exc.status_code >= 500:
            return f"unavailable (HTTP {exc.status_code})"
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return "unreachable"
    return None


def no_llm_available(exc: BaseException | None) -> bool:
    """True when `exc` means no model can run at all: no credentials, or every provider unusable (not a one-off error)."""
    while exc is not None:
        if type(exc).__name__ in ("DefaultCredentialsError", "RefreshError", "PermissionDenied", "Unauthenticated"):
            return True
        if isinstance(exc, RuntimeError) and "provider is unavailable" in str(exc):
            return True
        exc = exc.__cause__
    return False


def is_quota_error(exc: BaseException) -> bool:
    """Anthropic out-of-credit shapes: HTTP 402, error.type insufficient_quota/billing_error, or the credit message."""
    import anthropic
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    if exc.status_code == 402:
        return True
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    etype = (err or {}).get("type")
    if etype in ("insufficient_quota", "billing_error"):
        return True
    msg = (str((err or {}).get("message", "")) + " " + str(getattr(exc, "message", ""))).lower()
    return exc.status_code in (400, 403) and ("credit balance" in msg or "billing" in msg)


class ClaudeProvider:
    name = "claude"

    def __init__(self, client=None):
        if client is None:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=5)  # SDK retries 429/5xx
        self.client = client

    async def complete(self, system, user, schema, tier, max_tokens, effort) -> Raw:
        model = CLAUDE_MODELS[tier]
        try:
            resp = await self.client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],  # ends with a user turn: no prefill; no temperature
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as e:
            reason = unavailable_reason(e)
            if reason == "out of credit":
                raise QuotaError(str(e)) from e
            if reason:
                raise ProviderUnavailable(reason, str(e)) from e
            raise
        n_in, n_out = resp.usage.input_tokens, resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise LLMRefusal(f"{model} refused")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError(f"{model} hit max_tokens={max_tokens}")
        text = next((b.text for b in resp.content if b.type == "text"), "")  # select by type, never by index
        return Raw(text=text, in_tokens=n_in, out_tokens=n_out, model=model)


class GeminiProvider:
    """Gemini 2.5 Flash on Vertex AI (project and region from settings), JSON output."""
    name = "gemini"
    RETRIES = pacing.BACKGROUND["attempts"]   # 6 attempts: 429s, 5xx and network errors, backoff to 30 s + jitter
    priority: pacing.Priority = "background"     # queued behind Talk answers and translations in the shared pacer

    def __init__(self, client=None, model: str | None = None, sleep=None, pacer: "pacing.Pacer | None" = None):
        if client is None:
            from google import genai
            client = genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_region())
        self.client = client
        self.model = model or settings.enrich_gemini_model or settings.gemini_model
        self._sleep = sleep
        self._pacer = pacer

    async def complete(self, system, user, schema, tier, max_tokens, effort) -> Raw:
        from google.genai import types
        # Gemini 3 rejects thinking_budget; drop it and retry once when the model says so.
        thinking = True

        def config():
            # Gemini 3 ignores thinking_budget and spends the output cap on hidden thoughts, so leave room for them.
            return types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=max(max_tokens, 1024) + (0 if thinking else 8192),
                response_mime_type="application/json", response_json_schema=schema,
                **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if thinking else {}),
            )

        async def once():
            nonlocal thinking
            try:
                return await self.client.aio.models.generate_content(model=self.model, contents=user, config=config())
            except Exception as e:
                if not thinking or "Thinking budget is not supported" not in str(e):
                    raise
                thinking = False
            return await self.client.aio.models.generate_content(model=self.model, contents=user, config=config())

        resp = await (self._pacer or pacing.shared).call(once, priority=self.priority, what="gemini call",
                                                         attempts=self.RETRIES, sleep=self._sleep)
        meta = getattr(resp, "usage_metadata", None)
        n_in = (getattr(meta, "prompt_token_count", 0) or 0) if meta else 0
        n_out = ((getattr(meta, "candidates_token_count", 0) or 0)
                 + (getattr(meta, "thoughts_token_count", 0) or 0)) if meta else 0
        cands = getattr(resp, "candidates", None) or []
        reason = str(getattr(cands[0], "finish_reason", "") or "") if cands else ""
        if any(w in reason for w in ("SAFETY", "PROHIBITED", "BLOCKLIST")) or (not cands and getattr(resp, "prompt_feedback", None)):
            raise LLMRefusal(f"{self.model} refused ({reason})")
        if "MAX_TOKENS" in reason:
            raise RuntimeError(f"{self.model} hit max_tokens={max_tokens}")
        return Raw(text=resp.text or "", in_tokens=n_in, out_tokens=n_out, model=self.model)


UsageHook = Callable[[str, str, int, int, float], None]  # provider, model, in_tokens, out_tokens, usd


class LLM:
    """Provider switch with a concurrency limit, usage metering and automatic Claude -> Gemini fallback."""

    def __init__(self, primary: str = "claude", providers: dict[str, Provider] | None = None, *,
                 concurrency: int | None = None, on_usage: UsageHook | None = None, fallback: bool = True):
        self.providers: dict[str, Provider] = providers if providers is not None else {}
        self.active = primary
        self.fallback = fallback
        self.on_usage = on_usage
        self.concurrency = concurrency or settings.upload_concurrency
        self.sem = asyncio.Semaphore(self.concurrency)
        self.in_flight = self.peak = 0
        self.usd = 0.0
        self.calls = 0
        self.tokens_in = self.tokens_out = 0
        self.switched = False
        self.stopped: Exception | None = None  # set to stop the job (spend cap): later calls raise it at once

    def provider(self, name: str) -> Provider:
        if name not in self.providers:
            self.providers[name] = ClaudeProvider() if name == "claude" else GeminiProvider()
        return self.providers[name]

    def _record(self, provider: str, tier: str, raw: Raw) -> None:
        usd = price_usd(provider, tier, raw.in_tokens, raw.out_tokens)
        self.usd += usd
        self.calls += 1
        self.tokens_in += raw.in_tokens
        self.tokens_out += raw.out_tokens
        if provider == "claude":
            usage_log.record("anthropic", raw.model, input_tokens=raw.in_tokens, output_tokens=raw.out_tokens, usd=round(usd, 6))
        else:
            usage_log.record("llm", raw.model, prompt_tokens=raw.in_tokens, output_tokens=raw.out_tokens)
        if self.on_usage:
            self.on_usage(provider, raw.model, raw.in_tokens, raw.out_tokens, usd)  # may raise SpendLimit

    async def complete_json(self, system: str, user: str, schema_model: type[T], tier: str = "fast", *,
                            max_tokens: int = 4000, effort: str = "low") -> T:
        schema = schema_model.model_json_schema()
        while True:
            if self.stopped is not None:
                raise self.stopped
            name = self.active
            async with self.sem:
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
                try:
                    raw = await self.provider(name).complete(system, user, schema, tier, max_tokens, effort)
                except ProviderUnavailable as e:
                    if name == "claude" and self.fallback:
                        if not self.switched:
                            self.switched = True
                            log.warning("Claude is unusable (%s); switching the rest of the job to Gemini", e.reason)
                        self.active = "gemini"
                        continue
                    raise RuntimeError(f"The model provider is unavailable ({e.reason}).") from e
                finally:
                    self.in_flight -= 1
            try:
                self._record(name, tier, raw)
            except SpendLimit as e:
                self.stopped = e
                raise
            try:
                return schema_model.model_validate_json(raw.text)
            except ValueError as e:
                raise RuntimeError(f"{raw.model} returned invalid JSON") from e

    async def __call__(self, model: str, system: str, user: str, schema_model: type[T], *,
                       max_tokens: int = 4000, effort: str = "low") -> T:
        """The `call` convention of the segmentation/cast code: `model` is a tier name ("fast"/"smart")."""
        return await self.complete_json(system, user, schema_model, model, max_tokens=max_tokens, effort=effort)


def effective_provider(provider: str | None = None) -> str:
    """The provider that will really run: `provider` (default settings.upload_llm), or Gemini when Claude has no key."""
    name = provider or settings.upload_llm
    if name == "claude" and not settings.anthropic_api_key:
        return "gemini"
    return name


def make_llm(provider: str | None = None, **kw) -> LLM:
    """LLM for `provider` (default settings.upload_llm). Claude without an API key starts on Gemini."""
    name = effective_provider(provider)
    if name != (provider or settings.upload_llm):
        log.warning("ANTHROPIC_API_KEY is not set; using Gemini")
    return LLM(name, **kw)
