# Talkback

> ### ▶ [Click here to open the live app](https://talkback-272822965835.us-central1.run.app)
> Sign in with Google. Access is open during the judging window; if it asks for access, run it locally instead.
>
> ### Or run it locally (only a Boson API key needed)
> ```bash
> git clone https://github.com/fredwang1012/talkback && cd talkback
> cp .env.example .env     # then set BOSON_API_KEY=... in .env and leave the rest as it is
> cd frontend && npm install && npm run build && cd ..
> cd backend && uv sync && uv run uvicorn app.main:app --port 8000
> ```
> Open **http://localhost:8000**. Needs Python 3.12 + [uv](https://docs.astral.sh/uv/) and Node 20+ ([full steps](#quick-start-judges)).
> `.env.example` already points `PUBLIC_API_URL` at the hosted processing service, so typed answers, web search and
> upload processing work with no Google Cloud setup ([details](#the-public-processing-service-no-extra-key)).

Talkback reads books aloud with a full expressive cast and lets you talk back to the characters. Narration and
dialogue are voiced with **Higgs Audio TTS 3** (emotion, whispering and shouting, sound effects, singing, pauses),
with each word highlighted as it is spoken and a lip-synced face for whoever is speaking. Stop at any point and ask a
question out loud: **Higgs Realtime** hears you (turn detection, barge-in) and the narrator or character you pick
answers in their own voice (spoken by Higgs Realtime, or written by Gemini and spoken by Higgs TTS when Google Cloud
is configured), using
only what you have read so far, so it never spoils the story. Then reading continues from the same sentence. You can
also upload your own novel or textbook (PDF or .txt). Textbooks get typeset equations, figures, study-note recaps and
a spoken quiz.

Five fully processed public-domain books ship in the repo, so you can try everything with only a Boson API key:
*Alice's Adventures in Wonderland*, *The Wonderful Wizard of Oz*, *Peter Pan*, *The Wind in the Willows* and
*A Christmas Carol*.

## What you can do

- **Hear a book performed by a full cast.** Every line is spoken by its character with Higgs TTS 3 tags: one of 20
  emotions per line, whispering and shouting from the speech tags, sound effects (laughter, sighs, sneezes, humming)
  before matching words, pauses at scene breaks, and songs that are actually sung. Two voices (male and female, by
  each character's gender) or one voice.
- **Talk to the characters, hands-free.** Hold Space to ask, or turn on hands-free: Boson's `semantic_vad` turn
  detection decides when your question has ended, and you can talk over the character to interrupt (barge-in). The
  answer is in the character's voice and reading resumes where it stopped.
- **See them speak.** Faces are lip-synced with Higgs Avatar, streamed so the video starts before the render ends.
  Any voice can drive any face.
- **Clone a voice.** Record or upload a sample (with a consent check), and give it a face from a photo or your webcam.
- **No spoilers.** The server only lets a character use passages and summaries up to your position, and characters you
  have not met yet are locked. Turn Spoilers on to talk with full-book knowledge.
- **Ask in your language.** Questions in other languages are searched in English and answered and spoken in your
  language.
- **Let the character drive the reader.** "Read chapter 3", "go back a bit", "slower", "recap the last chapter",
  "bookmark this" act on the reader: Realtime function calls when Realtime answers, matched from the transcript
  when Gemini answers.
- **Study a textbook.** Equations typeset with KaTeX and read out in words, figures shown inline, nested sections,
  "where is BM25 explained?", "quiz me on this section", "explain this equation", per-chapter study notes.
- **Get grounded answers from the web.** For textbooks (and novels with Spoilers on), Gemini answers can use Google
  Search, with source links shown under the reply. Never for a novel read spoiler-safe.
- **Recaps.** Last chapter, this book so far, or the series so far, read aloud by the narrator.

The full list (191 features, with status and known gaps) is in [docs/FEATURES.md](docs/FEATURES.md).

## Try it online

A hosted copy runs on Google Cloud Run: **https://talkback-272822965835.us-central1.run.app**

It is the same app with everything configured (Gemini answers with web search, Vector Search, uploads processed in the
cloud). Access is through Google sign-in; it is opened for the judging window. If it asks for access, use the local quick
start below.

## Quick start (judges)

All you need is a Boson API key.

**Prerequisites**

- Python 3.12 and [uv](https://docs.astral.sh/uv/) (uv installs the right Python for you):
  `pip install uv`, or `winget install astral-sh.uv` (Windows), or `curl -LsSf https://astral.sh/uv/install.sh | sh`
  (macOS/Linux)
- Node 20 or newer
- A Boson AI API key. Set a monthly spend cap in the Boson workspace (Billing, then Usage) first.

**Run it**

```bash
git clone <this repo> && cd talkback

cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
                                # then edit .env and set BOSON_API_KEY=... (leave the rest as it is)

cd frontend && npm install && npm run build && cd ..

cd backend && uv sync && uv run uvicorn app.main:app --port 8000
```

Open **http://localhost:8000**. The backend serves the built frontend at `/` when `frontend/dist` exists, so there is
nothing else to start. Use a current desktop Chrome, Edge or Firefox, and allow the microphone.

**Dev mode** (hot reload): run the backend as above, then in a second terminal `cd frontend && npm run dev` and open
http://localhost:5173. Vite proxies `/api`, `/media` and `/portraits` to `http://localhost:8000` (set `TALKBACK_API`
to use another port).

**What to try**

1. Open **Alice's Adventures in Wonderland** and press play. Words light up as they are read; characters speak in
   their own voices.
2. Open **Character Control** in the player bar: Two voices or One voice, which voice narrates, a face for each voice,
   or clone your own.
3. Open **Recap** and pick **Last chapter**, **This book so far** or **The series so far**.
4. Switch to **Talkback**, pick the **Cheshire Cat** (or the Queen of Hearts, or the Duchess), **hold Space** and ask a
   question. Release to send. Press Space again while it talks to interrupt. Or turn on **Hands-free (use
   headphones)** and just speak.
5. Ask "read chapter 7" or "a bit slower": the character acts on the reader.
6. Ask about something later in the book: with Spoilers off (the default) the character declines.
7. Upload a book from the library page (a .txt from [Project Gutenberg](https://www.gutenberg.org) works well). The
   narrator can read it right away; characters, voices and recap follow in the background.

Questions with expected answers, and questions that should be declined, are in [docs/DEMO.md](docs/DEMO.md) and
`data/demo/questions.json`.

Keys while reading: hold Space to talk, K play/pause, left/right previous/next sentence, `[` and `]` speed, T switches
Read/Talkback, backtick opens the debug drawer (with a running estimate of this session's Boson spend).

Audio is made on demand and cached in `data/cache/`, so a passage costs money only the first time it is played.
Without a Boson key the reader still runs silently at an estimated reading pace.

### What works with only a Boson key

| Feature | With only `BOSON_API_KEY` |
| --- | --- |
| The five sample books: read-aloud, full cast, expressive delivery, highlighting | Yes (already fully processed) |
| Talking to characters by voice (push-to-talk, hands-free, barge-in, tools) | Yes |
| Faces, lip-sync, voice cloning | Yes |
| Spoiler-safe answers | Yes. Local search is keyword search (BM25), or hybrid BM25 + multilingual-e5 with `uv sync --extra embed`; answers are written and spoken by Boson Realtime |
| Typed questions answered by Gemini, web search | Only through the public processing service (below), when it is open |
| Uploads | Readable with the narrator at once. Full processing (characters, dialogue voices, recap notes, equation readings) runs on the public processing service when `PUBLIC_API_URL` is set and the service is open; otherwise simpler local fallbacks (a name-based cast, narrator reading) with a note saying why |

### The public processing service (no extra key)

`.env.example` sets `PUBLIC_API_URL` to a small processing service the author runs on Google Cloud (Gemini on
Vertex AI). It is used only when your machine has no LLM of its own (no `ANTHROPIC_API_KEY`, no Google Cloud
credentials). While a book is processed, its text is sent there and held in memory for up to 30 minutes; it is not
stored and request bodies are never logged. Spoiler filtering stays on your machine: for answers, the service only
receives passages you are already allowed to see. The service has hard spend limits and can be closed. If it is
closed, unreachable or over its limit, nothing breaks: uploads use the local fallbacks and the page says why, and
questions go through Boson Realtime. Voice conversations always run on your own Boson key. Remove `PUBLIC_API_URL`
from `.env` to never contact it.

### Optional keys

| Setting | What it adds |
| --- | --- |
| Google Cloud (`RETRIEVAL_BACKEND=vertex`, `GCP_PROJECT`, `gcloud auth application-default login`) | Gemini-written answers with optional web search, Vertex AI Vector Search (hybrid vector + full-text retrieval), Gemini upload processing on your own project |
| `ANTHROPIC_API_KEY` | Claude as the upload-processing model (Gemini is the default; the project itself runs on Gemini) |

All optional settings are listed, commented out, in `.env.example`; every setting is in `backend/app/config.py`.

## How Higgs Audio is used

| Model / API | Used for |
| --- | --- |
| **Higgs TTS 3** (`/v1/audio/speech`) | Read-aloud chunks (at most 280 characters, sentence-aligned, one voice per speaker) with word timestamps for highlighting. Expressive tags per chunk: emotion, style (whispering, shouting, singing), sound effects, prosody (pitch, speed, expressiveness, pauses). Answer and recap sentences with an emotion tag chosen per sentence, and a `language` code for answers in other languages. Every clip is cached by text and voice. |
| **Higgs Realtime** (WebSocket, short-lived key per connection; `higgs-realtime-1.5-preview` by default, the page falls back to `higgs-realtime` if the preview refuses the session, and `?rt=higgs-realtime` forces the stable model) | Voice questions: push-to-talk (manual turns) or hands-free (`semantic_vad` with `create_response: false`, so every answer still goes through spoiler-safe retrieval; falls back to `server_vad`, then a local voice gate). Barge-in with `conversation.item.truncate` + `response.cancel`. Client function tools that move the reader. Spoken answers in the character's voice when Gemini is not configured. |
| **Higgs STT 3.1** (Realtime transcription) | Transcripts of each question, with a language hint once the reader's language is clear. |
| **Higgs Avatar** (`/v1/videos`, `/v1/videos/stream`) | Talk and idle loops for each face, and a lip-synced clip for every sentence spoken by a face (read mode, answers, recaps), streamed as fragmented MP4 into the browser's MediaSource and kept in step with the voice. Output size is `AVATAR_SIZE` (default `640x640`, $0.09/min; `480x640` or `640x480` cost $0.07/min). |
| **Voice cloning** (`/v1/audio/voices`) | Your own voices from a recording, with a server-checked consent box, usable for the narrator or any character, with any face. |

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system context, deployment, module maps, sequence diagrams, data
  model, security and cost controls, external API calls, configuration.
- [docs/FEATURES.md](docs/FEATURES.md): every feature with its status, issue numbers and known gaps.
- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): how each feature works, step by step, and known limitations.
- [docs/DEMO.md](docs/DEMO.md): a demo script and test questions per book.
- [docs/DEPLOY.md](docs/DEPLOY.md): the author's Cloud Run setup (not needed to run locally).

Short version of the architecture: a FastAPI backend (books, TTS cache and word alignment, spoiler-safe retrieval,
Realtime key minting, avatar streaming, upload processing) and a React + Vite frontend (library, reader, Talkback,
Character Control). The spoiler rule is enforced in the backend and unit-tested: a passage is eligible only if it ends
at or before the reader's paragraph, and a summary only if it covers no more than that.

## Tests

```bash
cd backend  && uv run pytest -q
cd frontend && npm test && npx tsc -p tsconfig.app.json --noEmit
```

About 1,080 backend tests and 675 frontend tests. No test calls a paid API: every provider is faked.

## Limitations

- **Local search needs the optional `embed` extra for meaning-based search.** Without Google Cloud, retrieval is BM25
  keyword search. Install with `uv sync --extra embed` (adds sentence-transformers / PyTorch, a large download) and the
  server builds multilingual-e5 embeddings for every book in the background on first start (about 8–9 minutes for the
  sample books on a laptop), then searches hybrid BM25 + e5. Without it, answers stay spoiler-safe but can miss a
  passage phrased very differently from the question.
- **Reading position is per browser** (localStorage), not synced to an account.
- **Uploads must be in English** (#140). Talking to a character in another language works; a non-English book does not.
- **Moving a paper to Textbooks** restores its equations but not its figures (#148). Upload it as a textbook instead.
- **Hands-free works best with headphones.** With laptop speakers, the sensitive barge-in setting (#159) can let the
  character's own voice interrupt it. Push-to-talk is not affected.
- **Gemini processing can be throttled.** Vertex Gemini uses dynamic shared quota; when many books are processed at
  once, calls are slowed and retried, so processing takes longer.
- **Streaming lip-sync needs MediaSource**, which iOS Safari lacks; there, clips are rendered first and then played
  (slower to start). Answers spoken directly by Boson Realtime (the local default) show the face's speaking loop, not
  per-sentence lip-sync.
- Character personas are written by a model from each character's earliest lines, so quality varies. A spoiler eval
  over 122 questions found about 3 answers that leaked from the model's own knowledge of the book.
- Textbook tables are dropped, and formulas split over several PDF lines are only partly marked.
- Demo mode (`?demo=1`) is wired to a Harry Potter book, which a clone does not have.
- Voice cloning is for voices you have the right to use; the app asks you to confirm consent.

More detail: [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md#24-known-limitations) and
[docs/FEATURES.md](docs/FEATURES.md#known-gaps--not-built).

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

---

## The offline pipeline: process your own books (optional)

The sample books need none of this. This is how the author's private Harry Potter series was built from the owner's
own PDF at `data/uploads/harry-potter-collection.pdf` (gitignored: `data/uploads/` and `data/books/hp*/` are never
committed). It prints a cost estimate and asks before spending more than $5. The `--anthropic` stages were written
for Claude; the individual scripts below also run on Gemini with `LLM_PROVIDER=gemini`.

```bash
python scripts/run_all.py --plan          # what would run, what is blocked and why (runs nothing)
python scripts/run_all.py                 # probes, ingest (keeps existing books), chunking, Vector Search indexing
python scripts/run_all.py --anthropic     # also summaries, series cast (pauses for review), segmentation, spoiler eval
```

Each step on its own (`LLM_PROVIDER=gemini` runs the LLM steps on Gemini):

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

Then pick it in Character Control. The voice, face and loops stay local (gitignored, never deployed). The same can be
done in the app, without the scripts.

## Uploading your own books

Add a PDF or .txt from the library page (20 MB at most; several files at once is fine). A **novel** can be read with
the narrator right away; characters, voices and the recap follow in the background, with progress shown on its card.
A **textbook** gets equations, figures, nested sections and study notes. Kind defaults to **Auto-detect** (one cheap
model call on a sample, a heuristic when that is unavailable); each uploaded book has a "Move to Textbooks / Novels"
action. Re-uploading the same file opens the existing copy.

Processing is estimated before it starts. With your own Google Cloud or Anthropic key, a book whose estimate is above
`UPLOAD_BOOK_CAP_USD` (default $4) is read by the narrator only, and processing stops for good once
`UPLOAD_TOTAL_CAP_USD` (default $15) has been spent. `UPLOAD_CONFIRM=true` shows the estimate and asks first. (The
author's hackathon deployment raises both caps.)

## Layout

```
backend/    FastAPI: books, TTS cache + word alignment, spoiler-safe retrieval, Realtime keys, avatar streaming,
            voice cloning, upload ingest and enrichment (app/enrich), public processing API (app/public_api.py)
frontend/   React + Vite: library, reader, Talkback, Character Control, Realtime client and tools (src/talk), debug drawer
preprocess/ offline scripts (ingest_series, summaries, cast, segment, chunk_tts, eval_spoilers, prerender_tts, prerender_avatar)
scripts/    run_all.py, index_vertex.py, usage.py, clone_voice.py, make_face_loops.py, deploy_cloudrun.sh,
            deploy_public_api.sh, probes
infra/      budget_stop: the Cloud Run function behind the author's GCP budget hard stop
data/       raw texts, the five processed sample books, demo questions; caches (data/cache/ is gitignored)
docs/       ARCHITECTURE.md, FEATURES.md, HOW-IT-WORKS.md, DEMO.md, DEPLOY.md; internal/ holds planning notes
```

## Credits and licences

*Alice's Adventures in Wonderland* (Lewis Carroll), *The Wonderful Wizard of Oz* (L. Frank Baum), *Peter Pan*
(J. M. Barrie), *The Wind in the Willows* (Kenneth Grahame) and *A Christmas Carol* (Charles Dickens) are in the public
domain in the United States (texts from Project Gutenberg, eBooks #11, #55, #16, #289 and #46). The Harry Potter books
are copyrighted: they are used only from the owner's own copy, are never in this repository, and are visible only to
the owner's account on the deployed site.

Files under `frontend/src/audio`, `frontend/src/realtime`, `frontend/src/state`, `frontend/src/ui/DebugDrawer.tsx` and
`frontend/test/` are adapted from [boson-ai/higgs-realtime-tutorial](https://github.com/boson-ai/higgs-realtime-tutorial)
(Apache-2.0). Each copied file keeps its header; see `frontend/THIRD_PARTY_NOTICES.md`.
