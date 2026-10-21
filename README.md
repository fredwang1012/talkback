# Talkback

Talkback reads a book aloud with a full cast and lets you talk back to the characters. Narration and dialogue are
voiced with **Higgs Audio TTS 3**, using its expressive tags (emotion, style, prosody, sound effects), with the current
word highlighted as it is spoken. In **Talkback mode** you talk to one character at a time with push-to-talk over
**Higgs Realtime**; the answer is spoken in that character's own voice and is grounded only in what you have read so
far, so it never spoils the story, and the reading resumes from the same sentence. Characters get faces with **Higgs
Avatar** (including streaming lip-sync), you can clone a voice and give it a face, and you can upload your own novel or
textbook (a .txt or PDF) with automatic novel-vs-textbook detection.

Five fully processed public-domain samples ship in the repo, so you can try everything with nothing but a Boson API key:
*Alice's Adventures in Wonderland*, *The Wonderful Wizard of Oz*, *Peter Pan*, *The Wind in the Willows* and
*A Christmas Carol*.

## Quickstart (judges)

All you need is a Boson API key. Nothing else to sign up for.

**Prerequisites**

- Python 3.12, managed by [uv](https://docs.astral.sh/uv/) (uv installs the right Python for you):
  `pip install uv`, or `winget install astral-sh.uv` (Windows), or `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux)
- Node 20+
- A Boson AI API key

**Run it**

```bash
git clone <this repo> && cd talkback

cp .env.example .env            # then edit .env and set BOSON_API_KEY=... (leave everything else as it is)

cd frontend && npm install && npm run build && cd ..

cd backend && uv sync && uv run uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**. The backend serves the built frontend at `/` when `frontend/dist` exists, so there is
nothing else to start. (On Windows PowerShell use `Copy-Item .env.example .env`.)

**What to try**

1. On the library page open **Alice's Adventures in Wonderland** and press play. The narrator reads, the current word
   lights up, and the characters speak in their own voices.
2. Open the **Voices** panel. **One voice** reads everything with the narrator; **Full cast** gives every speaking
   character their own voice and expressive delivery.
3. Open **Recap** for a spoiler-safe "story so far" up to where you are.
4. Switch to **Talkback**, pick the **Cheshire Cat** (or the Queen of Hearts, or the Duchess), **hold Space** and ask
   a question out loud. Release to send it. Press Space again while it is talking to interrupt. Reading resumes from the
   same sentence.
5. Toggle **Spoilers**: off (the default) means the character only knows what you have read; on lets them know the whole
   book.
6. Upload your own book from the library page: a .txt from [Project Gutenberg](https://www.gutenberg.org) works well.
   Narration starts right away; characters, dialogue voices and the recap follow in the background (see below).

Keys while reading: Space (hold) talk, K play/pause, left/right previous/next sentence, [ and ] speed, T switch
Read/Talk, backtick opens the debug drawer (shows a running estimate of this session's spend).

**Set a monthly spend cap in the Boson workspace** (Billing, then Usage) before running anything that calls the API.
Audio is made on demand and cached in `data/cache/`, so a passage costs money only the first time it is played.

**Dev mode instead** (hot reload): run the backend as above, then in another terminal
`cd frontend && npm install && npm run dev` and open http://localhost:5173 (it proxies `/api` and `/media` to port 8000).

Without a Boson key the reader still works silently (play moves at an estimated reading pace).

## Keys: what is required and what degrades without each

| Key | Needed? | What it enables | Without it |
| --- | --- | --- | --- |
| `BOSON_API_KEY` | **Required** | Read-aloud audio (TTS 3), Talkback voice conversations (Realtime), faces (Avatar), voice cloning. Also writes Talkback answers in the default local mode. Stays on the server; the browser only gets short-lived keys. | The reader is silent; no Talkback, faces or cloning. |
| `PUBLIC_API_URL` | Pre-set in `.env.example` | Full upload processing and Gemini-written typed answers through the author's hosted service, with no key of your own (see below). | Uploads are processed with simple local fallbacks; typed answers go through Boson Realtime. |
| `ANTHROPIC_API_KEY` | Optional | Better character, dialogue and recap processing when you upload a novel. | Uploads are read by the narrator, and characters are found by a simple name-based fallback. The app says so: "Characters detected with a simple fallback (no LLM key configured)". |
| Google Cloud (`RETRIEVAL_BACKEND=vertex`, `GCP_PROJECT`, `GCP_BUCKET`, `gcloud auth application-default login`) | Optional | Gemini-written answers plus Vertex AI Vector Search (hybrid vector + full-text retrieval), and Gemini as an upload-processing model. | Local BM25 search over the book (no embedding model needed) and answers written through Boson Realtime. Works well for the sample book. |

All the optional settings are listed, commented out, in `.env.example`.

### Full upload processing and Gemini answers (no extra key)

`.env.example` already sets `PUBLIC_API_URL` to a small processing service the author runs on Google Cloud. With only a Boson
key, an uploaded novel is sent there while it is processed (main characters, dialogue split by character, chapter notes for
the recap), and typed Talkback questions are answered by Gemini. Your machine still does the spoiler filtering: the service
only ever receives passages you are already allowed to see, and it keeps no book text (results are held in memory for 30
minutes, request bodies are never logged).

The service has hard spend limits and can be switched off. If it is closed, unreachable, or over its free limit, nothing
breaks: the upload is processed locally with a simple name-based cast and narrator reading, and the page says why
("processing service unavailable" or "the free processing limit has been reached"); typed questions go through Boson
Realtime. The five sample books are already fully processed, so they are never affected. Voice conversations always run
on your Boson key. Remove `PUBLIC_API_URL` from `.env` to never contact the service.

## How Higgs Audio is used

- **Higgs TTS 3 (read-aloud).** Each paragraph is split into sentence-aligned chunks (at most 280 characters), one per
  speaker, and synthesized with timestamps, which drive the word-by-word highlighting. In full-cast mode every speaking
  character has a fixed voice, and expressive tags are added per segment: emotion and style (e.g. whispering,
  shouting) from the dialogue segmentation, prosody (pitch and speed) per character voice, and sound effects such as
  laughter inserted by rule. Clips are cached by text and voice.
- **Higgs Realtime (Talkback).** The browser opens a Realtime session with a short-lived key minted by the backend. It
  uses manual turns: hold Space to speak, release to commit. Pressing again interrupts the character (barge-in). The
  session is re-instructed and the voice switched per character, so each one answers in their own voice, and the spoken
  answer is built from the reader's position (retrieval limited to paragraphs already read, plus the "story so far").
- **Higgs Avatar (faces).** Face loops (speaking and idle) are rendered asynchronously from a portrait. For lip-sync, each
  answer sentence can be rendered as a talking clip and played in step with its audio using streaming fMP4 through the
  browser's MediaSource, so playback starts before the render ends.
- **Voice cloning.** In the Voices panel, create a voice from your own recording (a consent box is required), assign it to
  the narrator or a character, and give it a face.

## Architecture

Short version: a FastAPI backend (books, TTS cache and word alignment, spoiler-safe retrieval, token minting, upload
processing) and a React + Vite frontend (library, reader, Talkback stage, voice lab). The spoiler rule is enforced in the
backend and unit-tested: a passage is eligible only if it ends at or before the reader's paragraph. Details, data flow and
the per-service table are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

```bash
cd backend  && uv run pytest -q                              # or .venv/Scripts/python.exe -m pytest -q on Windows
cd frontend && npm test -- --run && npx tsc -p tsconfig.app.json --noEmit
```

No test calls a paid API; providers are faked.

## Known limitations

- The Harry Potter books are copyrighted and **not included**. The pipeline below can process them from your own PDF; only
  the public-domain Alice sample ships.
- Character personas are written by a model from each character's earliest lines, so quality varies. Two Alice personas
  (Queen of Hearts, Duchess) were tuned by hand.
- Without an LLM key an uploaded novel gets a heuristic cast (names next to speech verbs), narrator-read dialogue and a
  rough recap. Add `ANTHROPIC_API_KEY` or Google Cloud for the full result.
- Streaming lip-sync needs MediaSource (MSE): not available on iOS Safari, where lip-sync falls back to render-then-play
  clips (slower to start). Use a current desktop Chrome, Edge or Firefox.
- Talkback quality depends on the microphone and on the Boson Realtime service; answers may occasionally be too long or
  slightly off-character.
- Voice cloning is for voices you have the right to use; the UI asks you to confirm consent.
- The Cloud Run deployment files are the author's setup (see `docs/DEPLOY.md`) and are not needed to run locally.

## Example inputs and outputs (Alice's Adventures in Wonderland)

Questions asked in Talkback with the reader at the start of Chapter VII (*A Mad Tea-Party*). Answers are spoken; this is
the kind of answer to expect, not an exact transcript.

| You ask | Expected answer |
| --- | --- |
| "What happened to the Duchess's baby?" | The Duchess flung it to Alice before leaving for croquet, and it turned into a pig and trotted off into the wood. |
| "Why does the Cheshire Cat say it is mad?" | A dog growls when angry and wags its tail when pleased; the Cat does the opposite, so it must be mad. |
| To the Cheshire Cat: "Am I mad too?" | In character, calm and teasing: "We're all mad here... you must be, or you wouldn't have come here." |
| "What riddle does the Hatter ask at the tea-party?" | **Should decline**: the reader has not got there yet, so the character says it cannot spoil what comes next. |
| "Who stole the Queen's tarts?" (asked at Chapter VII) | **Should decline**, as a future event. |

More positions with already-read questions, should-decline questions and a character to ask are in
[docs/DEMO.md](docs/DEMO.md) and `data/demo/questions.json`.

---

## The pipeline: process your own books (optional)

The sample book needs none of this. This is how the Harry Potter series was built from the owner's own PDF at
`data/uploads/harry-potter-collection.pdf` (gitignored; `data/uploads/` and `data/books/hp*/` are never committed). It
uses an Anthropic key and Google Cloud, and prints a cost estimate and asks before spending more than $5.

```bash
python scripts/run_all.py --plan          # what would run, what is blocked and why (runs nothing)
python scripts/run_all.py                 # probes, ingest (keeps existing books), chunking, Vector Search indexing
python scripts/run_all.py --anthropic     # also summaries, series cast (pauses for review), segmentation, spoiler eval
```

Each step on its own:

```bash
uv run --project backend python preprocess/ingest_series.py --pdf data/uploads/harry-potter-collection.pdf
uv run --project backend python preprocess/summaries.py --book hp1-sorcerers-stone
uv run --project backend python preprocess/cast.py --series harry-potter
uv run --project backend python preprocess/segment.py --book hp1-sorcerers-stone
uv run --project backend python preprocess/chunk_tts.py --book hp1-sorcerers-stone
uv run --project backend python scripts/index_vertex.py --book hp1-sorcerers-stone
uv run --project backend python preprocess/eval_spoilers.py --book hp3-prisoner-of-azkaban --dry-run
```

### A cloned narrator with a face

```bash
uv run --project backend python scripts/clone_voice.py --audio clip.wav --transcript clip.txt --name "Cloned narrator" --face portrait.jpg --i-have-consent
uv run --project backend python scripts/make_face_loops.py --name "Cloned narrator"
```

Then pick it for the narrator in the Voices panel. The voice, face and loops stay local (gitignored, never deployed).

## Uploading your own books

Add a PDF or .txt from the library page. A **novel** can be read with the narrator right away; characters, voices and the
recap follow in the background (about 3 to 5 minutes), with progress shown on the page. A **textbook** gets the narrator
only. The kind defaults to **Auto-detect** (one cheap model call on a sample, a heuristic when that is unavailable);
each uploaded book has a "Move to Textbooks / Novels" button. Optional `.env` settings: `UPLOAD_LLM=claude|gemini`,
`LLM_PROVIDER=claude|gemini` (for the `preprocess/` scripts), `UPLOAD_CONFIRM=true` (show the cost estimate and ask
first). A book that would cost more than $4 to process is read by the narrator only, and processing stops for good at $15
total.

## Layout

```
backend/    FastAPI: books, TTS cache + word-timestamp alignment, spoiler-safe retrieval, token minting, voice cloning
frontend/   React + Vite: library, reader, Stage (Talk), voice lab, debug drawer
preprocess/ offline scripts (ingest_series, summaries, cast, segment, chunk_tts, eval_spoilers, prerender_tts, prerender_avatar)
scripts/    run_all.py, index_vertex.py, usage.py, clone_voice.py, make_face_loops.py, deploy_cloudrun.sh, probes
infra/      budget_stop: the Cloud Run function behind the author's $50 hard stop
data/       raw texts, generated books and indexes (the five public-domain sample books ship), caches (cache/ is gitignored)
docs/       ARCHITECTURE.md, DEMO.md, DEPLOY.md (author's Cloud Run setup); internal/ holds planning notes
```

## Credits and licences

*Alice's Adventures in Wonderland* (Lewis Carroll), *The Wonderful Wizard of Oz* (L. Frank Baum), *Peter Pan* (J. M. Barrie),
*The Wind in the Willows* (Kenneth Grahame) and *A Christmas Carol* (Charles Dickens) are in the public domain in the
United States (texts from Project Gutenberg, eBooks #11, #55, #16, #289 and #46). The Harry
Potter books are copyrighted, are used only from the owner's own copy and are not distributed with this repository. Audio
and realtime code in `frontend/src/audio` and parts of `src/realtime` are adapted from
[boson-ai/higgs-realtime-tutorial](https://github.com/boson-ai/higgs-realtime-tutorial) (Apache-2.0); see
`frontend/THIRD_PARTY_NOTICES.md`.
