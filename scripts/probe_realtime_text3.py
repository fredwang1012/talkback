"""Probe 3: why is a user text item ignored when response.create carries `instructions`?
(a) text item + bare response.create (no instructions); (b) a SPOKEN question (TTS'd PCM, append + commit) +
response.create with instructions (the production path); (c) response.input + a tool command; (d) response.input +
a web search question, text mode. Captures the web_search function_call_output for the source parser."""
import asyncio
import base64
import json
import time

from _common import BosonClient, CAPTURES, note, strip_audio
from probe_realtime_text import ALICE, JUMP_TOOL, TUTOR, connect, summary, text_of
from app.personas import ANSWER_SUFFIX, WEB_SEARCH_RULE  # noqa: E402

CHUNK = 24000 * 2 // 10


async def done_of(s, i, t0, timeout=40):
    done = await s.wait("response.done", timeout=timeout)
    out = done.get("response", {}).get("output", []) if done["type"] == "response.done" else []
    calls = [(o.get("name"), o.get("arguments"), o.get("executor")) for o in out if o.get("type") == "function_call"]
    if done["type"] != "response.done":
        note(f"    !! {json.dumps(strip_audio(done))[:300]}")
    return text_of(s.since(i)), calls, out, done["_t"] - t0


async def main():
    boson = BosonClient()
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto", "audio": {"output": {"voice": "chloe"}}})

    note("(a) text item + bare response.create")
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What does Alice drink? One sentence."}]})
    await s.wait("conversation.item.added", timeout=10)
    await s.send(type="response.create")
    text, calls, _, dt = await done_of(s, i, t0)
    note(f"  -> {text!r} calls={calls} ({dt:.2f} s)")

    note("(b) spoken question (audio item) + response.create with instructions")
    pcm = (await boson.create_speech("What does Alice drink? One sentence please.", "jake", response_format="pcm")).content
    i, t0 = len(s.events), time.monotonic()
    for k in range(0, len(pcm), CHUNK):
        await s.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[k:k + CHUNK]).decode())
    await s.send(type="input_audio_buffer.commit")
    await s.wait("input_audio_buffer.committed", timeout=10)
    await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
    text, calls, _, dt = await done_of(s, i, t0)
    note(f"  -> {text!r} calls={calls} ({dt:.2f} s)")

    note("(b2) spoken tool command (audio item) + response.create with instructions")
    pcm = (await boson.create_speech("Please jump to chapter two now.", "jake", response_format="pcm")).content
    i, t0 = len(s.events), time.monotonic()
    for k in range(0, len(pcm), CHUNK):
        await s.send(type="input_audio_buffer.append", audio=base64.b64encode(pcm[k:k + CHUNK]).decode())
    await s.send(type="input_audio_buffer.commit")
    await s.wait("input_audio_buffer.committed", timeout=10)
    await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
    text, calls, out, dt = await done_of(s, i, t0)
    note(f"  -> {text!r} calls={calls} ({dt:.2f} s)")
    if calls:
        call = next(o for o in out if o.get("type") == "function_call")
        i = len(s.events)
        await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": call["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
        await s.wait("conversation.item.added", timeout=10)
        await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
        text, calls, _, dt = await done_of(s, i, time.monotonic())
        note(f"  continuation -> {text!r} calls={calls}")

    note("(c) response.input + tool command")
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300,
                                                  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Please jump to chapter two now."}]}]})
    text, calls, out, dt = await done_of(s, i, t0)
    note(f"  -> {text!r} calls={calls} ({dt:.2f} s)")
    await s.ws.close()

    note("(d) response.input + web search question, text mode")
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL, {"type": "web_search"}], "audio": {"output": {"voice": "chloe"}}})
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": TUTOR + WEB_SEARCH_RULE + ANSWER_SUFFIX, "max_output_tokens": 400,
                                                  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Search the web: who first proposed tf-idf weighting, and in what year?"}]}]})
    created = 0
    while True:
        e = await s.wait("response.created", "response.done", "conversation.item.added", timeout=60)
        if e["type"] == "response.created":
            created += 1
            note(f"  response.created #{created} at {e['_t'] - t0:.2f} s")
        elif e["type"] == "conversation.item.added":
            it = e.get("item", {})
            note(f"  item.added {it.get('type')} name={it.get('name')} executor={it.get('executor')} at {e['_t'] - t0:.2f} s")
            if it.get("type") == "function_call_output":
                note(f"  function_call_output.output ({len(str(it.get('output')))} chars):\n{str(it.get('output'))[:1500]}")
        elif e["type"] == "response.done":
            out = e.get("response", {}).get("output", [])
            note(f"  response.done #{created} status={e.get('response', {}).get('status')} output={[(o.get('type'), o.get('name'), o.get('executor'), o.get('status')) for o in out]} at {e['_t'] - t0:.2f} s")
            if not any(o.get("type") == "function_call" and o.get("executor") == "server" for o in out):
                break
        else:
            note(f"  {e['type']}: {json.dumps(strip_audio(e))[:400]}")
            break
    note(f"  all text: {text_of(s.since(i))!r}")
    note(f"  event order: {summary(s.since(i))}")
    (CAPTURES / "realtime_text_websearch.json").write_text(json.dumps([strip_audio(e) for e in s.since(i)], indent=1), encoding="utf-8")
    await s.ws.close()


if __name__ == "__main__":
    asyncio.run(main())
