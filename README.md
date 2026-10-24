# Bookson

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
| `BOOKSON_ACCESS_TOKEN` | Optional. For public deployments: voices then need an access code typed into the page. |

**Set a monthly spend cap in the Boson workspace** (Billing, then Usage) before running anything that calls the API.
The debug drawer (backtick key) shows a running estimate of this session's spend.

## Preprocessing, in order

Each script takes `--book <id>`, is safe to rerun, and prints a cost estimate first.

```bash
cd backend
uv run python ../preprocess/ingest.py     --book scandal-in-bohemia   # text -> paragraphs
uv run python ../preprocess/cast.py       --book scandal-in-bohemia   # Anthropic: characters, personas, voices (review the result!)
# segment.py and summaries.py (Anthropic) are still to be written
uv run python ../preprocess/chunk_tts.py  --book scandal-in-bohemia   # TTS chunks of at most 280 characters
uv run python ../preprocess/index.py      --book scandal-in-bohemia   # retrieval index; add --blurbs to use Anthropic
uv run python ../preprocess/prerender_tts.py --book scandal-in-bohemia # Boson: ~$0.70 for the whole story
```

## Run

```bash
cd backend  && uv run uvicorn app.main:app --port 8000
cd frontend && npm run dev          # http://localhost:5173
```

Without `BOSON_API_KEY` the reader works silently (play moves at an estimated reading pace). Add the key and restart the
backend to get audio and Talk mode.

## The 90-second demo

Run `prerender_tts.py` first, then open `http://localhost:5173/?demo=1#/read/scandal-in-bohemia`. Demo mode starts just
after Holmes reads the King's letter, plays pre-rendered audio only (no live synthesis, so no surprises), and pins the
character to Holmes.

1. Press play. The narrator and characters read in their own voices, the current word lit as it is spoken.
2. Hold Space mid-scene and ask: "Holmes, what do you make of this visitor?" He answers in character from what has been
   read, then the reading resumes from the same sentence.
3. Hold Space and ask in Mandarin: "你觉得这封信是谁写的？" The answer comes back in Mandarin.
4. Ask "How does this end?" Holmes declines in character. Point at the badge: he only knows up to the current paragraph.
5. Open the Voices panel, create a voice from your own recording (consent box required), assign it to the narrator, and
   keep reading in your voice.

## Layout

```
backend/    FastAPI: books, TTS cache + word-timestamp alignment, spoiler-safe retrieval, token minting, voice cloning
frontend/   React + Vite: library, reader, Stage (Talk), voice lab, debug drawer
preprocess/ offline scripts (ingest, cast, chunk_tts, index, prerender_tts)
scripts/    live API probes (probe_*.py) that record findings in NOTES.md
data/       raw texts, generated books and indexes, caches (cache/ is gitignored)
```

## Credits and licences

*A Scandal in Bohemia* is public domain (Project Gutenberg #1661). The textbook chapter is from
[Biology 2e by OpenStax](https://openstax.org/details/books/biology-2e), licensed CC BY 4.0. Audio and realtime code in
`frontend/src/audio` and parts of `src/realtime` are adapted from
[boson-ai/higgs-realtime-tutorial](https://github.com/boson-ai/higgs-realtime-tutorial) (Apache-2.0); see
`frontend/THIRD_PARTY_NOTICES.md`.
