# Talkback

Talkback reads books aloud with a full expressive cast and lets you talk back to the characters. Narration and
dialogue are voiced with **Higgs Audio TTS 3** (emotion, whispering and shouting, sound effects, singing, pauses),
each word is highlighted as it is spoken, and the speaker gets a lip-synced face. Stop at any point and ask a question
out loud: **Higgs Realtime** hears you (turn detection, barge-in) and the character you pick answers in their own
voice, using only what you have read so far, so it never spoils the story. Then reading continues from the same
sentence. You can also upload your own novel or textbook (PDF or .txt).

Five fully processed public-domain books ship in the repo, so everything works with only a Boson API key:
*Alice's Adventures in Wonderland*, *The Wonderful Wizard of Oz*, *Peter Pan*, *The Wind in the Willows* and
*A Christmas Carol*.

## Features

- **A full cast.** Every line is spoken by its character with Higgs TTS 3 tags: one of 20 emotions per line,
  whispering and shouting from the speech tags, sound effects (laughter, sighs, humming), pauses at scene breaks, and
  songs that are actually sung. Two voices (male and female) or one.
- **Talk to the characters, hands-free.** Hold Space to ask, or turn on hands-free: Boson's `semantic_vad` decides
  when your question has ended, and you can talk over the character to interrupt.
- **Faces.** Lip-synced with Higgs Avatar and streamed, so the video starts before the render ends. Any voice can
  drive any face.
- **Your own voice.** Clone a voice from a recording and give it a face from a photo or your webcam.
- **Live faces (optional).** With a LiveKit Cloud project set in `.env`, a Boson preset face (Claire, Emma or James)
  can answer live in the Talkback view: its voice and lips come from one stream. Your own faces keep per-sentence clips.
- **No spoilers.** The server only gives a character passages and summaries up to your position; characters you have
  not met yet are locked. Turn Spoilers on for full-book knowledge.
- **Voice commands.** "Read chapter 3", "go back a bit", "slower", "recap the last chapter", "bookmark this".
- **Textbooks.** Typeset equations read out in words, figures inline, nested sections, "where is BM25 explained?",
  "quiz me on this section", "explain this equation", study notes per chapter.
- **Other languages.** Ask in your language; the answer is spoken in it.
- **Web search.** For textbooks, and novels with Spoilers on, answers can use Google Search, with sources shown.
- **Recaps.** Last chapter, this book so far, or the series so far, read aloud by the narrator.

Every feature, with its status: [docs/FEATURES.md](docs/FEATURES.md).

## Quick start

Prerequisites: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 20+, and a Boson AI API key (set a monthly
spend cap in the Boson workspace first).

```bash
git clone https://github.com/fredwang1012/talkback && cd talkback
cp .env.example .env            # then set BOSON_API_KEY=... in .env
cd frontend && npm install && npm run build && cd ..
cd backend && uv sync && uv run uvicorn app.main:app --port 8000
```

Open **http://localhost:8000** in desktop Chrome, Edge or Firefox and allow the microphone. For hot reload, also run
`cd frontend && npm run dev` and open http://localhost:5173 (Vite proxies to port 8000; set `TALKBACK_API` for another).

Try it: open *Alice's Adventures in Wonderland* and press play; open **Character Control** to change voices and
faces; switch to **Talkback**, pick the Cheshire Cat, hold Space and ask a question; ask about something later in the
book and it declines.

Keys: hold Space to talk, K play/pause, left/right sentence, `[` `]` speed, T switches Read/Talkback, backtick opens
the debug drawer. Audio is generated on demand and cached in `data/cache/`, so a passage costs credit only once.

### What needs more than a Boson key

| Feature | Boson key only | With a Gemini API key (no Google Cloud) | With Google Cloud |
| --- | --- | --- | --- |
| Sample books: reading, full cast, faces, voice cloning, voice questions | Yes | Yes | Yes |
| Spoiler-safe search | BM25 (or BM25 + multilingual-e5 with `uv sync --extra embed`) | Same as Boson key only | Vertex AI Vector Search |
| Answers | Written and spoken by Higgs Realtime | Written by Gemini (with web search), spoken by Higgs TTS | Written by Gemini (with web search), spoken by Higgs TTS |
| Uploads | Narrator reading, name-based cast | Full processing: cast, dialogue voices, recaps, equations | Full processing: cast, dialogue voices, recaps, equations |

Gemini API key: set `GEMINI_API_KEY` to a Google AI Studio key (free tier, rate-limited; same Flash / Flash-Lite
models). Google Cloud: set `RETRIEVAL_BACKEND=vertex` and `GCP_PROJECT`, then `gcloud auth application-default login`.
With both, Vertex is used; `/api/health` says which (`llm`: `vertex`, `ai_studio` or null).
`PUBLIC_API_URL` points at an optional hosted processing service; when it is closed or unreachable, the local
fallbacks are used and the page says why. Clear it to never contact it. Every setting is listed in `.env.example`
and `backend/app/config.py`.

## How Higgs Audio is used

| Model / API | Used for |
| --- | --- |
| **Higgs TTS 3** | Read-aloud chunks (at most 280 characters, one voice per speaker) with word timestamps, and per-chunk emotion, style, sound-effect and prosody tags. Answers and recaps with an emotion per sentence and a language code. Cached by text and voice. |
| **Higgs Realtime** | Voice questions: push-to-talk or hands-free (`semantic_vad`, `create_response: false` so every answer goes through spoiler-safe retrieval). Barge-in, client function tools that move the reader, spoken answers when Gemini is not configured. |
| **Higgs STT 3.1** | Question transcripts, with a language hint. |
| **Higgs Avatar** | Idle and talking loops per face, and a lip-synced clip per spoken sentence, streamed as fragmented MP4 into MediaSource. |
| **Voice cloning** | Your own voices, usable for the narrator or any character, with any face. |

## Architecture

A FastAPI backend (books, TTS cache and word alignment, spoiler-safe retrieval, Realtime key minting, avatar
streaming, upload processing) and a React + Vite frontend (library, reader, Talkback, Character Control). The spoiler
rule is enforced in the backend and unit-tested: a passage is eligible only if it ends at or before the reader's
paragraph, and a summary only if it covers no more than that.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): deployment, modules, sequence diagrams, data model, configuration.
- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): how each feature works, step by step.
- [docs/DEPLOY.md](docs/DEPLOY.md): the Cloud Run deployment (not needed to run locally).

```
backend/    FastAPI app (app/), tests
frontend/   React + Vite app (src/), tests
preprocess/ offline book processing (ingest, summaries, cast, segment, chunk_tts, spoiler eval)
scripts/    deploy, indexing, voice cloning, probes
infra/      budget hard-stop function for Google Cloud
data/       the five sample books and demo questions (caches are gitignored)
docs/       architecture, features, how it works, deployment
```

## Tests

```bash
cd backend  && uv run pytest -q
cd frontend && npm test && npx tsc -p tsconfig.app.json --noEmit
```

No test calls a paid API; every provider is faked.

## Limitations

- Without Google Cloud, search is keyword-based unless the `embed` extra is installed (a large PyTorch download).
- Reading position is kept per browser, not per account.
- Uploads in other languages (Spanish, French, Chinese and others) are detected and read in their language, but highlighting lights a sentence or two at a time instead of each word, and sound effects, singing and rule-based whisper/shout are English-only.
- Hands-free works best with headphones; laptop speakers can trigger barge-in.
- Streaming lip-sync needs MediaSource, which iOS Safari lacks (clips render first there).
- Textbook tables are dropped.

More: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md#24-known-limitations).

## Credits

The sample books are in the public domain in the United States (Project Gutenberg eBooks #11, #55, #16, #289 and
#46). Files under `frontend/src/audio`, `frontend/src/realtime`, `frontend/src/state`, `frontend/src/ui/DebugDrawer.tsx`
and `frontend/test/` are adapted from [boson-ai/higgs-realtime-tutorial](https://github.com/boson-ai/higgs-realtime-tutorial)
(Apache-2.0); see `frontend/THIRD_PARTY_NOTICES.md`.

## License

Copyright (c) 2026 Frederick Wang, Kent Chen and Siyi Zhu. All rights reserved. See [LICENSE](LICENSE).
