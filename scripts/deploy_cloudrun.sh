#!/usr/bin/env bash
# Build the image with Cloud Build and deploy it to Cloud Run. See docs/DEPLOY.md.
# Refuses to do anything without --yes. Never reads .env and never prints a secret value.
set -euo pipefail
# Git Bash on Windows rewrites "...=/app/data" arguments into Windows paths; exempt only the volume-mount argument
# (turning conversion off everywhere breaks gcloud.cmd itself). Ignored on Linux and macOS.
export MSYS2_ARG_CONV_EXCL="volume="

PROJECT=frederick-talkback
REGION=us-central1
SERVICE=talkback
BUCKET=frederick-talkback-data
PROJECT_NUMBER=272822965835
# Cloud Run IAP JWT audience: /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE (IAP signed-headers docs)
IAP_AUDIENCE="/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE}"
OWNER_EMAILS="fredwang1012@gmail.com"
SA="talkback-api@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/talkback"

cd "$(dirname "$0")/.."
TAG="$(git rev-parse --short HEAD)"
IMAGE="${REPO}/api:${TAG}"

YES=0
ALLOW_DIRTY=0
for arg in "$@"; do
  case "${arg}" in
    --yes) YES=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

if [[ "${YES}" -ne 1 ]]; then
  cat <<EOF
This script deploys Talkback to Cloud Run. Nothing has been done yet. It will:
  1. Refuse to continue if the git tree is dirty (unless --allow-dirty), so the image tag matches the code.
  2. Check that the Secret Manager secrets boson-api-key exists (stops with the create command if not).
  3. Sync public book data to gs://${BUCKET}:
       data/books     -> gs://${BUCKET}/books      (skipping u-* user uploads and cast_settings.json,
                                                      so settings saved on the live site are never overwritten)
     Portraits, data/uploads and data/samples are never synced. Nothing is deleted from the bucket.
  4. gcloud builds submit --tag ${IMAGE}
  5. gcloud run deploy ${SERVICE} in ${REGION} as ${SA}
       RETRIEVAL_BACKEND=vertex, TRUST_PROXY=true, UPLOAD_LLM=gemini, UPLOAD_ENRICH=true, UPLOAD_CONFIRM=false,
       UPLOAD_BOOK_CAP_USD, UPLOAD_TOTAL_CAP_USD (default 1000: caps lifted for the hackathon; export lower values to restore them), USAGE_LOG_FILE=false (usage goes to Cloud Logging only),
       PER_USER=true, IAP_AUDIENCE=${IAP_AUDIENCE}, OWNER_EMAILS=${OWNER_EMAILS}, OWNER_ONLY_BOOKS=hp* (per-account data;
       on first start the shared voice/face/cast data is copied to the owner, see docs/DEPLOY.md),
       secret BOSON_API_KEY, bucket mounted at /app/data, min 0 / max 1 instance, no CPU throttling, 1Gi, 1 CPU,
       300 s timeout, --no-allow-unauthenticated --iap (Google sign-in; only emails granted roles/iap.httpsResourceAccessor get in)
Re-run with --yes to proceed.
EOF
  exit 1
fi

if [[ "${ALLOW_DIRTY}" -ne 1 && -n "$(git status --porcelain)" ]]; then
  echo "Git tree is dirty; commit or stash first, or pass --allow-dirty (the image tag ${TAG} will not match the code)." >&2
  exit 1
fi

echo "Plan: sync data -> gs://${BUCKET}, build ${IMAGE}, deploy ${SERVICE} to ${REGION} (project ${PROJECT})"

# 1. Secrets must exist before the deploy references them.
missing=0
if ! gcloud secrets describe boson-api-key --project "${PROJECT}" >/dev/null 2>&1; then
  missing=1
  echo "Secret boson-api-key does not exist. Create it (with BOSON_API_KEY set in your shell, not echoed):"
  echo "  printf %s \"\$BOSON_API_KEY\" | gcloud secrets create boson-api-key --data-file=- --project ${PROJECT}"
fi
if [[ "${missing}" -ne 0 ]]; then
  echo "Create the missing secret(s) above, then re-run with --yes." >&2
  exit 1
fi

# 3. Sync public data. The whole data/ dir is the bucket mount on Cloud Run.
#    --exclude is one regex on the path relative to the source: u-* uploads, and cast_settings.json at any depth
#    (settings saved on the live site must survive a deploy). No --delete-unmatched-destination-objects:
#    the bucket also holds books uploaded through the deployed app.
gcloud storage rsync --recursive --exclude='^u-.*' --exclude='.*cast_settings[.]json$' "data/books" "gs://${BUCKET}/books"
# Portraits are not synced (per-person folders are personal); upload them by hand if wanted, see docs/DEPLOY.md.

# 3. Build.
gcloud builds submit --project "${PROJECT}" --tag "${IMAGE}" .

# 4. Deploy.
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SA}" \
  --set-env-vars "RETRIEVAL_BACKEND=vertex,TRUST_PROXY=true,GCP_PROJECT=${PROJECT},GCP_REGION=${REGION},CORS_ORIGINS=http://localhost:5173,UPLOAD_LLM=gemini,UPLOAD_ENRICH=true,UPLOAD_CONFIRM=false,UPLOAD_BOOK_CAP_USD=${UPLOAD_BOOK_CAP_USD:-1000},UPLOAD_TOTAL_CAP_USD=${UPLOAD_TOTAL_CAP_USD:-1000},GROUNDED_DAILY_CAP=${GROUNDED_DAILY_CAP:-100000},ENRICH_GEMINI_MODEL=${ENRICH_GEMINI_MODEL:-gemini-2.5-flash-lite},GEMINI_LOCATION=${GEMINI_LOCATION:-global},USAGE_LOG_FILE=false,PER_USER=true,IAP_AUDIENCE=${IAP_AUDIENCE},OWNER_EMAILS=${OWNER_EMAILS},OWNER_ONLY_BOOKS=hp*" \
  --set-secrets "BOSON_API_KEY=boson-api-key:latest" \
  --execution-environment gen2 \
  --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount "volume=data,mount-path=/app/data" \
  --min-instances 0 --max-instances 1 --no-cpu-throttling \
  --memory 1Gi --cpu 1 --timeout 300 \
  --no-allow-unauthenticated --iap
