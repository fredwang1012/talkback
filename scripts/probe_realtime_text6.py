"""Probe 6: the tool continuation needs a `response.input` too (probe 5: instructions alone are not acted on).
(a) a system input message with the tool results; (b) a user input message with them. Which confirms the action?"""
import asyncio
import time

from _common import BosonClient, note
from probe_realtime_text import ALICE, JUMP_TOOL, connect, text_of
from app.personas import ANSWER_SUFFIX  # noqa: E402

INSTR = ALICE + ANSWER_SUFFIX
RESULTS = ("You just acted for the reader, who said: \"Please jump to chapter two now.\" What each tool did:\n"
           "- jump_to: Moved to Chapter Two: The Pool of Tears.\n"
           "Tell the reader what happened in one short spoken sentence, in character. Do not call the tool again.")


async def one(s, role: str):
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": INSTR, "max_output_tokens": 200,
                                                  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Please jump to chapter two now."}]}]})
    done = await s.wait("response.done", timeout=40)
    calls = [o for o in done.get("response", {}).get("output", []) if o.get("type") == "function_call"]
    note(f"[{role}] call: {[(c.get('name'), c.get('arguments')) for c in calls]} preamble={text_of(s.since(i))!r}")
    if not calls:
        return
    await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
    await s.wait("conversation.item.added", timeout=10)
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": INSTR, "max_output_tokens": 200,
                                                  "input": [{"type": "message", "role": role, "content": [{"type": "input_text", "text": RESULTS}]}]})
    done = await s.wait("response.done", timeout=40)
    if done["type"] != "response.done":
        note(f"[{role}] !! {str(done)[:300]}")
        return
    again = [o for o in done.get("response", {}).get("output", []) if o.get("type") == "function_call"]
    note(f"[{role}] continuation -> {text_of(s.since(i))!r} calls_again={len(again)} ({done['_t'] - t0:.2f} s)")


async def main():
    boson = BosonClient()
    for role in ("system", "user"):
        s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto", "audio": {"output": {"voice": "chloe"}}})
        await one(s, role)
        await s.ws.close()


if __name__ == "__main__":
    asyncio.run(main())
