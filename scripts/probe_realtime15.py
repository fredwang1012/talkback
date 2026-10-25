"""Probe (A6): does a Realtime model id accept Talkback's exact session.update, and what does a refusal look like?

Usage: python scripts/probe_realtime15.py [--model higgs-realtime-1.5-preview] [--session-json path] [--no-reply]
The session comes from --session-json (the `session.update` event TalkClient's sessionConfig() builds: semantic_vad with
create_response:false, higgs-stt-3.1, all tools, a voice); without it a smaller built-in copy with one tool is used.
Checks: session.created/updated echo the turn detection we asked for, then (unless --no-reply) one typed question gets
a short spoken answer. Cost: well under a cent. An unknown model id (try --model higgs-realtime-9 --no-reply) is free.
"""
import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
BASIC = {"type": "session.update", "session": {
    "type": "realtime", "instructions": "You are a pirate. Answer in one short sentence.", "output_modalities": ["audio"],
    "tools": [{"type": "function", "name": "jump_to", "description": "Move the reader to a chapter.",
               "parameters": {"type": "object", "properties": {"chapter": {"type": "string"}}}}],
    "tool_choice": "auto",
    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000},
                        "turn_detection": {"type": "semantic_vad", "create_response": False, "interrupt_response": False,
                                           "threshold": 0.6, "silence_duration_ms": 500},
                        "transcription": {"model": "higgs-stt-3.1"}},
              "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "oliver"}},
    "temperature": 0.3}}


def api_key() -> str:
    k = os.environ.get("BOSON_API_KEY")
    if not k:
        k = re.search(r"^BOSON_API_KEY=(.*)$", (ROOT / ".env").read_text(), re.M).group(1).strip().strip("\"'")
    return k


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="higgs-realtime-1.5-preview")
    ap.add_argument("--session-json")
    ap.add_argument("--no-reply", action="store_true")
    a = ap.parse_args()
    cfg = json.loads(Path(a.session_json).read_text()) if a.session_json else BASIC
    cfg["session"]["model"] = a.model
    secret = httpx.post("https://api.boson.ai/v1/realtime/client_secrets", headers={"Authorization": f"Bearer {api_key()}"},
                        json={"expires_after": {"seconds": 120}}, timeout=30).json()["value"]
    url = f"wss://api.boson.ai/v1/realtime?model={a.model}"
    try:
        async with websockets.connect(url, subprotocols=["realtime", f"bai-client-secret.{secret}"]) as ws:
            await ws.send(json.dumps(cfg))
            e = json.loads(await asyncio.wait_for(ws.recv(), 20))
            td = (e.get("session") or {}).get("audio", {}).get("input", {}).get("turn_detection")
            print("first event:", e["type"], "| model:", (e.get("session") or {}).get("model"), "| turn_detection:", td)
            if e["type"] == "error":
                print("REFUSED:", json.dumps(e)[:400])
                return
            if a.no_reply:
                return
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Say hello in five words."}]}}))
            t0 = time.monotonic()
            await ws.send(json.dumps({"type": "response.create", "response": {"max_output_tokens": 60}}))
            first = None
            while True:
                e = json.loads(await asyncio.wait_for(ws.recv(), 30))
                if e["type"] == "response.output_audio.delta" and first is None:
                    first = time.monotonic() - t0
                if e["type"] == "error":
                    print("ERROR:", json.dumps(e)[:400])
                    return
                if e["type"] == "response.done":
                    print(f"response.done: status={e['response'].get('status')} first audio {first and round(first*1000)} ms")
                    return
    except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed) as ex:
        print("REFUSED at the socket:", repr(ex)[:300])


asyncio.run(main())
