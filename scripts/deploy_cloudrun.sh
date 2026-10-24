#!/usr/bin/env bash
# Build the image with Cloud Build and deploy it to Cloud Run. See docs/DEPLOY.md.
# Refuses to do anything without --yes. Never reads .env and never prints a secret value.
set -euo pipefail

PROJECT=frederick-talkback
REGION=us-central1
SERVICE=talkback
BUCKET=frederick-talkback-data
SA="talkback-api@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/talkback"

cd "$(dirname "$0")/.."
TAG="$(git rev-parse --short HEAD)"
IMAGE="${REPO}/api:${TAG}"

if [[ "${1:-}" != "--yes" ]]; then
  cat <<EOF
This script deploys Talkback to Cloud Run. Nothing has been done yet. It will:
  1. Check that the Secret Manager secrets boson-api-key and talkback-access-token exist (stops with the create command if not).
  2. Sync public data to gs://${BUCKET}:
       data/books     -> gs://${BUCKET}/books      (skipping u-* user uploads)
       data/portraits -> gs://${BUCKET}/portraits  (top-level files only; per-person subfolders skipped)
     data/uploads and data/samples are never synced. Nothing is deleted from the bucket.
  3. gcloud builds submit --tag ${IMAGE}
  4. gcloud run deploy ${SERVICE} in ${REGION} as ${SA}
       RETRIEVAL_BACKEND=vertex, bucket mounted at /app/data, min 0 / max 2 instances, 1Gi, 1 CPU, 300 s timeout,
       --allow-unauthenticated (paid endpoints are gated by the TALKBACK_ACCESS_TOKEN secret)
Re-run with --yes to proceed.
EOF
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
if ! gcloud secrets describe talkback-access-token --project "${PROJECT}" >/dev/null 2>&1; then
  missing=1
  echo "Secret talkback-access-token does not exist. The service is public, so paid endpoints need this gate."
  echo "Generate a code and create the secret (keep the code to share with visitors):"
  echo "  export TALKBACK_ACCESS_TOKEN=\$(openssl rand -hex 16)"
  echo "  printf %s \"\$TALKBACK_ACCESS_TOKEN\" | gcloud secrets create talkback-access-token --data-file=- --project ${PROJECT}"
fi
if [[ "${missing}" -ne 0 ]]; then
  echo "Create the missing secret(s) above, then re-run with --yes." >&2
  exit 1
fi

# 2. Sync public data. The whole data/ dir is the bucket mount on Cloud Run.
#    --exclude is a regex on the path relative to the source. No --delete-unmatched-destination-objects:
#    the bucket also holds books uploaded through the deployed app.
gcloud storage rsync --recursive --exclude='^u-.*' "data/books" "gs://${BUCKET}/books"
gcloud storage rsync --recursive --exclude='.*/.*' "data/portraits" "gs://${BUCKET}/portraits"

# 3. Build.
gcloud builds submit --project "${PROJECT}" --tag "${IMAGE}" .

# 4. Deploy.
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SA}" \
  --set-env-vars "RETRIEVAL_BACKEND=vertex,GCP_PROJECT=${PROJECT},GCP_REGION=${REGION},CORS_ORIGINS=http://localhost:5173" \
  --set-secrets "BOSON_API_KEY=boson-api-key:latest,TALKBACK_ACCESS_TOKEN=talkback-access-token:latest" \
  --execution-environment gen2 \
  --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount "volume=data,mount-path=/app/data" \
  --min-instances 0 --max-instances 2 \
  --memory 1Gi --cpu 1 --timeout 300 \
  --allow-unauthenticated
