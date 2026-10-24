"""Probes (a) and (b): TTS presets, tags, timestamps."""
import asyncio
import base64

from _common import BosonClient, BosonError, CAPTURES, flush_notes, note

VOICES = ["chloe", "eleanor", "jake", "marcus", "nora", "oliver", "berlinda", "default"]
SENTENCE = "To Harry Quill the lantern road is always the longest."


async def main() -> None:
    c = BosonClient()
    # (a1) plain preset
    r = await c.create_speech(SENTENCE, "marcus")
    (CAPTURES / "tts_plain.mp3").write_bytes(r.content)
    note(f"plain marcus: {len(r.content)} bytes, content-type {r.headers.get('content-type')}")

    # (a2) tags + timestamps: do tag tokens show up in the word list?
    tagged = "<|emotion:contemplation|><|prosody:pitch_low|>" + SENTENCE
    r = await c.create_speech(tagged, "oliver", timestamps=True)
    j = r.json()
    (CAPTURES / "tts_tagged.mp3").write_bytes(base64.b64decode(j["audio"]))
    words = j["timestamps"]
    note(f"tagged+timestamps: keys={sorted(j)}; input echoed={j['input']!r}")
    note(f"timestamp words: {[w['word'] for w in words] if words else words!r}")
    note(f"tag tokens appear in timestamps: {any('<|' in w['word'] or 'emotion' in w['word'] for w in (words or []))}")

    # (a3) numbers with timestamps (normalization is disabled)
    r = await c.create_speech("It was the twentieth of March, 1888, at Mr. Quill's rooms.", "marcus", timestamps=True)
    note(f"number text timestamps: {[w['word'] for w in (r.json()['timestamps'] or [])]}")

    # (a4) null vs [] for tiny / non-English input
    for label, text in [("one word", "Hi"), ("french", "Bonjour, comment allez-vous aujourd'hui?"),
                        ("mandarin", "你好，今天过得怎么样？")]:
        j = (await c.create_speech(text, "marcus", timestamps=True)).json()
        ts = j["timestamps"]
        note(f"timestamps for {label}: {'null' if ts is None else f'{len(ts)} entries'}")

    # (b) which preset names work
    for v in VOICES:
        try:
            r = await c.create_speech("Testing one two three.", v)
            note(f"voice {v}: OK ({len(r.content)} bytes)")
        except BosonError as e:
            note(f"voice {v}: FAILED {e}")
    flush_notes("TTS (a, b)")


asyncio.run(main())
