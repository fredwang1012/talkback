"""Probe: several hands-free turns on semantic_vad with create_response=false and NO response.create in between (the
Gemini answer path only uses Realtime for the transcripts). Does the transcript of turn N contain turns 1..N-1?
And what closes a turn: an assistant item, deleting the user items, or input_audio_buffer.clear?

Cost: a few short TTS clips + ~30 s of input audio (well under a cent). Audio is streamed in real time as the app does.
"""
import asyncio
import base64
import json
import time

import websockets

from _common import BosonClient, CAPTURES, note, strip_audio

URL = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
CHUNK = 24000 * 2 // 10  # 100 ms of PCM16 @ 24 kHz
SILENCE = bytes(CHUNK)
UTTERANCES = [
    "What is Boolean retrieval?",
    "I don't understand anything.",
    "Can you give me an example?",
    "Thanks, that helps a lot.",
    "Goodbye for now.",
]


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self.q: asyncio.Queue = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for msg in self.ws:
                e = json.loads(msg)
                e["_t"] = time.monotonic()
                self.events.append(e)
                await self.q.put(e)
        except Exception as ex:
            await self.q.put({"type": "_closed", "error": repr(ex), "_t": time.monotonic()})

    async def send(self, **evt):
        await self.ws.send(json.dumps(evt))

    async def wait(self, type_: str, timeout=10):
        async def go():
            while True:
                e = await self.q.get()
                if e["type"] == type_ or e["type"] in ("error", "_closed"):
                    return e
        try:
            return await asyncio.wait_for(go(), timeout)
        except TimeoutError:
            return {"type": "_timeout"}


async def stream(s: Session, pcm: bytes, trailing_silence_s: float = 1.5):
    """Real-time: one 100 ms chunk every 100 ms, then silence so the VAD closes the turn."""
    for i in range(0, len(pcm), CHUNK):
        await s.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[i:i + CHUNK]).decode())
        await asyncio.sleep(0.1)
    for _ in range(int(trailing_silence_s * 10)):
        await s.send(type="input_audio_buffer.append", audio=base64.b64encode(SILENCE).decode())
        await asyncio.sleep(0.1)


async def turn(s: Session, label: str, pcm: bytes) -> dict:
    n0 = len(s.events)
    await stream(s, pcm)
    tr = await s.wait("conversation.item.input_audio_transcription.completed", timeout=8)
    seen = [(e["type"], e.get("item_id") or (e.get("item") or {}).get("id")) for e in s.events[n0:]
            if e["type"].startswith(("input_audio_buffer.", "conversation.item"))]
    note(f"{label}: events={seen}")
    note(f"{label}: transcript item={tr.get('item_id')!r} text={tr.get('transcript')!r}" if tr["type"].endswith("completed") else f"{label}: {tr}")
    return tr


async def main() -> None:
    boson = BosonClient()
    chars = sum(len(u) for u in UTTERANCES)
    note(f"estimate: TTS {chars} chars (~${chars / 1000 * 0.015:.4f}) + ~35 s realtime input (~${35 / 60 * 0.005:.4f})")
    clips = [(await boson.create_speech(u, "chloe", response_format="pcm")).content for u in UTTERANCES]
    note(f"clips: {[round(len(c) / 48000, 1) for c in clips]} s")
    secret = (await boson.mint_client_secret(300))["value"]

    async with websockets.connect(URL, subprotocols=["realtime", f"bai-client-secret.{secret}"]) as ws:
        s = Session(ws)
        await s.send(type="session.update", session={
            "type": "realtime", "model": "higgs-realtime",
            "instructions": "You are a spoken reading companion.",
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "turn_detection": {"type": "semantic_vad", "create_response": False, "interrupt_response": False,
                                             "threshold": 0.6, "silence_duration_ms": 500},
                          "transcription": {"model": "higgs-stt-3.1"}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "oliver"},
            },
            "temperature": 0.3,
        })
        created = await s.wait("session.created")
        td = (created.get("session") or {}).get("audio", {}).get("input", {}).get("turn_detection")
        note(f"session.created turn_detection={td}")

        items: list[str] = []
        # A: two turns, nothing in between but a 3 s gap with no audio at all (the mic hold while the character speaks)
        t1 = await turn(s, "A1", clips[0])
        if t1.get("item_id"): items.append(t1["item_id"])
        await asyncio.sleep(3)
        t2 = await turn(s, "A2 (after a 3 s hold, no response in between)", clips[1])
        if t2.get("item_id"): items.append(t2["item_id"])
        # B: an assistant text item closes the turn?
        await s.send(type="conversation.item.create", event_id="asst-1",
                     item={"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Boolean retrieval matches documents by exact term logic."}]})
        ack = await s.wait("conversation.item.added", timeout=5)
        note(f"B: assistant item -> {ack['type']} {json.dumps(ack.get('error', ''))[:160]}")
        t3 = await turn(s, "B3 (after an assistant item)", clips[2])
        if t3.get("item_id"): items.append(t3["item_id"])
        # C: deleting the earlier user items
        for it in items:
            await s.send(type="conversation.item.delete", item_id=it)
        await asyncio.sleep(0.5)
        dels = [e["type"] for e in s.events if e["type"] in ("conversation.item.deleted", "error")][-len(items):]
        note(f"C: deleted {items} -> {dels}")
        t4 = await turn(s, "C4 (after deleting the user items)", clips[3])
        # D: input_audio_buffer.clear
        await s.send(type="input_audio_buffer.clear")
        await s.wait("input_audio_buffer.cleared", timeout=3)
        t5 = await turn(s, "D5 (after input_audio_buffer.clear)", clips[4])
        errors = [json.dumps(e.get("error", e))[:200] for e in s.events if e["type"] == "error"]
        note(f"errors: {errors}")
        (CAPTURES / "realtime_semantic_turns.json").write_text(
            json.dumps([strip_audio({k: v for k, v in e.items() if k != "_t"}) for e in s.events], indent=1),
            encoding="utf-8")


asyncio.run(main())
