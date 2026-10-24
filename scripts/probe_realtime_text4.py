"""Probe 4: does a per-response `instructions` hide the conversation? (1) text item + instructions with no notes;
(2) seeded history + response.input question about it; (3) tool call via response.input, then function_call_output +
response.create {instructions}: does the continuation see the result?"""
import asyncio
import time

from _common import BosonClient, note
from probe_realtime_text import ALICE, JUMP_TOOL, connect, text_of
from app.personas import ANSWER_SUFFIX  # noqa: E402

PLAIN = "You are a reading companion. Answer the reader's latest message in one short spoken sentence."


async def run(s, body: dict):
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response=body)
    done = await s.wait("response.done", timeout=40)
    out = done.get("response", {}).get("output", []) if done["type"] == "response.done" else []
    calls = [o for o in out if o.get("type") == "function_call"]
    return text_of(s.since(i)), calls, done["_t"] - t0


async def main():
    boson = BosonClient()
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto", "audio": {"output": {"voice": "chloe"}}})
    note("(1) text item + instructions without notes")
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What does Alice drink?"}]})
    await s.wait("conversation.item.added", timeout=10)
    text, calls, dt = await run(s, {"instructions": PLAIN, "max_output_tokens": 200})
    note(f"  -> {text!r} ({dt:.2f} s)")
    note("(2) seeded history, then response.input asks about it")
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "By the way, my dog is called Biscuit."}]})
    await s.wait("conversation.item.added", timeout=10)
    await s.send(type="conversation.item.create", item={"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Biscuit, what a lovely name."}]})
    await s.wait("conversation.item.added", timeout=10)
    text, calls, dt = await run(s, {"instructions": PLAIN, "max_output_tokens": 200,
                                    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "What is my dog called?"}]}]})
    note(f"  -> {text!r} ({dt:.2f} s)")
    note("(3) tool via response.input, then function_call_output + response.create {instructions}")
    text, calls, dt = await run(s, {"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 200,
                                    "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Please jump to chapter two now."}]}]})
    note(f"  call: {[(c.get('name'), c.get('arguments')) for c in calls]} text={text!r}")
    if calls:
        await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
        await s.wait("conversation.item.added", timeout=10)
        text, calls, dt = await run(s, {"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 200})
        note(f"  continuation -> {text!r} calls={len(calls)} ({dt:.2f} s)")
    await s.ws.close()


if __name__ == "__main__":
    asyncio.run(main())
