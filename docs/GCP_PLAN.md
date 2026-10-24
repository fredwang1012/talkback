# Talkback on GCP: plan and to-dos

Goal: move retrieval (RAG) and the answer model onto Google Cloud. Boson stays the voice
(Realtime for listening, TTS 3 for speaking, Avatar for faces). The Anthropic key is not used
in this plan.

## Budget

- Test round: **$50 hard limit** on project `frederick-talkback` (billing account
  "Billing Account for Education", `01DD69-A66B2B-3E4B37`).
- Budget "talkback test round" exists with email alerts at 25/50/80/100 %.
- A GCP budget only alerts; it does not stop spending. The hard stop is to-do G3 below.
- Later: up to ~$800 across accounts for the real project.

Expected spend for the test round is a few dollars:

| Item | Rough cost |
|---|---|
| Gemini 2.5 Flash answers (~1k tokens in, ~150 out) | ~$0.0007 per question |
| gemini-embedding-001, one short novel chapter set (~10k words) | < $0.01 |
| Vector Search 2.0 collection, KNN, usage-based | pennies at this size |
| Cloud Run, scale to zero | free tier covers a demo |
| Cloud Storage, a few hundred MB | < $0.05/month |
| Artifact Registry images | < $0.10/month |

Things that could blow the budget, and are not used: ANN index deployments (always-on VMs),
Cloud Run min-instances left on for days, the RAG Engine LLM parser on large PDF sets.

## Architecture

```
 Browser (localhost:5173 now, Cloud Run URL later)
   │  mic audio ─────────────► Boson Realtime (STT only: higgs-stt-3.1, ephemeral key)
   │  transcript
   ▼
 Talkback API  (FastAPI container on Cloud Run, us-central1, service account talkback-api)
   ├─ POST /api/answer  ──► 1. embed question     (Vertex gemini-embedding-001, 768-d, RETRIEVAL_QUERY)
   │                        2. hybrid search      (Vector Search 2.0 "chunks": vector + text, RRF)
   │                           filter book_id == B AND para_end <= reader_para   ◄── spoiler gate
   │                        3. answer             (Vertex Gemini 2.5 Flash, in character, grounded)
   │                        4. speak              (Boson TTS 3, character voice, sentence by sentence)
   ├─ POST /api/uploads ──► signed URL; browser PUTs the PDF straight to the bucket
   ├─ POST /api/books/ingest ─► PDF → paragraphs → chunks → embeddings → upsert into "chunks"
   ├─ /api/tts, /media     ──► Boson TTS 3, cached in the bucket
   └─ secrets: BOSON_API_KEY from Secret Manager
 Cloud Storage  gs://frederick-talkback-data   (mounted at /app/data/cache via Cloud Run volume)
 Artifact Registry  us-central1-docker.pkg.dev/frederick-talkback/talkback
```

### Decisions

1. **Retrieval: Vector Search 2.0 (Agent Retrieval) direct, KNN, no index.**
   - Spoiler gate is a numeric filter in the query itself:
     `{"$and":[{"book_id":{"$eq":B}},{"para_end":{"$lte":P}}]}`.
     This keeps today's paragraph-level rule; chapter-level gating would be weaker.
   - Hybrid: vector search plus text search, merged with Reciprocal Rank Fusion, so names
     like "Hagrid" match exactly.
   - We embed ourselves with `gemini-embedding-001` (768-d, verified working) instead of
     auto-embeddings, because filtered vector search is the documented path.
   - Not chosen: RAG Engine (fixed-size chunks with no idea of paragraphs, so spoiler gating
     is awkward; preview, us-central1 only). Firestore, pgvector, old Vector Search: no.
2. **Answer model: Gemini 2.5 Flash on Vertex** (verified working; `gemini-2.5-flash-lite`
   also works and is the cheaper fallback). `gemini-3-flash-preview` responds too, but
   preview models can change; try it only if 2.5 Flash answers are weak.
   - Thinking budget set low (or 0) to keep first-token latency down.
   - Gemini gets: persona, the gated passages, the story-so-far summary, the question.
     It returns the final spoken answer text (≤ 3 sentences).
3. **Voice of the answer: Boson TTS 3** with the character's voice, streamed sentence by
   sentence as Gemini produces them. Exact words, so what was checked is what is spoken.
   Boson Realtime is used for listening (STT). The current path (Realtime answers from
   passages in its instructions) stays behind a flag as a fallback.
4. **Frontend: stays local for development.** Mic access works on `localhost`. The API on
   Cloud Run allows the `http://localhost:5173` origin. For the demo, the same container
   serves the built frontend over HTTPS on Cloud Run, which mic access needs off localhost.
5. **PDF upload from the local UI: yes.** Browser asks the API for a signed URL, PUTs the PDF
   to the bucket, then calls ingest and polls status. Large files never pass through
   Cloud Run. Ingest uses PyMuPDF for text, our paragraph splitter, and our chunker.
6. **Storage: Cloud Storage volume mount.** `data/` (book.json, TTS cache, portraits) lives
   in the bucket, mounted into Cloud Run, so the backend keeps reading files as today.
7. **Region: us-central1** for everything (Vector Search 2.0, Gemini, Cloud Run, bucket).

### Latency budget (talk mode, target < 2 s to first audio)

| Step | Target |
|---|---|
| Release button → transcript (Boson STT) | 0.3–0.6 s |
| Embed + hybrid search | 0.2–0.4 s |
| Gemini Flash first sentence | 0.4–0.8 s |
| Boson TTS first audio | 0.3–0.6 s |

If it runs over: thinking budget 0, Flash-Lite, start TTS on the first sentence, keep one
Cloud Run instance warm only during the demo.

## Done (2026-10-17)

- [x] Project `frederick-talkback` on the Education billing account.
- [x] APIs enabled: Cloud Run, Artifact Registry, Cloud Build, Vertex AI, Vector Search,
      Cloud Storage, Secret Manager, IAM Credentials, Billing Budgets.
- [x] $50 budget with alerts at 25/50/80/100 %.
- [x] Bucket `gs://frederick-talkback-data` (us-central1, uniform access, no public access).
- [x] Artifact Registry repo `talkback` (docker, us-central1).
- [x] Vector Search 2.0 collection `chunks` (us-central1): fields `book_id, chunk_id, section,
      para_start, para_end, speaker, text`; vector `embedding` (dense, 768).
- [x] Smoke tests: `gemini-2.5-flash-lite` answers; `gemini-2.5-flash` and
      `gemini-3-flash-preview` respond; `gemini-embedding-001` returns 768-d vectors.

## To-do

### You (needs your login or approval)

- [x] **G1** `gcloud auth application-default login` so the local backend can call Vertex.
- [x] **G2** Service account and roles (blocked for Claude by the permission check; run it
      yourself or allow it):
      ```powershell
      $P='frederick-talkback'; $SA="talkback-api@$P.iam.gserviceaccount.com"
      gcloud iam service-accounts create talkback-api --display-name="Talkback API" --project $P
      foreach ($r in 'roles/aiplatform.user','roles/vectorsearch.admin','roles/secretmanager.secretAccessor','roles/logging.logWriter') {
        gcloud projects add-iam-policy-binding $P --member="serviceAccount:$SA" --role=$r --condition=None }
      gcloud storage buckets add-iam-policy-binding gs://$P-data --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
      gcloud iam service-accounts add-iam-policy-binding $SA --member="serviceAccount:$SA" --role=roles/iam.serviceAccountTokenCreator
      ```
      (`roles/vectorsearch.admin` is the expected role name; check with
      `gcloud iam roles list --filter="name:vectorsearch"` if it fails.)
- [~] **G3** Hard stop at $50: the budget publishes to Pub/Sub topic `budget-alerts`; the Cloud Run
      function `budget-stop` (code in `infra/budget_stop/`, service account `budget-stop@`) unlinks billing
      when cost >= budget. Deployed in DRY_RUN mode. Remaining (you, the permission check blocks Claude):
      ```bash
      gcloud projects add-iam-policy-binding frederick-talkback         --member=serviceAccount:budget-stop@frederick-talkback.iam.gserviceaccount.com --role=roles/billing.projectManager
      gcloud pubsub topics publish budget-alerts --project frederick-talkback         --message='{"costAmount":51,"budgetAmount":50,"currencyCode":"CAD"}'   # dry run: logs "would disable billing"
      gcloud run services update budget-stop --region us-central1 --project frederick-talkback --update-env-vars DRY_RUN=false
      ```
- [x] **G4** Secret Manager: `boson-api-key` (from `.env`) and `talkback-access-token` (the code is in the
      gitignored `.talkback-access-code`).

### Code (Claude, next)

- [x] **C1** `backend/app/gcp/`: `embed.py` (gemini-embedding-001), `vectorstore.py`
      (Vector Search 2.0 search + batch upsert), `llm.py` (Gemini via `google-genai`,
      `vertexai=True`). Settings: `GCP_PROJECT`, `GCP_REGION`, `RETRIEVAL_BACKEND=local|vertex`.
- [x] **C2** `scripts/index_vertex.py`: push existing `chunks` for a book into the collection.
- [x] **C3** Retrieval switch: `/api/retrieve` uses Vector Search when `RETRIEVAL_BACKEND=vertex`.
      Spoiler property tests run against both backends (fake client for Vertex).
- [x] **C4** `POST /api/answer`: retrieve → Gemini → streamed sentences. Frontend Talk mode
      plays them through Boson TTS in the character voice; old path behind a flag.
- [x] **C5** Upload: `/api/uploads` (signed URL), `/api/books/ingest` (PyMuPDF → book.json →
      chunks → embed → upsert), status polling; Library page "Add a book" button.
- [x] **C6** Deploy: Cloud Build → Artifact Registry → Cloud Run with the bucket volume,
      Secret Manager env, CORS for localhost, max-instances 2, min-instances 0.
- [x] **C7** Cost log: every Gemini/embedding call logs tokens; `scripts/usage.py` totals
      them so spend is visible between billing updates (billing data lags up to a day).

### Later (real project, ~$800)

- [x] Faces in talk mode: speaking/idle loops (instant) and optional lip-synced clips (~10 s per sentence).
- [ ] Lip-sync via `/v1/videos/stream` if it gets fast enough to keep up with speech.
- [ ] Re-evaluate Gemini 3 Flash when it leaves preview.
- [ ] Separate dev and demo projects so test spending never touches the demo.
