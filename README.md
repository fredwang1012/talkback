# Talkback

Books read aloud in full cast, with a way to talk back. Hold a key, ask a question out loud, get a spoken answer
grounded in what you have read so far (never a spoiler), and the story resumes from the same sentence.
Built for a Boson AI hackathon on Higgs TTS 3 and Higgs Realtime.

## Status

See `BLOCKED.md` for what is done and what is waiting on keys or files, and `NOTES.md` for findings. The full spec is `docs/BRIEF.md`.

## Setup

Requires Python 3.12 (managed by uv) and Node 20+.

```bash
# backend
cd backend
uv sync                      # add --extra embed to install sentence-transformers for the retrieval index
uv run pytest

# frontend
cd ../frontend
npm install
npm test
```

Copy `.env.example` to `.env` (gitignored) and fill it in:

| Variable | Needed for |
| --- | --- |
| `BOSON_API_KEY` | Audio, Talk mode, voice cloning. Never reaches the browser; the server mints short-lived keys. |
| `ANTHROPIC_API_KEY` | Offline preprocessing only (cast, segments, summaries). Not used at runtime. |
| `BOSON_BASE_URL` | Defaults to `https://api.boson.ai`. |
| `TALKBACK_ACCESS_TOKEN` | Optional. For public deployments: voices then need an access code typed into the page. |

**Set a monthly spend cap in the Boson workspace** (Billing, then Usage) before running anything that calls the API.
The debug drawer (backtick key) shows a running estimate of this session's spend.

## The pipeline: one command

The app contains only the Harry Potter series (7 books: `hp1-sorcerers-stone`, `hp2-chamber-of-secrets`,
`hp3-prisoner-of-azkaban`, `hp4-goblet-of-fire`, `hp5-order-of-the-phoenix`, `hp6-half-blood-prince`,
`hp7-deathly-hallows`), ingested from your own PDF at `data/uploads/harry-potter-collection.pdf` (gitignored).
The books are copyrighted: `data/uploads/` and `data/books/hp*/` are never committed.

```bash
python scripts/run_all.py --plan          # what would run, what is blocked and why (runs nothing)
python scripts/run_all.py                 # probes, ingest (keeps existing books), chunking, Vector Search indexing
python scripts/run_all.py --anthropic     # also summaries, series cast (pauses for review), segmentation, spoiler eval
```

Each step on its own (all print a cost estimate and ask before spending more than $5):

```bash
uv run --project backend python preprocess/ingest_series.py --pdf data/uploads/harry-potter-collection.pdf
uv run --project backend python preprocess/summaries.py --book hp1-sorcerers-stone [--book ...]   # Sonnet, incremental
uv run --project backend python preprocess/cast.py --series harry-potter                          # Sonnet, one call
uv run --project backend python preprocess/segment.py --book hp1-sorcerers-stone [--book ...]     # Haiku, batched
uv run --project backend python preprocess/chunk_tts.py --book hp1-sorcerers-stone [--book ...]
uv run --project backend python scripts/index_vertex.py --book hp1-sorcerers-stone [--book ...]   # Gemini embeddings
uv run --project backend python preprocess/eval_spoilers.py --book hp3-prisoner-of-azkaban --dry-run
uv run --project backend python scripts/usage.py --gcp-budget 50 --anthropic-budget 77.33        # spend so far
```

Read-mode audio is made on demand and cached (the series is ~6 M characters, so nothing is pre-rendered).

### A cloned narrator with a face

```bash
uv run --project backend python scripts/clone_voice.py --audio clip.wav --transcript clip.txt     --name "Cloned narrator" --face portrait.jpg --i-have-consent
uv run --project backend python scripts/make_face_loops.py --name "Cloned narrator"   # speaking + idle loops
```

Then pick it for the narrator in the reader's Voices panel (the choice covers the whole series). Its face shows in the
Stage while it reads or answers; tick "Lip-synced face" for real lip-sync on answers (~10 s extra per answer).
The voice, face and loops stay local (gitignored, never deployed). Architecture: `docs/ARCHITECTURE.md`.

## Run

```bash
cd backend  && uv run uvicorn app.main:app --port 8000
cd frontend && npm run dev          # http://localhost:5173
```

Without `BOSON_API_KEY` the reader works silently (play moves at an estimated reading pace). Add the key and restart the
backend to get audio and Talk mode.

## The 90-second demo

Open `http://localhost:5173/?demo=1#/read/hp1-sorcerers-stone` after pre-rendering the demo stretch
(`preprocess/prerender_tts.py --book hp1-sorcerers-stone --from-para N --to-para M`, plus
`--narrator-voice <id>` if the narrator uses the cloned voice). Demo mode starts at a preset point
in book 1, plays pre-rendered audio only (no live synthesis, so no surprises), and pins the character to Hagrid.

1. Press play. The narrator and characters read in their own voices, the current word lit as it is spoken.
2. Hold Space mid-scene and ask: "Hagrid, what do you make of this visitor?" He answers in character from what has been
   read, then the reading resumes from the same sentence.
3. Hold Space and ask in Mandarin: "你觉得这封信是谁写的？" The answer comes back in Mandarin.
4. Ask "How does this end?" Hagrid declines in character. Point at the badge: he only knows up to the current paragraph.
5. Open the Voices panel, create a voice from your own recording (consent box required), assign it to the narrator, and
   keep reading in your voice.

## Layout

```
backend/    FastAPI: books, TTS cache + word-timestamp alignment, spoiler-safe retrieval, token minting, voice cloning
frontend/   React + Vite: library, reader, Stage (Talk), voice lab, debug drawer
preprocess/ offline scripts (ingest_series, summaries, cast, segment, chunk_tts, eval_spoilers, prerender_tts, prerender_avatar)
scripts/    run_all.py, index_vertex.py, usage.py, clone_voice.py, make_face_loops.py, deploy_cloudrun.sh, probes
infra/      budget_stop: the Cloud Run function behind the $50 hard stop
data/       raw texts, generated books and indexes, caches (cache/ is gitignored)
```

Keys: Space (hold) talk, K play/pause, left/right sentence, [ and ] speed, T switch Read/Talk, backtick debug drawer.
Deployment notes: `docs/DEPLOY.md`.

## Credits and licences

The Harry Potter books are copyrighted and are used only from the owner's own copy; they are not distributed with this repository. Audio and realtime code in
`frontend/src/audio` and parts of `src/realtime` are adapted from
[boson-ai/higgs-realtime-tutorial](https://github.com/boson-ai/higgs-realtime-tutorial) (Apache-2.0); see
`frontend/THIRD_PARTY_NOTICES.md`.
