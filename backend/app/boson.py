"""Thin async client for the Boson API with error handling per brief section 3.1."""
import asyncio
import random
from typing import Any

import httpx

from .config import settings

TTS_SEMAPHORE = asyncio.Semaphore(4)
MAX_RETRIES = 4


class BosonError(Exception):
    def __init__(self, status: int, type_: str, message: str, code: str | None = None):
        super().__init__(f"{status} {type_}: {message}")
        self.status, self.type, self.message, self.code = status, type_, message, code

    @property
    def transient(self) -> bool:
        return self.type == "rate_limit_error" or self.status >= 500


def _to_error(resp: httpx.Response) -> BosonError:
    try:
        err = resp.json().get("error", {})
    except Exception:
        err = {}
    return BosonError(
        resp.status_code,
        err.get("type") or "api_error",
        err.get("message") or resp.text[:200],
        err.get("code"),
    )


class BosonClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.boson_base_url,
            headers={"Authorization": f"Bearer {settings.require_boson_key()}"},
            timeout=60,
            transport=self._transport,
        )

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Retries only transient errors; insufficient_quota (also 429) is never retried."""
        for attempt in range(MAX_RETRIES + 1):
            async with self._client() as c:
                resp = await c.request(method, path, **kw)
            if resp.is_success:
                return resp
            err = _to_error(resp)
            if not err.transient or attempt == MAX_RETRIES:
                raise err
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(30, 2**attempt) * random.uniform(0.5, 1.5)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def create_speech(self, input: str, voice: str = "default", *, timestamps: bool = False,
                            response_format: str = "mp3", **extra: Any) -> httpx.Response:
        body = {"model": "higgs-tts-3", "input": input, "voice": voice,
                "response_format": response_format, "stream": False, **extra}
        if timestamps:
            body["timestamps"] = True
        async with TTS_SEMAPHORE:
            return await self.request("POST", "/v1/audio/speech", json=body)

    async def create_voice(self, ref_audio_b64: str, ref_text: str, description: str | None = None) -> str:
        body: dict[str, Any] = {"ref_audio": ref_audio_b64, "ref_text": ref_text}
        if description:
            body["description"] = description
        data = (await self.request("POST", "/v1/audio/voices", json=body)).json()
        return data.get("voice") or data["voice_id"]  # schema says `voice`, guide says `voice_id`

    async def mint_client_secret(self, ttl: int = 600) -> dict[str, Any]:
        data = (await self.request("POST", "/v1/realtime/client_secrets",
                                   json={"expires_after": {"seconds": ttl}})).json()
        return {"value": data["value"], "expires_at": data["expires_at"]}  # drop session object
