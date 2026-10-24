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
| Faces | `data/portraits/voices/<voice>.png`, `-talk.mp4`, `-idle.mp4`; lip-synced clips in `data/cache/avatar/talk/` (`/media/avatar/`) | the cloned narrator's face |
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

## The spoiler rule (enforced on the server, never by the model)

- A passage of the book being read is eligible only if `para_end <= reader_para`. Earlier books of the series are
  fully eligible; later books never.
- Enforced three times: the Vector Search filter, the `VertexIndex` post-filter, and `retrieval._finish`.
- Summaries: only those with `upto_para <= reader_para`. They are built incrementally, each from the previous
  summary plus the new paragraphs only, so no summary ever saw later text.
- Characters can be talked to only after they have appeared (`first_para`; 0 if met in an earlier book).
- Checked by `preprocess/eval_spoilers.py` (questions written by Sonnet, answered by the real `/api/answer`, judged by Opus).

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
- The cloned voice and face (`data/voices.json`, `data/voice_faces.json`, `data/portraits/`) and the books
  (`data/books/hp*`) are gitignored. The deploy script syncs `data/books` to the private bucket but never portraits.
