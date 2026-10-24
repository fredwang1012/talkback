"""Probe 5: the fix for tool continuations. Text mode, the question in response.input, a jump_to call, then
function_call_output + response.create whose instructions repeat the result (TalkClient.withToolResults): does the
model now confirm what the tool did?"""
import asyncio
import time

from _common import BosonClient, note
from probe_realtime_text import ALICE, JUMP_TOOL, connect, text_of
from app.personas import ANSWER_SUFFIX  # noqa: E402

INSTR = ALICE + ANSWER_SUFFIX


def with_tool_results(instructions: str, results: list[tuple[str, str]]) -> str:
    lines = "\n".join(f"- {name}: {out}" for name, out in results)
    return (f"{instructions}\n\nYou just acted for the reader. What each tool did:\n{lines}\n"
            "Tell the reader what happened in one short spoken sentence, in character, and do not call the tool again.")


async def main():
    boson = BosonClient()
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto", "audio": {"output": {"voice": "chloe"}}})
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": INSTR, "max_output_tokens": 200,
                                                  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Please jump to chapter two now."}]}]})
    done = await s.wait("response.done", timeout=40)
    calls = [o for o in done.get("response", {}).get("output", []) if o.get("type") == "function_call"]
    note(f"call: {[(c.get('name'), c.get('arguments')) for c in calls]} preamble={text_of(s.since(i))!r} ({done['_t'] - t0:.2f} s)")
    if not calls:
        return
    await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
    await s.wait("conversation.item.added", timeout=10)
    i, t0 = len(s.events), time.monotonic()
    await s.send(type="response.create", response={"instructions": with_tool_results(INSTR, [("jump_to", "Moved to Chapter Two: The Pool of Tears.")]), "max_output_tokens": 200})
    done = await s.wait("response.done", timeout=40)
    again = [o for o in done.get("response", {}).get("output", []) if o.get("type") == "function_call"]
    note(f"continuation -> {text_of(s.since(i))!r} calls_again={len(again)} ({done['_t'] - t0:.2f} s)")
    await s.ws.close()


if __name__ == "__main__":
    asyncio.run(main())
