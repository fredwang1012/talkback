"""Probe: Realtime answers in TEXT mode (B1) and the server-side web_search tool (B2).

Three short sessions with text items (no mic audio), about $0.02 in total:
  A. output_modalities ["text"] + a client function tool: does the model call the tool in text mode, and does it
     follow the "[feeling]" marker rule? Time to the first sentence, then TTS of that sentence = time to first audio.
  B. output_modalities ["audio"], the same question: time to the first output_audio.delta (the baseline).
  C. tools [{"type": "web_search"}] in text mode: the event order around a server-run search, and what the
     function_call_output item looks like (the source-link parser is written from this capture).
Also checks that `tools` can be changed with a mid-session session.update (per-turn web_search on/off).

Run from the repo root with BOSON_API_KEY in the environment (never printed):
  backend/.venv/Scripts/python.exe scripts/probe_realtime_text.py
"""
import asyncio
import json
import sys
import time

import websockets

from _common import BosonClient, CAPTURES, note, strip_audio

sys.path.insert(0, str(CAPTURES.parents[1] / "backend"))
from app.personas import ANSWER_SUFFIX, WEB_SEARCH_RULE  # noqa: E402

URL = "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
SESSION_INSTRUCTIONS = (
    "You are a spoken reading companion. Follow the instructions given with each response. Speak naturally; no markdown. "
    "When the reader asks you to act on the book (move, read, repeat, find a topic, pause, speed, recap, bookmark), call the tool, then confirm in one short sentence.")
JUMP_TOOL = {
    "type": "function", "name": "jump_to",
    "description": "Move the reader to a chapter (by number or title).",
    "parameters": {"type": "object", "properties": {"chapter": {"type": "string", "description": "Chapter number"}}},
}
ALICE = ("You are the Narrator of Alice's Adventures in Wonderland. Answer in two short spoken sentences from these notes only.\n\n"
         "Notes: Alice is a curious girl who follows a White Rabbit down a rabbit hole and finds a hall of locked doors. "
         "She drinks from a bottle labelled DRINK ME and shrinks.")
TUTOR = ("You are a warm tutor for a textbook on information retrieval. Answer in at most three short spoken sentences. "
         "Notes: the chapter introduces the inverted index and tf-idf weighting.")


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

    async def wait(self, *types: str, timeout=40):
        async def go():
            while True:
                e = await self.q.get()
                if e["type"] in types or e["type"] in ("error", "_closed"):
                    return e
        return await asyncio.wait_for(go(), timeout)

    def since(self, i: int):
        return self.events[i:]


def text_of(events):
    return "".join(e.get("delta", "") for e in events if e["type"] == "response.output_text.delta")


def summary(events):
    return [e["type"] + (f"({e['item'].get('type')},{e['item'].get('name')},{e['item'].get('executor')})" if "item" in e and isinstance(e["item"], dict) else "")
            for e in events if e["type"] not in ("response.output_text.delta", "response.output_audio.delta",
                                                 "response.output_audio_transcript.delta", "response.function_call_arguments.delta")]


async def connect(boson: BosonClient, session: dict):
    secret = (await boson.mint_client_secret(300))["value"]
    ws = await websockets.connect(URL, subprotocols=["realtime", f"bai-client-secret.{secret}"])
    s = Session(ws)
    await s.send(type="session.update", session={"type": "realtime", "model": "higgs-realtime", "instructions": SESSION_INSTRUCTIONS,
                                                  "temperature": 0.3, **session})
    ack = await s.wait("session.created", "session.updated")
    note(f"  ack {ack['type']}: output_modalities={ack.get('session', {}).get('output_modalities')} tools={[t.get('type') + ':' + str(t.get('name')) for t in ack.get('session', {}).get('tools', [])]}")
    return s


async def probe_a(boson: BosonClient):
    note("A. text mode + function tool + marker rule")
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto",
                              "audio": {"output": {"voice": "chloe"}}})
    # turn 1: a command -> expect a function call (text mode)
    i = len(s.events)
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Jump to chapter two."}]})
    await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
    done = await s.wait("response.done")
    out = done.get("response", {}).get("output", [])
    calls = [o for o in out if o.get("type") == "function_call"]
    note(f"  turn 1 events: {summary(s.since(i))}")
    note(f"  turn 1 text: {text_of(s.since(i))!r}")
    note(f"  turn 1 function calls: {[(c.get('name'), c.get('arguments'), c.get('executor')) for c in calls]}")
    if calls:
        i = len(s.events)
        await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
        await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
        await s.wait("response.done")
        note(f"  turn 1 continuation text: {text_of(s.since(i))!r}")
    # turn 2: a question -> text with markers; time to first sentence
    i = len(s.events)
    t0 = time.monotonic()
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Who is Alice and what does she drink?"}]})
    await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
    first_delta = None
    first_sentence_t = None
    buf = ""
    while True:
        e = await s.wait("response.output_text.delta", "response.output_text.done", "response.done")
        if e["type"] == "response.output_text.delta":
            if first_delta is None:
                first_delta = e["_t"] - t0
            buf += e["delta"]
            if first_sentence_t is None and any(p in buf for p in (". ", "! ", "? ")):
                first_sentence_t = e["_t"] - t0
        elif e["type"] in ("response.done", "error", "_closed"):
            break
    t_done = time.monotonic() - t0
    text = text_of(s.since(i))
    note(f"  turn 2 text: {text!r}")
    note(f"  first text delta {first_delta and round(first_delta, 2)} s, first sentence complete {first_sentence_t and round(first_sentence_t, 2)} s, text done {t_done:.2f} s")
    first = text.split(". ")[0] + "."
    t1 = time.monotonic()
    r = await boson.create_speech(first[:300], "chloe", response_format="mp3")
    tts = time.monotonic() - t1
    note(f"  TTS of the first sentence ({len(first)} chars, {len(r.content)} bytes mp3): {tts:.2f} s")
    if first_sentence_t is not None:
        note(f"  => text path, time to first audio about {first_sentence_t + tts:.2f} s (first sentence + TTS; lip-sync adds its own wait)")
    await s.ws.close()
    return (first_sentence_t or 0) + tts


async def probe_b(boson: BosonClient):
    note("B. audio mode baseline")
    s = await connect(boson, {"output_modalities": ["audio"], "tools": [JUMP_TOOL],
                              "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "chloe"}}})
    t0 = time.monotonic()
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Who is Alice and what does she drink?"}]})
    await s.send(type="response.create", response={"instructions": ALICE, "max_output_tokens": 300})
    e = await s.wait("response.output_audio.delta")
    first_audio = e["_t"] - t0
    done = await s.wait("response.done")
    note(f"  first output_audio.delta {first_audio:.2f} s, response.done {done['_t'] - t0:.2f} s")
    await s.ws.close()
    return first_audio


async def probe_c(boson: BosonClient):
    note("C. web_search (server tool) in text mode")
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "audio": {"output": {"voice": "chloe"}}})
    # mid-session tools change (per-turn web_search): accepted?
    await s.send(type="session.update", session={"tools": [JUMP_TOOL, {"type": "web_search"}]})
    ack = await s.wait("session.updated")
    note(f"  tools changed mid-session: {ack['type']} tools={[t.get('type') for t in ack.get('session', {}).get('tools', [])]}" + (f" error={ack.get('error')}" if ack["type"] == "error" else ""))
    i = len(s.events)
    t0 = time.monotonic()
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Who first proposed tf-idf weighting and in what year? Please look it up on the web."}]})
    await s.send(type="response.create", response={"instructions": TUTOR + WEB_SEARCH_RULE + ANSWER_SUFFIX, "max_output_tokens": 400})
    responses_created = 0
    while True:
        e = await s.wait("response.created", "response.done", "conversation.item.added", timeout=60)
        if e["type"] == "response.created":
            responses_created += 1
            note(f"  response.created #{responses_created} at {e['_t'] - t0:.2f} s")
        elif e["type"] == "conversation.item.added":
            it = e.get("item", {})
            note(f"  item.added {it.get('type')} name={it.get('name')} executor={it.get('executor')} at {e['_t'] - t0:.2f} s")
            if it.get("type") == "function_call_output":
                note(f"  function_call_output.output ({len(str(it.get('output')))} chars):\n{str(it.get('output'))[:1500]}")
        elif e["type"] == "response.done":
            out = e.get("response", {}).get("output", [])
            kinds = [(o.get("type"), o.get("name"), o.get("executor"), o.get("status")) for o in out]
            note(f"  response.done #{responses_created} status={e.get('response', {}).get('status')} output={kinds} at {e['_t'] - t0:.2f} s")
            if not any(o.get("type") == "function_call" and o.get("executor") == "server" for o in out):
                break
        else:
            note(f"  {e['type']}: {json.dumps(strip_audio(e))[:400]}")
            break
    note(f"  all text: {text_of(s.since(i))!r}")
    note(f"  event order: {summary(s.since(i))}")
    (CAPTURES / "realtime_text_websearch.json").write_text(json.dumps([strip_audio(e) for e in s.since(i)], indent=1), encoding="utf-8")
    await s.ws.close()


async def main():
    boson = BosonClient()
    text_first = await probe_a(boson)
    audio_first = await probe_b(boson)
    await probe_c(boson)
    note(f"Latency to first audio: text path about {text_first:.2f} s vs direct audio {audio_first:.2f} s")


if __name__ == "__main__":
    asyncio.run(main())
