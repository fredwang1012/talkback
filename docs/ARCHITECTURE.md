# Talkback architecture

Talkback reads the Harry Potter series aloud (word-highlighted, optionally full cast) and lets you stop and ask a
question out loud. The answer is grounded only in what you have read so far, spoken in the voice of whoever you are
talking to, and the reading picks up where it stopped.

## What runs where

| Piece | Where | What it does |
| --- | --- | --- |
| Frontend (React + Vite) | your browser; served by Vite on :5173 locally, by the backend container on Cloud Run | library, reader (one chapter rendered at a time), Stage panel (speaker, face, push-to-talk), voice lab |
| Backend (FastAPI, Python 3.12) | localhost:8000, or Cloud Run service `talkback` (us-central1) | every API below; holds all keys; enforces the spoiler rule |
| Book data | `data/books/<id>/book.json` (+ `index/chunks.json`); on Cloud Run the bucket `gs://frederick-talkback-data` is mounted at `/app/data` | paragraphs, chapters, cast, segments, TTS chunks, summaries |
| Audio cache | `data/cache/tts/<sha256>.mp3` + `.json` (word timestamps), served at `/media/tts/` | every Boson TTS clip is made once |
| Faces | `data/faces.json` + `data/portraits/faces/<id>.png`, `-talk.mp4`, `-idle.mp4`; lip-synced clips in `data/cache/avatar/talk/` (`/media/avatar/`) | the face library (see Faces and cast settings) |
| Spend log | `data/cache/usage.jsonl` (never served); `scripts/usage.py` prints the report | every paid call: Gemini, embeddings, Claude, Boson |

## External services

| Service | Used for | When |
| --- | --- | --- |
| **Boson Higgs TTS 3** (`/v1/audio/speech`) | read-mode audio per chunk, and each answer sentence (`/api/speak`) | live, cached |
| **Boson Realtime** (STT `higgs-stt-3.1`) | push-to-talk speech -> text, via a short-lived browser key from `/api/realtime/token` | live |
| **Boson voices** (`/v1/audio/voices`) | the cloned narrator voice | once |
| **Boson Higgs Avatar** (`/v1/videos`) | the face's speaking/idle loops (once), and optional lip-synced answer clips (~10 s render per sentence) | once / opt-in |
| **Vertex AI `gemini-embedding-001`** (768-d) | embeds retrieval chunks (indexing) and each question | indexing / live |
| **Vertex AI Vector Search 2.0**, collection `chunks` | hybrid search: vector + full-text, fused with RRF | live |
| **Vertex AI `gemini-2.5-flash`** | writes the spoken answer in character, streamed sentence by sentence | live |
| **Anthropic Claude** (Sonnet 5.5, Haiku 5.5, Opus 5.5) | offline preprocessing only: summaries, casting, dialogue segmentation, spoiler eval | offline |
| Cloud Run, Artifact Registry, Cloud Build | hosting the single container (min 0 instances: costs nothing idle) | deploy |
| Secret Manager | `boson-api-key`, `talkback-access-token` for the deployed service | deploy |
| Cloud Storage | `gs://frederick-talkback-data`: book data for the deployed service | deploy |
| Budget + Pub/Sub + Cloud Run function `budget-stop` | $50 budget alerts; the function unlinks billing at 100% (hard stop) | always on, idle |

## One question, step by step (Talk mode)

1. Hold Space or the mic button. The browser streams mic audio to **Boson Realtime** with a short-lived key; on release
   it commits the turn and gets the transcript (~0.7 s).
2. The browser sends the transcript and the reader's paragraph to `POST /api/answer`.
3. The backend embeds the question (**Gemini embeddings**) while **Vector Search** full-text search is already
   running, then runs the vector search; both use the filter
   `book_id in earlier books OR (book_id = this book AND para_end <= reader_para)` and are fused with RRF.
4. Spoiler backstop in our own code: anything from this book past the reader's paragraph is dropped again, whatever
   the store returned. Story-so-far = the latest summary at or before the reader's paragraph, plus one closing
   summary per earlier book of the series.
5. **Gemini 2.5 Flash** writes 2-4 spoken sentences in character; they stream back as NDJSON lines.
6. For each sentence the browser calls `/api/speak` (**Boson TTS**, in the speaker's voice, cached) and plays it while
   the next one is synthesised. If the voice has a face, the face's speaking loop plays; with lip-sync on, each
   sentence is also rendered as a lip-synced clip (`/api/avatar/talk`) and played in step with its audio.
7. Pressing again interrupts (barge-in); reading resumes from the same sentence.

## Read mode

`/api/tts` per TTS chunk (<= 280 characters, sentence-aligned), cached by text + voice. Word timestamps are aligned
to the paragraph text for highlighting. Voice choices (e.g. the cloned narrator) are stored per series in the
browser, so one choice covers all seven books. Audio is made on demand; nothing is pre-rendered (the series is
~6 M characters).

## Faces and cast settings

- **Face library** (`app/faces.py`): `POST /api/faces` (multipart `image`, `name`; PNG/JPEG/WebP by magic bytes, <= 5 MB),
  `GET /api/faces`, `DELETE /api/faces/{id}`. Ids are random. A face is a still portrait until the user clicks
  "Animate": `POST /api/faces/{id}/animate` renders the talk and idle loops with Boson Avatar (a few cents; idempotent,
  one render per face at a time, rate-limited with the lip-sync bucket). Faces made with a voice clone carry that
  voice id, so lip-sync and `/api/voices/faces` read from the same list; the old `data/voice_faces.json` is migrated
  into the library on first access.
- **Cast settings** (`app/cast.py`): `GET/PUT /api/books/{id}/cast` stores `main` (character ids), the narrator's and
  each character's `{voice, face}` in `data/books/<id>/cast_settings.json` (atomic write). `null` means the book's own
  voice / no face. `/api/tts` resolves a chunk's voice as request override, then the cast voice for its speaker, then
  the book's voice. Every endpoint above is gated by the access code.

## Uploaded novels: characters after upload (`app/enrich/`)

An uploaded **novel** is readable with the narrator seconds after upload (text ingest, TTS chunks, retrieval chunks).
Then a background job enriches it; textbooks skip it. Stages, each saving `book.json` atomically so voices and recap
switch on progressively: **cast** (one Sonnet-tier call, at most 8 main characters with voices from the presets and a
`first_para` computed from the text; other speakers are read by the narrator) -> **segment** (Haiku-tier, 10 paragraphs
per call, `UPLOAD_CONCURRENCY`=24 calls at once, verbatim check, narrator fallback; only main characters are speakers;
quotation marks a model drops are restored from the text) -> **summaries** (chapter notes in parallel: a checkpoint
every ~40 paragraphs and at each chapter end, each written only from that chapter's text up to the checkpoint; the
stored summary at paragraph P = condensed end-notes of completed chapters + the chapter's checkpoint at P, so nothing
derives from text after `upto_para`; chapters over 200 paragraphs are cut into units) -> **index** (Vertex re-index with
speakers when `RETRIEVAL_BACKEND=vertex`). Typical time is 3 to 5 minutes for a novel.

- **Status** (`GET /api/books/{id}/status`): `{status: "ready", enrich: {status, stage, progress 0..1, message, estimate_usd?}}`.
  `enrich.status` is `awaiting_confirmation | running | done | refused | error`; `stage` is
  `queued | cast | segment | summaries | index | done`. A failed or refused enrichment leaves the book readable with the
  narrator and a plain message. A server restart mid-job reports "interrupted".
- **LLM provider** (`app/enrich/llm.py`): `UPLOAD_LLM=claude|gemini` (default claude; developer-only). Gemini is 2.5 Flash on
  Vertex with JSON output. With Claude, `insufficient_quota`, billing, HTTP 402 or "credit balance too low" switches
  the rest of the job to Gemini (logged). `preprocess/` scripts use the same code with `LLM_PROVIDER=claude|gemini`.
- **Cost controls**: an estimate from text length per provider; a book estimated above $4 is refused ("This book is too
  long to process with characters; it will be read by the narrator."); real spend from usage tokens is added to
  `data/usage/upload_spend.json` (atomic) and everything stops at $15 cumulative. `UPLOAD_CONFIRM=true` (testing mode)
  sets `awaiting_confirmation` with `estimate_usd` and waits for `POST /api/books/{id}/enrich` (access-gated); the default
  starts straight away with no cost UI. One enrichment runs at a time.
- **Kind detection** (`app/enrich/detect.py`): upload `kind=auto` (default) classifies the extracted text with one
  fast-tier call on ~3,000 chars from the start, middle and end (`{kind, confidence}`), counted in the upload spend; on
  failure, when `UPLOAD_ENRICH=false` or the budget is used up, a heuristic decides (dialogue-quote ratio, Figure/Table/
  Exercise/Review questions/Chapter summary markers, numbered headings like `3.2 `; ties go to novel). The book stores
  `kind` and `kind_detected {kind, confidence, method}`; the status and `/api/books` expose them.
- **Changing kind**: `POST /api/books/{id}/kind {kind}` (access-gated; `u-` uploads only, 403 otherwise; 409 while
  enrichment runs or another change is in flight). textbook -> novel starts enrichment with the same caps and
  `UPLOAD_CONFIRM` behaviour unless the book already has characters; novel -> textbook only flips the kind (the cast
  stays in the file, the UI ignores it). The check-and-claim is synchronous and `_run_enrichment` skips books with a job
  in flight, so concurrent requests start one paid job.

### The public processing API (`app/public_api.py`, `app/public_client.py`)

A clone with only a Boson key has no LLM. When `PUBLIC_API_URL` is set and the machine has no `ANTHROPIC_API_KEY` or usable
GCP credentials, `app/public_client.py` sends the ingested narrator-only novel (title, sections, paragraphs; at most 3 MB /
6,000 paragraphs) to the separate `talkback-public` Cloud Run service (same image, `PUBLIC_API=true`, entry `app.asgi:app`).
The service runs the same `pipeline.run_stages` (cast, segment, summaries) with Gemini on an in-memory book and returns
characters, narrator, summaries and the non-trivial segments; the client re-validates every paragraph's segments against
its own text (anything that does not tile the paragraph becomes plain narration), rebuilds the TTS chunks and saves
`book.json`. Jobs are polled (`POST /api/public/enrich`, `GET /api/public/enrich/{id}`) into the usual `enrich.stage` /
`progress` status. Closed (403), missing (404), unreachable, failing or capped (402/429): the upload falls back to the
simple local cast with a degraded message that says which. Typed answers: `/api/answer` retrieves and spoiler-filters
locally and posts only the eligible passages, the persona and the question to `POST /api/public/answer`, which builds the
same prompt as `personas.build_answer_prompt` and streams Gemini's sentences back. `/api/health` reports
`answer_backend: "gemini"` only while the service answers its health check, so a closed service means Realtime answers.
Caps, rate limits and the deploy are in DEPLOY.md.

## Retrieval sizes

- Chunks (`retrieval.make_chunks`): paragraph-aligned, about 1,400 characters (min 900, max 2,000; a single longer paragraph
  stays one chunk), neighbours sharing about one trailing paragraph (up to 250 characters). HP book 3 is 607 chunks
  (median 1,308 characters). The whole series is 6,208 Vector Search objects (was 17,327 three-paragraph chunks, median 443 chars).
- `/api/answer` (Gemini): k = 8 (`DEFAULT_K`), passages capped at 12,000 characters (`MAX_PASSAGE_CHARS`), whole prompt at
  24,000 (`MAX_ANSWER_PROMPT_CHARS`). The most recent two paragraphs are always added.
- `/api/retrieve` (Boson realtime instructions): k <= 4 (`REALTIME_K`), passages capped at 4,500 characters
  (`REALTIME_MAX_PASSAGE_CHARS`), so the instructions fit `MAX_INSTRUCTIONS_CHARS` = 8,000 and stay short (docs/internal/BRIEF.md: long
  realtime prompts made the model stop calling tools).

## The spoiler rule (enforced on the server, never by the model)

- A passage of the book being read is eligible only if `para_end <= reader_para`. Earlier books of the series are
  fully eligible; later books never.
- Enforced three times: the Vector Search filter, the `VertexIndex` post-filter, and `retrieval._finish`.
- Summaries: only those with `upto_para <= reader_para`. They are built incrementally, each from the previous
  summary plus the new paragraphs only, so no summary ever saw later text.
- Characters can be talked to only after they have appeared (`first_para`; 0 if met in an earlier book). Full-book mode lifts
  this gate (server 403 and the Stage picker), so every character is available with spoilers on.
- Checked by `preprocess/eval_spoilers.py` (questions written by Sonnet, answered by the real `/api/answer`, judged by Opus).
- Full-book mode: `/api/retrieve` and `/api/answer` accept `spoilers: true` (default false, so every existing client stays
  spoiler-safe). For a novel it makes the whole book and the earlier books eligible (the Vector Search filter becomes
  current book OR earlier books), uses the book's last summary as "Whole book:", and switches to the full-book persona
  prompts. Textbooks ignore the flag, and the Reader toggle ("Spoilers: off / on") is stored per book in localStorage.

## Preprocessing pipeline (`scripts/run_all.py`)

| Step | Script | Model / cost |
| --- | --- | --- |
| ingest | `preprocess/ingest_series.py --pdf data/uploads/harry-potter-collection.pdf` | none: the PDF's table of contents gives 7 books, 198 chapters |
| summaries | `preprocess/summaries.py` | Sonnet 5.5, ~$12 for the series |
| cast | `preprocess/cast.py --series harry-potter` | Sonnet 5.5, one call (< $0.10) |
| segment | `preprocess/segment.py` | Haiku 5.5, 10 paragraphs per call |
| chunk | `preprocess/chunk_tts.py` | none |
| index | `scripts/index_vertex.py` | Gemini embeddings (~$0.25) + Vector Search storage |
| eval | `preprocess/eval_spoilers.py --book hp3-prisoner-of-azkaban` | Sonnet + Gemini + Opus (~$2-3) |

## Access and abuse limits

- Keys never reach the browser. On a public deployment, `TALKBACK_ACCESS_TOKEN` gates every endpoint that spends
  money and the full-text book endpoint (copyrighted text).
- Per-IP rate limits on answers, retrieval, TTS cache misses, lip-sync renders, uploads and clones.
- The cloned voice and face (`data/voices.json`, `data/faces.json`, `data/portraits/`) and the books
  (`data/books/hp*`) are gitignored. The deploy script syncs `data/books` to the private bucket but never portraits.
