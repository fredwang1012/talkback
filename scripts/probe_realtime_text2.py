"""Probe 2 (follow-up to probe_realtime_text.py): in text mode the model seemed not to see the user item.
Try: (1) wait for conversation.item.added before response.create; (2) response.input instead of an item;
(3) a clear tool command in text mode vs (4) the same in audio mode; (5) web_search with the ack-first order."""
import asyncio
import json
import time

from _common import BosonClient, CAPTURES, note, strip_audio
from probe_realtime_text import ALICE, JUMP_TOOL, TUTOR, connect, summary, text_of
from app.personas import ANSWER_SUFFIX, WEB_SEARCH_RULE  # noqa: E402


def transcript_of(events):
    return "".join(e.get("delta", "") for e in events if e["type"] == "response.output_audio_transcript.delta")


async def turn(s, text: str, instructions: str, *, ack_first: bool, via_input=False, audio=False, timeout=40):
    i = len(s.events)
    t0 = time.monotonic()
    if via_input:
        await s.send(type="response.create", response={"instructions": instructions, "max_output_tokens": 300,
                                                      "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]})
    else:
        await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
        if ack_first:
            await s.wait("conversation.item.added", timeout=10)
        await s.send(type="response.create", response={"instructions": instructions, "max_output_tokens": 300})
    done = await s.wait("response.done", timeout=timeout)
    out = done.get("response", {}).get("output", []) if done["type"] == "response.done" else []
    calls = [(o.get("name"), o.get("arguments"), o.get("executor")) for o in out if o.get("type") == "function_call"]
    got = transcript_of(s.since(i)) if audio else text_of(s.since(i))
    note(f"  [{'input' if via_input else 'ack-first' if ack_first else 'no-ack'}{' audio' if audio else ''}] {text!r} -> {got!r} calls={calls} ({done['_t'] - t0:.2f} s)")
    if done["type"] != "response.done":
        note(f"    !! {json.dumps(strip_audio(done))[:300]}")
    return calls, out


async def main():
    boson = BosonClient()
    note("probe 2: does the text-mode model see the user item?")
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL], "tool_choice": "auto", "audio": {"output": {"voice": "chloe"}}})
    await turn(s, "What does Alice drink? One sentence.", ALICE + ANSWER_SUFFIX, ack_first=False)
    await turn(s, "What animal does Alice follow? One sentence.", ALICE + ANSWER_SUFFIX, ack_first=True)
    await turn(s, "Where does Alice find herself after the hole? One sentence.", ALICE + ANSWER_SUFFIX, ack_first=False, via_input=True)
    calls, out = await turn(s, "Please jump to chapter two now.", ALICE + ANSWER_SUFFIX, ack_first=True)
    if calls:
        call = next(o for o in out if o.get("type") == "function_call")
        i = len(s.events)
        await s.send(type="conversation.item.create", item={"type": "function_call_output", "call_id": call["call_id"], "output": "Moved to Chapter Two: The Pool of Tears."})
        await s.wait("conversation.item.added", timeout=10)
        await s.send(type="response.create", response={"instructions": ALICE + ANSWER_SUFFIX, "max_output_tokens": 300})
        await s.wait("response.done")
        note(f"  continuation after the tool: {text_of(s.since(i))!r}")
    await s.ws.close()

    note("audio mode, the same tool command")
    s = await connect(boson, {"output_modalities": ["audio"], "tools": [JUMP_TOOL], "tool_choice": "auto",
                              "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "chloe"}}})
    await turn(s, "Please jump to chapter two now.", ALICE, ack_first=True, audio=True)
    await s.ws.close()

    note("web_search, text mode, ack-first")
    s = await connect(boson, {"output_modalities": ["text"], "tools": [JUMP_TOOL, {"type": "web_search"}], "audio": {"output": {"voice": "chloe"}}})
    i = len(s.events)
    t0 = time.monotonic()
    await s.send(type="conversation.item.create", item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Search the web: who first proposed tf-idf weighting, and in what year?"}]})
    await s.wait("conversation.item.added", timeout=10)
    await s.send(type="response.create", response={"instructions": TUTOR + WEB_SEARCH_RULE + ANSWER_SUFFIX, "max_output_tokens": 400})
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
