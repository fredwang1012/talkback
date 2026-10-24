# Deploying Talkback

You do not need any of this to run Talkback locally (see the README Quickstart). This is how the author deployed it. The
project id `frederick-talkback`, the bucket `frederick-talkback-data` and the service account below are the author's own
GCP resources and are baked into `scripts/deploy_cloudrun.sh` as constants; substitute your own project, region and bucket
(edit the constants at the top of the script) before deploying anywhere else.

One container serves everything (entry point `app.asgi:app`, which also selects the public processing API below when `PUBLIC_API=true`): the FastAPI API, cached audio (`/media`), portraits, and the built frontend (the
backend mounts `frontend/dist` at `/` when it exists). Same origin, so the browser needs no CORS setup.

Status: the Dockerfile and `scripts/deploy_cloudrun.sh` are written but **not built or run yet**. Treat the first
build and deploy as part of the work.

## Cloud Run (GCP project `frederick-talkback`, region `us-central1`)

Already in place: Artifact Registry repo `talkback`, service account `talkback-api@frederick-talkback.iam.gserviceaccount.com`
with its roles, bucket `gs://frederick-talkback-data`.

1. Create the three secrets (once). The script checks for them and stops, printing these commands, if any is missing:
   ```
   printf %s "$BOSON_API_KEY" | gcloud secrets create boson-api-key --data-file=- --project frederick-talkback
   printf %s "$ANTHROPIC_API_KEY" | gcloud secrets create anthropic-api-key --data-file=- --project frederick-talkback
   export TALKBACK_ACCESS_TOKEN=$(openssl rand -hex 16)
   printf %s "$TALKBACK_ACCESS_TOKEN" | gcloud secrets create talkback-access-token --data-file=- --project frederick-talkback
   ```
2. `scripts/deploy_cloudrun.sh` prints its plan and does nothing; `scripts/deploy_cloudrun.sh --yes` runs it:
   - refuses a dirty git tree unless `--allow-dirty` is passed;
   - syncs `data/books` (skipping `u-*` user uploads and every `cast_settings.json`, so settings saved on the live site survive) to the bucket with `gcloud storage rsync`. Portraits,
     `data/uploads` and `data/samples` are never synced and nothing is deleted from the bucket (it also holds books
     uploaded via the deployed app). Portraits are personal: upload any you want public by hand, e.g.
     `gcloud storage cp data/portraits/<file> gs://frederick-talkback-data/portraits/`;
   - builds `us-central1-docker.pkg.dev/frederick-talkback/talkback/api:<git short sha>` with Cloud Build;
   - deploys service `talkback` with `RETRIEVAL_BACKEND=vertex`, the secrets as `BOSON_API_KEY`, `ANTHROPIC_API_KEY` and
     `TALKBACK_ACCESS_TOKEN`, the env vars `UPLOAD_LLM=claude`, `UPLOAD_ENRICH=true`, `UPLOAD_CONFIRM=false`,
     `UPLOAD_BOOK_CAP_USD=4`, `UPLOAD_TOTAL_CAP_USD=15` and `USAGE_LOG_FILE=false` (usage lines go to Cloud Logging
     only; appending to `usage.jsonl` on the gcsfuse mount rewrites the whole object each time), and the bucket mounted at `/app/data` (the whole data dir).
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

## Access control (Google sign-in via IAP)
The `talkback` service is private: Cloud Run IAP with a custom OAuth client (personal Gmail projects have no organization, so the
Google-managed client cannot be used). The old `TALKBACK_ACCESS_TOKEN` code is no longer deployed.
- Allow someone (they must also be a test user on the OAuth consent screen while it is in Testing):
  `gcloud beta iap web add-iam-policy-binding --member=user:EMAIL --role=roles/iap.httpsResourceAccessor --resource-type=cloud-run --service=talkback --region=us-central1 --project=frederick-talkback`
- Remove: same command with `remove-iam-policy-binding`.
- The OAuth client JSON lives in the gitignored `.iap-client.json`; the client needs the redirect URI
  `https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect`.
- Turn the gate off (e.g. for a public demo): `gcloud beta run services update talkback --region us-central1 --project frederick-talkback --no-iap` and add `allUsers` as `roles/run.invoker`; on again with `--iap` and remove `allUsers`.

## Per-account data (`PER_USER`)
The deploy script sets `PER_USER=true`, `IAP_AUDIENCE=/projects/272822965835/locations/us-central1/services/talkback`,
`OWNER_EMAILS=fredwang1012@gmail.com` and `OWNER_ONLY_BOOKS=hp*`. Local runs leave `PER_USER` unset and keep one shared
`data/` namespace exactly as before.
- The user is the email in the IAP JWT (`x-goog-iap-jwt-assertion`), verified against Google's IAP public keys and the
  audience above. The plain `x-goog-authenticated-user-email` header is ignored. No IAP header at all (for example when the
  gate is switched off for a demo) means the shared anonymous namespace `public`; a header that fails verification gets 401.
  The server refuses to start with `PER_USER=true` and no `IAP_AUDIENCE`. Lip-sync clips are per account too
  (`data/users/<hash>/lipsync/`, served at `/media/avatar/` only to their owner).
- Per account, under `data/users/<sha256(email)[:16]>/` (anonymous: `data/users/public/`): `voices.json`, `faces.json`,
  `faces/` (served only to their owner at `/portraits/faces/...`), `cast/<book>.json`. Uploaded books stay in
  `data/books/u-*` with an `owner.txt` holding the owner's namespace; other accounts get 404 for them. Built-in books are shared,
  except ids matching `OWNER_ONLY_BOOKS`, which only `OWNER_EMAILS` can list or open. Spend caps stay global; rate limits are per account.
- Sign out: the library header links to `/?gcp-iap-mode=CLEAR_LOGIN_COOKIE` (IAP's logout).
- Migration: on the first start with `PER_USER=true` the shared `voices.json`, `faces.json`, `portraits/faces/*` and every
  `cast_settings.json` are copied to the first owner's namespace, and `u-*` uploads without an owner get the owner.
  It only copies files that are missing, so it is idempotent, never deletes the originals and never overwrites later edits;
  failures are logged, not fatal. To preview or run it by hand against a copy of the bucket:
  `uv run --project backend python scripts/migrate_per_user.py --data-dir <dir>` (dry run) and add `--apply`.
- Check after deploying: sign in as the owner (cloned narrator and face present, `hp*` books listed), then as another
  allowed account (neither visible, only the public sample books).

## Public processing API (`talkback-public`)

A second, much smaller Cloud Run service from the same image. It lets a fresh clone that has only a `BOSON_API_KEY` get
full upload processing (main characters, dialogue split, recap notes) and Gemini-written typed answers, on the author's
GCP project. With `PUBLIC_API=true` the container (entry point `app.asgi:app`) serves **only** `GET /api/health`,
`POST /api/public/enrich` (+ `GET /api/public/enrich/{job_id}` to poll), and `POST /api/public/answer`. Every other path
is 404: no frontend, no media, no portraits, no book data. It uses Gemini only (`UPLOAD_LLM=gemini`), holds results in
memory for 30 minutes, never logs request bodies (they may contain book text), and never decides what a reader may see:
the local app sends already spoiler-filtered passages.

Limits, all enforced by the service:
- Per book: estimate at most $1.5 (`PUBLIC_BOOK_CAP_USD`); a running job also stops at 1.25 times that.
- Cumulative enrichment spend at most $10 (`PUBLIC_TOTAL_CAP_USD`, persisted). Admission counts the unspent estimates of
  running jobs, so concurrent jobs cannot overshoot it.
- Per client address: 1 book per hour, and at most $2 of enrichment estimates per UTC day (`PUBLIC_IP_DAILY_CAP_USD`,
  persisted; addresses are stored as short hashes). The estimate is charged at admission and settled to real spend at the end.
- Gemini answers at most $2 per UTC day (`PUBLIC_ANSWER_DAILY_CAP_USD`, persisted). Each answer reserves a worst-case
  cost (estimated input plus 700 output tokens) at admission and is settled to the real cost afterwards; a stream that
  fails or is dropped before Gemini reports usage is charged the reservation. At most 4 answer streams run at once.
- 20 answers per IP per minute, 3 MB of book text (6,000 paragraphs), two enrichment jobs at a time. Results are kept 30
  minutes after the job finishes.
A cap answers `402` and a rate limit or full slot `429`, each with `{"detail", "code"}`. Counters live in the bucket's
`usage/` folder: `upload_spend.json` (cumulative enrichment), `public_answer_spend.json` (answers, today),
`public_ip_spend.json` (per address, today). To reset one, delete that file
(`gcloud storage rm gs://frederick-talkback-public/usage/<file>`) and restart the instance by redeploying or waiting
for it to idle out (counters are also cached in memory). To change a cap, redeploy with a different env value.

### One-time setup

```
gcloud iam service-accounts create talkback-public --display-name "Talkback public API" --project frederick-talkback
gcloud projects add-iam-policy-binding frederick-talkback --member serviceAccount:talkback-public@frederick-talkback.iam.gserviceaccount.com --role roles/aiplatform.user
gcloud storage buckets create gs://frederick-talkback-public --project frederick-talkback --location us-central1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://frederick-talkback-public --member serviceAccount:talkback-public@frederick-talkback.iam.gserviceaccount.com --role roles/storage.objectAdmin
```

The service account has no other role: no Secret Manager, no access to the `talkback` data bucket. The service holds no secrets.

### Deploy (ships CLOSED)

`scripts/deploy_public_api.sh` prints its plan; `scripts/deploy_public_api.sh --yes` builds the image if this commit has none,
deploys `talkback-public` with `--no-allow-unauthenticated` (min 0 / max 1 instance, no CPU throttling so a job keeps
running between polls, 120 s request timeout) and prints the service URL. A closed service answers `403` to everyone
without a Google identity token, which the local app treats as "processing service unavailable". Put the printed URL in
`.env.example` as `PUBLIC_API_URL`.

### Open and close it

```
# open: anyone can call it (the caps above bound the spend)
gcloud run services add-iam-policy-binding talkback-public --project frederick-talkback --region us-central1 --member=allUsers --role=roles/run.invoker
# close it again
gcloud run services remove-iam-policy-binding talkback-public --project frederick-talkback --region us-central1 --member=allUsers --role=roles/run.invoker
```

Check from a shell: `curl -s -o /dev/null -w "%{http_code}\n" <URL>/api/health` is `403` when closed, `200` when open.
Closing is the kill switch; deleting the service removes it entirely. Set a GCP budget alert on the project as well.

### How the local app uses it

`PUBLIC_API_URL` (set in `.env.example`, empty in code) is used only when the machine has no LLM of its own (no
`ANTHROPIC_API_KEY`, no usable GCP credentials). Upload processing then runs through the service and fills the existing
progress status; if the service is closed, unreachable or capped, the upload falls back to the simple local cast with a
message saying which. `/api/answer` retrieves and spoiler-filters locally, then asks the service only to write the answer;
`/api/health` reports `answer_backend: "gemini"` while the service answers its health check (probed at most once a minute)
and `"realtime"` otherwise, so typed questions fall back to Boson Realtime when it is closed.
