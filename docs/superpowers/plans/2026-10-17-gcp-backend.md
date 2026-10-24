# GCP backend: implementation plan

Spec: `docs/GCP_PLAN.md` (binding). This plan turns its to-dos C1–C7 into tasks.

## Context

Talkback is a FastAPI backend (`backend/app/`) plus a Vite/React frontend (`frontend/src/`). Talk mode today:
browser records speech → Boson Realtime transcribes → frontend `POST /api/retrieve` (local BM25 + e5 index in
`backend/app/retrieval.py`) → frontend sends the returned `instructions` to Boson Realtime, whose own model answers.

Target (user-confirmed): transcript → `POST /api/answer` on our backend → retrieval from **Vertex AI Vector
Search 2.0** (spoiler-gated) → **Gemini on Vertex** writes the exact spoken answer → frontend speaks each sentence
with **Boson TTS 3** in the character's voice. Realtime then only listens. Users can also upload a PDF from the
local UI; it is ingested into a new book and indexed in the cloud.

## Global constraints (bind every task)

1. **Spoiler invariant (novels):** a passage is eligible only if `para_end <= reader_para`. It must be enforced in
   backend Python code on every retrieval path, including the Vertex path (filter in the query AND a post-filter
   on returned results). Textbooks (`book.kind == "textbook"`) are exempt, as today.
2. **No live API calls in tests.** Use `httpx.MockTransport`, fakes, or monkeypatching. Implementers must not run
   anything that calls Boson, Vertex AI or Vector Search for real.
3. **Secrets:** never log or return `BOSON_API_KEY`, GCP tokens, or `.env` contents. No keys in frontend code.
4. **GCP values (use verbatim):**
   - project `frederick-talkback`, region `us-central1`, bucket `frederick-talkback-data`
   - collection path `projects/frederick-talkback/locations/us-central1/collections/chunks`
   - collection data fields: `book_id` (string), `chunk_id` (string), `section` (integer), `para_start` (integer),
     `para_end` (integer), `speaker` (string), `text` (string); vector field `embedding`, dense, 768 dims
   - data object id: `f"{book_id}__{chunk_id}"`
   - embeddings: model `gemini-embedding-001`, `output_dimensionality=768`, task type `RETRIEVAL_DOCUMENT` for
     chunks and `RETRIEVAL_QUERY` for questions
   - answer model: `gemini-2.5-flash` (setting `GEMINI_MODEL`), thinking budget 0, max 300 output tokens
5. **Vector Search 2.0 REST** (base `https://vectorsearch.googleapis.com/v1/{collection path}`):
   - batch create: `POST {base}/dataObjects:batchCreate`, body
     `{"requests":[{"parent": "<collection path>", "dataObjectId": id, "dataObject": {"data": {...}, "vectors": {"embedding": {"dense": {"values": [...]}}}}}]}`;
     max 1000 per request; atomic.
   - delete: `DELETE {base}/dataObjects/{id}`
   - search: `POST {base}/dataObjects:search`, vector body
     `{"vector_search": {"search_field": "embedding", "vector": {"values": [...]}, "filter": {...}, "top_k": N, "output_fields": {"data_fields": "*"}}}`.
     Response: `{"results": [{"dataObject": {"name": ..., "data": {...}}, "distance": float}]}`.
   - text search (shape not fully documented): `{"text_search": {"search_text": q, "data_field_name": "text", "top_k": N, "filter": {...}}}`.
     Treat any error from text search as "no text results" (log at debug, never fail the request).
   - filter for novels: `{"$and": [{"book_id": {"$eq": B}}, {"para_end": {"$lte": P}}]}`; textbooks:
     `{"book_id": {"$eq": B}}`.
   - auth: bearer token from Application Default Credentials (`google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])`),
     refreshed when expired. Header `x-goog-user-project: frederick-talkback`.
6. **Style:** match the surrounding code: async httpx where the app is async, short module docstrings, pydantic
   settings in `backend/app/config.py`, no new frameworks. Keep the local backend working with no GCP access:
   `RETRIEVAL_BACKEND=local` (default) must behave exactly as today.
7. **Commands:** uv is at `/c/Users/fredw/AppData/Roaming/Python/Python314/Scripts/uv.exe` (not on PATH).
   Backend tests: `cd backend && $UV run python -m pytest -q`. Frontend: `cd frontend && npm test` and
   `npx tsc -p tsconfig.app.json --noEmit`. Add deps with `$UV add <pkg>` inside `backend/`.
8. Commit after each task on branch `gcp-backend` with message trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
   Never push.

## Task 1: GCP settings, auth, embeddings, Gemini wrapper, usage log

Files: `backend/app/config.py`, new `backend/app/gcp/__init__.py`, `backend/app/gcp/auth.py`,
`backend/app/gcp/embed.py`, `backend/app/gcp/llm.py`, `backend/app/gcp/usage.py`, tests
`backend/tests/test_gcp_core.py`, `backend/pyproject.toml` (+ `uv.lock`).

- Settings (all optional, with these defaults): `gcp_project="frederick-talkback"`, `gcp_region="us-central1"`,
  `gcp_bucket="frederick-talkback-data"`, `retrieval_backend: Literal["local","vertex"]="local"`,
  `gemini_model="gemini-2.5-flash"`, `vs_collection="chunks"`. Add a property `vs_collection_path`.
- Deps: `google-genai`, `google-auth`.
- `auth.py`: `get_token() -> str` using ADC with cloud-platform scope, cached credentials, refresh when invalid.
- `embed.py`: `class GeminiEmbedder` with `embed_query(text) -> np.ndarray` and
  `embed_documents(texts, batch_size=100) -> np.ndarray` (float32, shape (n, 768)), using
  `google.genai.Client(vertexai=True, project=..., location=...)` and `models.embed_content` with
  `EmbedContentConfig(task_type=..., output_dimensionality=768)`. The client is injectable for tests.
  `embed_query` must satisfy the existing `retrieval.Embedder` protocol.
- `llm.py`: `class GeminiAnswerer` with `async def stream_sentences(system: str, user: str) -> AsyncIterator[str]`:
  streams from `client.aio.models.generate_content_stream` (config: `system_instruction`, `max_output_tokens=300`,
  `thinking_config=ThinkingConfig(thinking_budget=0)`), buffers text and yields complete sentences (split on
  `.`, `!`, `?`, `。`, `！`, `？` followed by whitespace or end), then the remainder at the end. Strip whitespace;
  never yield empty strings. Client injectable.
- `usage.py`: `record(kind: str, model: str, **counts)` appends one JSON line
  `{"ts": iso, "kind", "model", ...counts}` to `data/cache/usage.jsonl` (path from `settings.data_dir`).
  The embedder records `{"chars": total}`; the answerer records `prompt_tokens`/`output_tokens` from the final
  chunk's `usage_metadata` when present. Failures to write the log never raise.
- Tests (fakes only): sentence splitting across stream chunk boundaries (e.g. chunks `"Hello th"`, `"ere. How"`,
  `" are you? Fine"` → `["Hello there.", "How are you?", "Fine"]`); Chinese punctuation; embed batching (250 texts
  → 3 calls of ≤100) and output shape/dtype; task types passed correctly; usage lines written.

## Task 2: Vector Search 2.0 client

Files: new `backend/app/gcp/vectorstore.py`, tests `backend/tests/test_vectorstore.py`.

- `class VectorStore(collection_path, token_fn=get_token, client: httpx.AsyncClient | None = None)`.
- `async def upsert(objects: list[dict])`: each item `{"id": str, "data": {...}, "embedding": list[float]}`;
  sends batches of ≤1000 via `:batchCreate`. If a batch fails with HTTP 409 (already exists), delete those ids
  and retry the batch once.
- `async def delete(ids: list[str])`: one DELETE per id; 404 is fine.
- `async def vector_search(vector, filter, top_k) -> list[dict]` and
  `async def text_search(query, filter, top_k) -> list[dict]`: return `[{"data": {...}, "distance": float|None}]`.
  `text_search` returns `[]` on any HTTP error.
- `def spoiler_filter(book_id: str, reader_para: int | None) -> dict`: novel filter when `reader_para` is not
  None, else book-only filter (constraint 5).
- `def rrf(lists: list[list[dict]], key="chunk_id", k=60) -> list[dict]`: Reciprocal Rank Fusion, stable,
  dedup by `data[key]`.
- Errors: non-2xx (other than the cases above) raise `VectorStoreError(status, message)`; retry 429/5xx up to 3
  times with short exponential backoff (injectable sleep for tests).
- Tests with `httpx.MockTransport`: request URLs and JSON bodies match constraint 5 exactly; batching at 1000;
  409 path; text search error → `[]`; rrf ordering; auth header present.

## Task 3: Index a book into Vector Search

Files: new `scripts/index_vertex.py`, new `backend/app/gcp/indexing.py`, tests `backend/tests/test_indexing.py`.

- `indexing.py`: `build_objects(book, chunks, embeddings) -> list[dict]` (data fields per constraint 4; `section`
  = index of the chunk's section in `book.sections` (int); `speaker` = `"narrator"` unless all paragraphs in the
  chunk share one non-narrator speaker in their segments); `async def index_book(book, chunks, embedder, store)`
  embeds `chunk.text` (prefixed with `context_blurb` when present) and upserts. Returns count.
- Chunks come from `data/books/<id>/index/chunks.json` when it exists, else `retrieval.make_chunks(book)`.
- `scripts/index_vertex.py --book ID [--dry-run]`: prints chunk count, total characters and an estimated cost
  (`chars / 1000 * 0.000025` USD, label it an estimate); `--dry-run` stops there. Runs with
  `uv run --project backend python scripts/index_vertex.py`.
- Tests: object shape, id format, section mapping, dry-run makes no store/embedder calls (fakes).

## Task 4: Retrieval switch

Files: `backend/app/retrieval.py` (only if needed), new `backend/app/gcp/vertex_index.py`, `backend/app/main.py`,
tests `backend/tests/test_vertex_index.py`.

- `class VertexIndex(book, embedder, store)` exposing the same interface `retrieval.retrieve` uses from `Index`:
  `mode` (returns `"vertex"`) and `search(query, reader_para, k, *, spoiler_safe=True) -> list[Chunk]`.
  `retrieval.retrieve` is synchronous today; choose one: make VertexIndex.search sync by running the async
  store calls safely from sync code, or add an async `aretrieve` used by the endpoint. Whatever you choose,
  `/api/retrieve` must still work for `RETRIEVAL_BACKEND=local` with zero behaviour change.
- Search: embed query (`RETRIEVAL_QUERY`), run vector search (top_k = 4·k) and text search (top_k = 4·k) with
  `spoiler_filter(book.id, reader_para if spoiler_safe else None)`, fuse with `rrf`, **post-filter**
  `para_end <= reader_para` when spoiler_safe (constraint 1), take top k, return in book order as `Chunk`s.
  Apply the same recency boost semantics as `Index.search` only if it is simple; otherwise skip and note it.
- `main._index_for` returns a `VertexIndex` when `settings.retrieval_backend == "vertex"`.
- Tests: a fake store that ignores filters and returns a future chunk → it is still excluded (post-filter);
  textbook path passes `reader_para=None`; local backend unchanged (existing tests still pass).

## Task 5: `/api/answer` and `/api/speak`

Files: `backend/app/main.py`, `backend/app/personas.py`, tests `backend/tests/test_answer.py`.

- `POST /api/answer` body: `{book_id, query (≤500), reader_para ≥0, speaker_id="narrator", k=4}`. Same 404/403
  checks as `/api/retrieve` (unknown speaker, character not yet appeared). Runs retrieval (whichever backend),
  builds the prompt with a new `personas.build_answer_prompt(book, speaker_id, story_so_far, passages, query)
  -> (system, user)` (reuse the existing templates' wording; the user message is the question), then streams
  NDJSON (`application/x-ndjson`): one `{"type":"sentence","text":...}` line per sentence from
  `GeminiAnswerer.stream_sentences`, then `{"type":"done","passages":n,"retrieve_ms":..,"first_sentence_ms":..}`.
  On Gemini failure after streaming started, emit `{"type":"error","message":...}` and end.
  The answerer is created lazily and is overridable in tests (module-level factory or FastAPI dependency).
- `POST /api/speak` (requires `require_access`): body `{text (1..400 chars), voice}`; voice must pass
  `voices.is_known_voice`; rate-limited with the existing `tts_bucket` on cache misses; uses
  `tts_cache.get_or_create(boson, text, voice, text)` and returns the same shape as `/api/tts`
  (`audio_url`, `timestamps`, `cached`, `voice`).
- Tests: NDJSON lines in order with a fake answerer and fake retrieval; 403 for unmet character; spoiler: the
  prompt never contains text from paragraphs after reader_para (fake index returning only eligible chunks
  plus assert on built prompt); `/api/speak` rejects unknown voice and over-long text; cached path does not
  call Boson (monkeypatch `tts_cache`).

## Task 6: Frontend talk path via `/api/answer`

Files: `frontend/src/api.ts`, `frontend/src/talk/TalkController.ts`, `frontend/src/talk/useTalk.ts`,
`frontend/test/talkController.test.ts` (+ `frontend/test/api.test.ts`).

- `api.ts`: `streamAnswer(bookId, query, readerPara, speakerId, onSentence) -> Promise<void>` reading the NDJSON
  body incrementally (fetch + `ReadableStream` reader + line splitting), and `speak(text, voice)` →
  `TtsResponse`. Both use `authHeaders()` and the existing timeout helper where a whole-request timeout fits.
- `TalkController`: add an optional dep `answer?: (query, readerPara, speakerId, onSentence) => Promise<void>`
  and `speakSentence?: (text) => Promise<void>` (plays one sentence's audio to completion). When both are
  provided, the controller uses them instead of `retrieve` + `session.respond`: sentences are queued and
  spoken in order (start speaking the first while later ones arrive), state goes `retrieving` → `answering`
  → resume rules exactly as today (auto-resume only if the reader was playing before the press; barge-in on a
  new press stops playback and drops the queue). Keep the existing Realtime-answer path when the deps are
  absent.
- `useTalk.ts`: wire `answer` = `streamAnswer`, `speakSentence` = call `speak(text, voiceForSpeaker)` and play
  `audio_url` with an `HTMLAudioElement` (resolve on `ended`, reject on `error`; stop on barge-in). Voice for
  the speaker: the character's `voice` from the book, or the narrator voice. Use the new path by default; keep
  a constant `ANSWER_VIA_GEMINI = true` to flip back.
- Tests (vitest, fakes): sentences spoken in order; first sentence starts before the stream ends; barge-in
  clears the queue; auto-resume rules unchanged; old path still used when deps absent.

## Task 7: PDF upload and ingest (backend)

Files: new `backend/app/ingest_upload.py`, `backend/app/main.py`, tests `backend/tests/test_upload.py`,
deps `pymupdf`.

- `POST /api/books/upload` (requires `require_access`): multipart `file` (PDF or .txt, ≤20 MB, bounded read),
  `title` (1..120), `author` (≤120, optional), `kind` (`novel`|`textbook`). Creates a safe `book_id` slug from
  the title (+ short random suffix), saves the original to `data/uploads/<book_id>.<ext>`, then starts ingest
  in a background task and returns `{"book_id", "status": "processing"}` with 202.
- Ingest: extract text (PyMuPDF for PDF: page text joined with blank lines; for .txt read UTF-8), split into
  sections/paragraphs with `preprocess/ingest.py`'s `parse(raw, None, None)` (import it; if importing from
  `preprocess` is awkward, move the parsing helpers into `backend/app/` and have `preprocess/ingest.py` import
  them, keeping its tests green), build a narrator-only `Book` (license `"Uploaded by the user"`, narrator voice
  `marcus` with a minimal `Narrator`), add TTS chunks with `preprocess/chunk_tts.py`'s `chunk_paragraph` (same
  import rule), write `data/books/<book_id>/book.json`, build retrieval chunks with `retrieval.make_chunks`,
  save them to `data/books/<id>/index/chunks.json`, and when `retrieval_backend == "vertex"` index them with
  Task 3's `index_book`. Clear `_index_for`'s cache for that id.
- `GET /api/books/{book_id}/status` → `{"status": "processing"|"ready"|"error", "message"?}` kept in an
  in-memory dict and also written to `data/books/<id>/status.json`.
- Reject PDFs with no extractable text (`error`, message says scanned PDFs are not supported).
- Tests: a tiny generated PDF (create it with PyMuPDF in the test) and a .txt go through to `ready` with
  paragraphs and tts_chunks; oversize rejected; bad kind rejected; vertex indexing called only when enabled
  (fake); the new book appears in `GET /api/books`.

## Task 8: Upload UI

Files: `frontend/src/pages/Library.tsx`, `frontend/src/api.ts`, `frontend/src/index.css`, `frontend/test/api.test.ts`.

- An "Add a book" control on the Library page: file input (accept `.pdf,.txt`), title (prefilled from the file
  name), author, kind (Novel/Textbook). Submit → `uploadBook(...)` → poll `GET /api/books/{id}/status` every
  1.5 s until `ready`/`error` (stop polling on unmount). Show progress text and the error message. On ready,
  refresh the book list. Match existing Library styling; keyboard accessible; works at phone width.
- `api.ts`: `uploadBook(file, title, author, kind)` and `fetchBookStatus(id)`.
- Tests: `uploadBook` sends multipart with the fields and auth header; status polling helper stops on ready.

## Task 9: Usage report script

Files: new `scripts/usage.py`, tests `backend/tests/test_usage_report.py`.

- Reads `data/cache/usage.jsonl`, totals per kind/model and per day, and prints an estimated cost table using
  prices defined as constants at the top (gemini-2.5-flash: $0.30 / 1M input tokens, $2.50 / 1M output tokens;
  gemini-embedding-001: $0.000025 per 1K characters — label them "estimates; check the billing console").
  Also prints the remaining amount of a `--budget` (default 50). Handles a missing or partly corrupt file.
- Tests: totals and cost math on a fixture file; corrupt line skipped.

## Task 10: Cloud Run deploy artifacts (not executed)

Files: `Dockerfile`, `.dockerignore`, new `scripts/deploy_cloudrun.sh`, `docs/DEPLOY.md`.

- Dockerfile must install the new deps and still build the frontend into `frontend/dist`. Do not install the
  `embed` extra in the image (Vertex replaces local embeddings there).
- `deploy_cloudrun.sh`: `gcloud builds submit` to `us-central1-docker.pkg.dev/frederick-talkback/talkback/api`,
  then `gcloud run deploy talkback --region us-central1 --service-account talkback-api@frederick-talkback.iam.gserviceaccount.com
  --set-env-vars RETRIEVAL_BACKEND=vertex,... --set-secrets BOSON_API_KEY=boson-api-key:latest
  --add-volume name=data,type=cloud-storage,bucket=frederick-talkback-data --add-volume-mount volume=data,mount-path=/app/data
  --min-instances 0 --max-instances 2 --allow-unauthenticated` (plus `TALKBACK_ACCESS_TOKEN` from a secret, since
  the service is public). Before deploying, the script syncs `data/books` and `data/portraits` to the bucket (`gcloud storage rsync`), since the whole `data/` directory is the bucket mount on Cloud Run. The script must refuse to run unless `--yes` is passed, and print what it will do.
- CORS: allow origins from a `CORS_ORIGINS` setting (comma-separated, default `http://localhost:5173`) instead
  of the hard-coded list in `main.py`.
- `docs/DEPLOY.md`: replace/extend with the Cloud Run steps, the access-token note, and cost guardrails
  (min-instances 0, max 2).
- Tests: CORS setting parsed (one small backend test). Do NOT run the deploy script.
