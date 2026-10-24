# What is left (updated 2026-10-15)

Everything that can be built without a key is built and tested (105 backend tests, 97 frontend tests, types clean, build OK).
What remains needs a key or a file from you. When they arrive: `python scripts/run_all.py --plan`, then run it.

Standing rule: do **not** use the Anthropic key until told (user instruction, 2026-10-15).

## 1. You provide
- [ ] `BOSON_API_KEY` in `talkback/.env` (unblocks section 3).
- [ ] OK to spend Anthropic credit (unblocks section 2; estimated a few dollars of the $100).
- [ ] Set a monthly spend cap in the Boson workspace.
- [ ] Review the cast after `cast.py` runs (voices, personas, `first_para`). `run_all.py --anthropic` pauses for this.
- [ ] OpenStax Biology 2e ch. 8 text pasted into `data/raw/biology2e_ch8.txt` (the fetch tool refused to return it verbatim). The meta file and pipeline are ready.
- [ ] Original-art portraits (Holmes, Watson/narrator, the King, Irene Adler, a tutor): drop into `data/portraits/scandal-in-bohemia/<character-id>.png` (also `narrator`, `cover`). Without them the UI shows initials.
- [ ] A 5-30 s recording of your voice plus its exact transcript, for the voice-clone test.

## 2. Needs the Anthropic key (all written and tested with a fake LLM; none has made a real call)
`python scripts/run_all.py --anthropic` runs these in order:
- [ ] `cast.py` (Sonnet 5.5, ~$0.05) then your review.
- [ ] `segment.py` (Haiku 5.5), then `chunk_tts.py` again.
- [ ] `summaries.py` (Sonnet 5.5, under $1).
- [ ] `index.py --blurbs` (optional).
- [ ] `eval_spoilers.py` (dry run says about $2.3 for 100 questions; target 0 leaks; appends to NOTES.md).
- [ ] Textbook emphasis picks: `segment.py --book biology-2e-ch8 --emphasis` (optional).

## 3. Needs the Boson key (probes and prerender written; nothing has hit the live API)
`python scripts/run_all.py` runs these in order:
- [ ] The four probes `probe_{secret,tts,voice,realtime}.py` (M0 acceptance). They settle: timestamps null vs `[]`, tag tokens in timestamps, preset `berlinda`, mid-session voice change, commit-during-response, voice-clone response field.
- [ ] `prerender_tts.py` for Sherlock (~365 chunks, ~$0.69) and, once the text exists, Biology.
- [ ] Listen: M3 acceptance (no gaps over 150 ms, highlight tracks words).
- [ ] M4 acceptance: 10 live Talk cycles, latency under 2 s first audio, logged to NOTES.md.
- [ ] M7: five multilingual questions (zh/es/fr) answered in kind. Retrieval already verified offline.
- [ ] M8: clone a voice live, assign it to the narrator.
- [ ] M9 (stretch): `probe_avatar.py --portrait ...` then `prerender_avatar.py` (needs portraits).
- [ ] M10: the 90-second demo three times from a cold start.

## 4. Built without a key (do not redo)
- [x] M1 ingest (Sherlock 259 paragraphs), chunker (365 chunks), retrieval index (129 chunks + e5 embeddings).
- [x] M2 backend: books, TTS cache + timestamp alignment, token mint, retrieve with spoiler gating + property tests, voice allowlist, rate limits, optional access code, clone consent gate, startup warm-up.
- [x] M3 ReaderEngine, M4 TalkController (state machine, tap vs hold, barge-in, auto-resume, 10 cycles) + TalkClient (manual turns, close-code handling, **1013 backoff with a fresh key per retry**).
- [x] M5 server-side character gating; **`eval_spoilers.py`** (fake-tested; real run needs the Anthropic key).
- [x] M6 textbook pipeline: ingest headings/skip rules, no-LLM tutor cast, optional emphasis picks, tutor labels in the Stage. Only the chapter text is missing.
- [x] M7 **language badge** from the transcript (script and stopword detection) and the heard question shown in the Stage.
- [x] M8 voice lab UI (consent gate). Live cloning untested.
- [x] M9 scaffolding: `boson.py` video calls, `/api/avatar/{chunk_id}`, `/api/avatars/{book}`, `prerender_avatar.py`, `probe_avatar.py`, muted avatar video in the Stage. Request shape and audio-track question unverified.
- [x] M10 demo mode, README with the 90-second script, `scripts/run_all.py`, `Dockerfile` + `docs/DEPLOY.md` (image not built yet).
- [x] UI: portraits (auto-detected files), lock icon on unmet characters, thinking shimmer, speaking waveform, `T` key, frontend request timeouts, dark mode and phone layout checked in a browser.
- [x] Security: voice allowlist, rate limits, bounded uploads, query cap, keys never in the bundle or logs.

## 5. Known limits (kept on purpose)
- Spoiler rule is paragraph-granular: the rest of the paragraph the reader is in can be used.
- Until `segment.py` runs, chunks are narrator-only (no multi-voice reading).
- Server starts without BOSON_API_KEY; the error surfaces when audio is requested.
- Clone duration check only works for WAV; browser recordings rely on Boson's own 400.
- Hands-free mode (semantic_vad + `search_story` tool) is not built: stretch, and its API behaviour is unverified.
- Real-microphone behaviour in the browser has not been tested (needs the Boson key to be meaningful).
- Dockerfile and avatar request shape are unverified.
