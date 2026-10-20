"""Thin async client for the Boson API with error handling per brief section 3.1."""
import asyncio
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from .config import settings
from .gcp import usage

TTS_SEMAPHORE = asyncio.Semaphore(4)
MAX_RETRIES = 4


class BosonError(Exception):
    def __init__(self, status: int, type_: str, message: str, code: str | None = None):
        super().__init__(f"{status} {type_}: {message}")
        self.status, self.type, self.message, self.code = status, type_, message, code

    @property
    def transient(self) -> bool:
        return self.type == "rate_limit_error" or self.status >= 500 or self.code == "all_replicas_busy"


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
        usage.record("boson_voice_clone", "boson-voice-clone", clones=1)  # count only, unpriced
        return data.get("voice") or data["voice_id"]  # schema says `voice`, guide says `voice_id`

    # ---- Higgs Avatar (stretch). Request/response shapes follow the docs; verify with scripts/probe_avatar.py. ----

    async def create_video(self, ref_image_b64: str, audio_b64: str, *, size: str = "640x640",
                           image_mime: str = "image/png", audio_mime: str = "audio/mpeg",
                           seconds: float | None = None) -> str:
        """Start an async avatar render from a portrait and a speech clip (at most 60 s). Returns the video id."""
        body = {"model": "higgs-avatar", "size": size,
                "ref_image": f"data:{image_mime};base64,{ref_image_b64}",
                "input": f"data:{audio_mime};base64,{audio_b64}"}
        vid = (await self.request("POST", "/v1/videos", json=body)).json()["id"]
        usage.record("boson_avatar", "higgs-avatar", **({"seconds": seconds} if seconds is not None else {}))
        return vid

    @asynccontextmanager
    async def stream_video(self, ref_image_b64: str, audio_b64: str, *, size: str = "640x640",
                           image_mime: str = "image/png", audio_mime: str = "audio/mpeg",
                           seconds: float | None = None) -> AsyncIterator[tuple[str | None, AsyncIterator[bytes]]]:
        """POST /v1/videos/stream: yields (video id, fMP4 chunk iterator) once the 200 headers arrive.

        Transient failures (429, all_replicas_busy, 5xx) before any byte is sent are retried like request().
        """
        body = {"model": "higgs-avatar", "size": size,
                "ref_image": f"data:{image_mime};base64,{ref_image_b64}",
                "input": f"data:{audio_mime};base64,{audio_b64}"}
        for attempt in range(MAX_RETRIES + 1):
            client = self._client()
            client.timeout = httpx.Timeout(300, connect=15)
            async with client:
                async with client.stream("POST", "/v1/videos/stream", json=body) as resp:
                    if resp.is_success:
                        usage.record("boson_avatar", "higgs-avatar", **({"seconds": seconds} if seconds is not None else {}))
                        yield resp.headers.get("X-Video-Id"), resp.aiter_bytes()
                        return
                    await resp.aread()
                    err = _to_error(resp)
                    retry_after = resp.headers.get("Retry-After")
            if not err.transient or attempt == MAX_RETRIES:
                raise err
            delay = float(retry_after) if retry_after else min(30, 2**attempt) * random.uniform(0.5, 1.5)
            await asyncio.sleep(delay)

    async def get_video(self, video_id: str) -> dict[str, Any]:
        return (await self.request("GET", f"/v1/videos/{video_id}")).json()

    async def wait_video(self, video_id: str, *, timeout: float = 300, interval: float = 3) -> dict[str, Any]:
        """Poll until the video is completed or failed; raises on failure or timeout."""
        waited = 0.0
        while True:
            info = await self.get_video(video_id)
            if info.get("status") == "completed":
                return info
            if info.get("status") == "failed":
                raise BosonError(500, "api_error", f"avatar render failed: {info.get('error') or info}")
            if waited >= timeout:
                raise BosonError(504, "api_error", f"avatar render {video_id} not finished after {timeout:.0f}s")
            await asyncio.sleep(interval)
            waited += interval

    async def download_video(self, video_id: str) -> bytes:
        return (await self.request("GET", f"/v1/videos/{video_id}/content")).content

    async def mint_client_secret(self, ttl: int = 600) -> dict[str, Any]:
        data = (await self.request("POST", "/v1/realtime/client_secrets",
                                   json={"expires_after": {"seconds": ttl}})).json()
        return {"value": data["value"], "expires_at": data["expires_at"]}  # drop session object
