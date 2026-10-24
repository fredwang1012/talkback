"""Probe (f): create a custom voice with `description`, check the id field and idempotency, then delete it."""
import asyncio
import base64

import httpx

from _common import BosonClient, BosonError, flush_notes, note, settings

LINE = ("Well, it is a rather long story, but I have to tell you that I was walking home late one evening "
        "when I noticed a light burning in the window of the old house.")


async def main() -> None:
    c = BosonClient()
    audio = (await c.create_speech(LINE, "nora", response_format="wav")).content
    b64 = base64.b64encode(audio).decode()
    try:
        r = await c.request("POST", "/v1/audio/voices", json={"ref_audio": b64, "ref_text": LINE, "description": "probe voice"})
        j = r.json()
        note(f"create voice response keys: {sorted(j)}")
        vid = j.get("voice") or j.get("voice_id")
        note(f"id field: {'voice' if 'voice' in j else 'voice_id' if 'voice_id' in j else '??'} = {vid}")
        vid2 = await c.create_voice(b64, LINE, "probe voice")
        note(f"idempotent (same id on re-create): {vid == vid2}")
        r = await c.create_speech("Hello from a cloned voice.", vid)
        note(f"speech with cloned voice: {len(r.content)} bytes")
        async with httpx.AsyncClient(base_url=settings.boson_base_url,
                                     headers={"Authorization": f"Bearer {settings.boson_api_key}"}) as h:
            d = await h.delete(f"/v1/audio/voices/{vid}")
            note(f"delete voice: {d.status_code} {d.text[:120]}")
    except BosonError as e:
        note(f"voice create FAILED: {e} (code={e.code})")
    flush_notes("custom voice (f)")


asyncio.run(main())
