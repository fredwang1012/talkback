# Blocked items (updated as work proceeds)

Rule for now: do **not** use the Anthropic key (user instruction, 2026-10-15). Boson key not provided yet.

## Needs BOSON_API_KEY in bookson/.env
- [ ] Run live probes `scripts/probe_{tts,secret,realtime,voice}.py` (M0 acceptance). Settles: timestamps null vs [], tag tokens in timestamps, preset `berlinda`, mid-session voice change, commit-during-response, voice-clone response field.
- [ ] Pre-render all TTS for Sherlock (`prerender_tts.py`, ~365 chunks, ~$0.69).
- [ ] Hear it: real audio in Read mode (M3 acceptance: no gaps > 150 ms, highlight tracks words).
- [ ] Talk loop against the live Realtime API (M4 acceptance, latency numbers).
- [ ] Multilingual Q&A check (M7), voice clone (M8), avatars (M9).

## Needs ANTHROPIC key (paused by user instruction)
- [ ] `cast.py` run -> user reviews casting (written, not run).
- [ ] `segment.py` (speaker + emotion per paragraph; Haiku 5.5) — not written yet.
- [ ] `summaries.py` (story-so-far, prefix-only; Sonnet 5.5) — not written yet.
- [ ] Retrieval `context_blurb` per chunk (Haiku) — index works without blurbs meanwhile.
- [ ] `eval_spoilers.py` (M5 acceptance, 0 leaks).
- [ ] Textbook tutor emotion picks (optional).

## Needs the user
- [ ] Review cast after `cast.py`.
- [ ] Portraits (original art): Holmes, Watson, King, Irene Adler, Biology tutor.
- [ ] OpenStax Biology 2e ch. 8 text in `data/raw/biology2e_ch8.txt` if fetch is blocked.
- [ ] Voice sample + transcript for voice lab test (M8).
- [ ] Set a monthly spend cap in the Boson workspace.

## Done without any key (tested with fakes/mocks)
- [x] Talk prompt templates (`personas.py`), spoiler-safe hybrid retrieval + invariant tests (property-based), `/api/retrieve` with server-side character gate.
- [x] Retrieval index built for Sherlock: 129 chunks + multilingual-e5-small embeddings (no blurbs yet). Mandarin/Spanish/French queries retrieve relevant earlier passages.
- [x] `/api/tts` with disk cache + timestamp->character alignment (mocked Boson), `/api/realtime/token` (fresh key, rate limited), `/api/voices/clone` consent gate.
- [x] `ReaderEngine` (prefetch 3, seek cancellation, pitch-preserving speed, word lookup) wired into the Reader; used when the server has a Boson key, silent preview otherwise.
- [x] `TalkController` state machine (15 tests incl. 10 consecutive cycles). Not yet attached to a real socket.

## Still to build without a key
- [ ] `TalkClient` (real WebSocket adapter for manual turns, reuses tutorial audio code) + wire Talk mode/PTT into the Stage UI. Live behaviour (voice change, commit during response) must be confirmed with the probes.
- [ ] Voice lab UI (consent checkbox, record/upload, transcript).
- [ ] Textbook ingest (OpenStax Biology 2e ch. 8): try fetching.
- [ ] README with setup + 90-second demo script; demo mode (`?demo=1`).
- [ ] Debug drawer + cost guard in the UI.
