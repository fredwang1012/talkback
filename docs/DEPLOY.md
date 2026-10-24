# Deploying Talkback

One container serves everything: the FastAPI API, cached audio (`/media`), portraits, and the built frontend (the
backend mounts `frontend/dist` at `/` when it exists). Same origin, so the browser needs no CORS setup.

Status: the Dockerfile and `scripts/deploy_cloudrun.sh` are written but **not built or run yet**. Treat the first
build and deploy as part of the work.

## Cloud Run (GCP project `frederick-talkback`, region `us-central1`)

Already in place: Artifact Registry repo `talkback`, service account `talkback-api@frederick-talkback.iam.gserviceaccount.com`
with its roles, bucket `gs://frederick-talkback-data`.

1. Create the two secrets (once). The script checks for them and stops, printing these commands, if either is missing:
   ```
   printf %s "$BOSON_API_KEY" | gcloud secrets create boson-api-key --data-file=- --project frederick-talkback
   export TALKBACK_ACCESS_TOKEN=$(openssl rand -hex 16)
   printf %s "$TALKBACK_ACCESS_TOKEN" | gcloud secrets create talkback-access-token --data-file=- --project frederick-talkback
   ```
2. `scripts/deploy_cloudrun.sh` prints its plan and does nothing; `scripts/deploy_cloudrun.sh --yes` runs it:
   - refuses a dirty git tree unless `--allow-dirty` is passed;
   - syncs `data/books` (skipping `u-*` user uploads) to the bucket with `gcloud storage rsync`. Portraits,
     `data/uploads` and `data/samples` are never synced and nothing is deleted from the bucket (it also holds books
     uploaded via the deployed app). Portraits are personal: upload any you want public by hand, e.g.
     `gcloud storage cp data/portraits/<file> gs://frederick-talkback-data/portraits/`;
   - builds `us-central1-docker.pkg.dev/frederick-talkback/talkback/api:<git short sha>` with Cloud Build;
   - deploys service `talkback` with `RETRIEVAL_BACKEND=vertex`, the secrets as `BOSON_API_KEY` and
     `TALKBACK_ACCESS_TOKEN`, and the bucket mounted at `/app/data` (the whole data dir).
3. Health check path: `/api/health`.

The image contains no `data/`, `.env`, logs, `node_modules` or `.venv` (see `.dockerignore`) and no torch: the `embed`
extra is not installed because Vertex AI does the embedding and search.

### Access token

The service is public (`--allow-unauthenticated`), so endpoints that spend credit (voices, `/api/answer`, cloning,
upload) require header `X-Talkback-Token` matching the `TALKBACK_ACCESS_TOKEN` secret. Visitors type the code into the
page. Share the code only with people you want spending your credit. The setting reads the env var
`TALKBACK_ACCESS_TOKEN` (plain `ACCESS_TOKEN` also works).

### Per-IP rate limits behind Cloud Run

Behind Cloud Run every request's socket peer is a Google front end, which would put all visitors in one rate-limit
bucket. The service sets `TRUST_PROXY=true`, and `client_ip` then uses the **right-most** `X-Forwarded-For` entry,
which is the one Cloud Run's front end appends. Entries to its left are client-supplied and ignored, so spoofing the
header does not dodge the limits. uvicorn's `--proxy-headers` is deliberately not used (it trusts the leftmost entry).
Only set `TRUST_PROXY` when a trusted proxy is the sole ingress. Buckets keep at most 10,000 keys (oldest evicted).
`/api/retrieve` is gated by the access token and limited to 30/min per IP because it calls paid Vertex services.

### CORS

`CORS_ORIGINS` (comma-separated, default `http://localhost:5173`) lists browser origins allowed to call the API. The
deployed UI is same-origin, so the script leaves it at the default; add origins only for a separately hosted frontend.

### Public deployments

- `TALKBACK_ACCESS_TOKEN` must be set for any public deployment. Without it the paid endpoints (speech, answers, upload)
  are open to anyone.
- `GET /api/books/{id}` returns the full text of any book, including uploaded ones, to anyone who can reach the service
  (it is not behind the access code). Do not upload copyrighted books to a public deployment.

### Cost guardrails

- `--min-instances 0`: no cost when idle (cold start applies).
- `--max-instances 1`: upload status, the one-ingest-at-a-time guard and the rate-limit buckets live in process memory, so
  a second instance would split them (a status poll could hit an instance that does not know the upload). Keep it at 1.
- `--no-cpu-throttling`: PDF ingest runs in a background task after the `202` response; with the default request-only CPU
  the instance would be starved once the response is sent. The cost: an instance is billed for CPU while it is warm,
  not just while serving requests. With min-instances 0 it still idles out and stops billing.
- `--memory 1Gi --cpu 1 --timeout 300`.
- Set a monthly spend cap in the Boson workspace and a budget alert on the GCP project.
- Keep the access token set. Rate limits (token mint 10/min, TTS cache misses 120/min, clone 5/min, answer 20/min) are
  per IP.
- Demo mode (`?demo=1`) serves pre-rendered audio only and never calls Boson for speech.

## Other hosts (Railway, Fly, Render)

Build from the Dockerfile with variables `BOSON_API_KEY` and `TALKBACK_ACCESS_TOKEN`, and a volume at `/app/data`
(the image has no data in it). Without Vertex configured, leave `RETRIEVAL_BACKEND` unset (BM25 only, since the image
has no embed extra). Add `--extra embed` to the Dockerfile's `uv sync` for local multilingual embeddings at the cost
of a much larger image (torch).
