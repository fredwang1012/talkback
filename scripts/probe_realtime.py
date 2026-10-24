"""Probes (d) and (e): Realtime manual-turn flow, latency, per-response instructions, mid-session voice change.

TTS is requested as raw 24 kHz PCM16 so it can be fed straight in as 'mic' audio (no resampling needed).
"""
import asyncio
import base64
import json
import time
import wave

import websockets

from _common import BosonClient, CAPTURES, flush_notes, note, strip_audio

URL = "wss://api.boson.ai/v1/realtime"
QUESTION = "Holmes, what do you make of this visitor who came to your door tonight?"
CHUNK = 24000 * 2 // 10  # 100 ms of PCM16 @ 24 kHz


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self.audio = bytearray()
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for msg in self.ws:
                e = json.loads(msg)
                e["_t"] = time.monotonic()
                self.events.append(e)
                if e["type"] == "response.output_audio.delta":
                    self.audio += base64.b64decode(e["delta"])
                await self.q.put(e)
        except Exception as ex:  # closed
            await self.q.put({"type": "_closed", "error": repr(ex), "_t": time.monotonic()})

    async def send(self, **evt):
        await self.ws.send(json.dumps(evt))

    async def wait(self, type_: str, timeout=30):
        async def go():
            while True:
                e = await self.q.get()
                if e["type"] == type_ or e["type"] in ("error", "_closed"):
                    return e

        return await asyncio.wait_for(go(), timeout)


def save_wav(name: str, pcm: bytes):
    with wave.open(str(CAPTURES / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(bytes(pcm))


async def speak(s: Session, pcm: bytes) -> float:
    for i in range(0, len(pcm), CHUNK):
        await s.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[i:i + CHUNK]).decode())
    t0 = time.monotonic()
    await s.send(type="input_audio_buffer.commit")
    return t0


async def main() -> None:
    boson = BosonClient()
    pcm = (await boson.create_speech(QUESTION, "chloe", response_format="pcm")).content
    note(f"question audio: {len(pcm) / 48000:.1f} s of PCM16 @24k")
    secret = (await boson.mint_client_secret(300))["value"]

    async with websockets.connect(URL, subprotocols=["realtime", f"bai-client-secret.{secret}"]) as ws:
        s = Session(ws)
        await s.send(type="session.update", session={
            "type": "realtime", "model": "higgs-realtime",
            "instructions": "You are a helpful assistant.",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None,
                          "transcription": {"model": "higgs-stt-3.1"}, "noise_reduction": {"type": "near_field"}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "oliver"},
            },
            "temperature": 0.3,
        })
        created = await s.wait("session.created")
        note(f"first session.update acked with: {created['type']}")

        # (d) manual turn: commit -> transcription latency
        t0 = await speak(s, pcm)
        tr = await s.wait("conversation.item.input_audio_transcription.completed")
        note(f"commit -> transcription.completed: {(tr['_t'] - t0) * 1000:.0f} ms; transcript={tr.get('transcript')!r}")

        # response.create with per-response instructions
        t1 = time.monotonic()
        await s.send(type="response.create", response={
            "instructions": "You are a pirate. Answer in one short sentence, and say 'Arrr' at least once.",
            "max_output_tokens": 200})
        first = None
        while True:
            e = await asyncio.wait_for(s.q.get(), 30)
            if e["type"] == "response.output_audio.delta" and first is None:
                first = e["_t"] - t1
            if e["type"] in ("response.done", "error", "_closed"):
                break
        note(f"response.create -> first audio delta: {first * 1000:.0f} ms" if first else "no audio delta!")
        tdone = [x for x in s.events if x["type"] == "response.output_audio_transcript.done"]
        note(f"answer transcript (should be pirate-y): {tdone[-1]['transcript']!r}" if tdone else "no transcript.done")
        save_wav("realtime_answer_oliver.wav", s.audio)

        # commit while a response is active?
        await s.send(type="response.create", response={"instructions": "Count slowly from one to twenty in words."})
        await s.wait("response.created")
        await s.send(type="input_audio_buffer.commit")
        err = await s.wait("error", timeout=10)
        note(f"commit during active response -> {err['type']}: {json.dumps(err.get('error', err))[:200]}")
        n0 = len(s.events)
        await s.send(type="response.cancel")
        try:
            done = await s.wait("response.done", timeout=10)
            note(f"response.cancel -> {done['type']} status={done.get('response', {}).get('status')}")
        except TimeoutError:
            seen = sorted({e["type"] for e in s.events[n0:]})
            note(f"response.cancel -> no response.done within 10 s; events after cancel: {seen}")
        # let any in-flight response finish before the next test
        while s.q.qsize():
            s.q.get_nowait()

        # (e) mid-session voice change
        s.audio.clear()
        await s.send(type="session.update", session={"audio": {"output": {"voice": "nora"}}})
        upd = await s.wait("session.updated")
        note(f"voice change -> {upd['type']}" + (f": {json.dumps(upd.get('error'))[:200]}" if upd["type"] == "error" else ""))
        if upd["type"] == "session.updated":
            await s.send(type="conversation.item.create", item={
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Say hello in one short sentence."}]})
            await s.send(type="response.create")
            await s.wait("response.done")
            save_wav("realtime_answer_nora.wav", s.audio)
            note("saved realtime_answer_oliver.wav and realtime_answer_nora.wav: LISTEN to confirm the voice changed")

        (CAPTURES / "realtime_events.json").write_text(
            json.dumps([strip_audio({k: v for k, v in e.items() if k != "_t"}) for e in s.events], indent=1),
            encoding="utf-8")
    flush_notes("Realtime manual turns (d, e)")


asyncio.run(main())
