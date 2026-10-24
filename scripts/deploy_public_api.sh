#!/usr/bin/env bash
# Build the image (if this commit has none yet) and deploy the PUBLIC PROCESSING API to Cloud Run as the separate
# service `talkback-public`. See docs/DEPLOY.md. Refuses to do anything without --yes. Never reads .env, never prints a
# secret. The service ships CLOSED (--no-allow-unauthenticated): a fresh clone gets 403 until you open it (docs/DEPLOY.md).
set -euo pipefail
# Git Bash on Windows rewrites "...=/app/data" arguments into Windows paths; exempt only the volume-mount argument
# (turning conversion off everywhere breaks gcloud.cmd itself). Ignored on Linux and macOS.
export MSYS2_ARG_CONV_EXCL="volume="

PROJECT=frederick-talkback
REGION=us-central1
SERVICE=talkback-public
BUCKET=frederick-talkback-public          # tiny bucket: only the persisted spend counters live here
SA="talkback-public@${PROJECT}.iam.gserviceaccount.com"
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
This script deploys the public processing API to Cloud Run. Nothing has been done yet. It will:
  1. Refuse to continue if the git tree is dirty (unless --allow-dirty), so the image tag matches the code.
  2. Check that the service account ${SA} and the bucket gs://${BUCKET} exist (stops and prints the create
     commands if not; docs/DEPLOY.md lists them with the IAM roles: aiplatform.user on the project, objectAdmin on that bucket only).
  3. Build ${IMAGE} with Cloud Build unless that tag already exists (it is the same image as the main service).
  4. gcloud run deploy ${SERVICE} in ${REGION} as ${SA}
       PUBLIC_API=true (only /api/health and /api/public/*), UPLOAD_LLM=gemini, TRUST_PROXY=true, USAGE_LOG_FILE=false,
       PUBLIC_BOOK_CAP_USD=1.5, PUBLIC_TOTAL_CAP_USD=10, PUBLIC_IP_DAILY_CAP_USD=2, PUBLIC_ANSWER_DAILY_CAP_USD=2, no secrets,
       bucket mounted at /app/data (spend counters), min 0 / max 1 instance, no CPU throttling (jobs run between polls),
       1Gi, 1 CPU, 120 s request timeout (jobs are polled, no long request),
       --no-allow-unauthenticated: CLOSED. Nobody can call it until you grant allUsers roles/run.invoker.
Re-run with --yes to proceed.
EOF
  exit 1
fi

if [[ "${ALLOW_DIRTY}" -ne 1 && -n "$(git status --porcelain)" ]]; then
  echo "Git tree is dirty; commit or stash first, or pass --allow-dirty (the image tag ${TAG} will not match the code)." >&2
  exit 1
fi

echo "Plan: build ${IMAGE} if needed, deploy ${SERVICE} (closed) to ${REGION} (project ${PROJECT})"

# 1. The service account and the bucket must exist before the deploy references them.
missing=0
if ! gcloud iam service-accounts describe "${SA}" --project "${PROJECT}" >/dev/null 2>&1; then
  missing=1
  echo "Service account ${SA} does not exist. Create it and grant only what it needs:"
  echo "  gcloud iam service-accounts create talkback-public --display-name 'Talkback public API' --project ${PROJECT}"
  echo "  gcloud projects add-iam-policy-binding ${PROJECT} --member serviceAccount:${SA} --role roles/aiplatform.user"
fi
if ! gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT}" >/dev/null 2>&1; then
  missing=1
  echo "Bucket gs://${BUCKET} does not exist. Create it and give the service account access to this bucket only:"
  echo "  gcloud storage buckets create gs://${BUCKET} --project ${PROJECT} --location ${REGION} --uniform-bucket-level-access"
  echo "  gcloud storage buckets add-iam-policy-binding gs://${BUCKET} --member serviceAccount:${SA} --role roles/storage.objectAdmin"
fi
if [[ "${missing}" -ne 0 ]]; then
  echo "Create the missing resource(s) above, then re-run with --yes." >&2
  exit 1
fi

# 2. Build (the image is the same one the main service uses; skip when this commit was already built).
if gcloud artifacts docker images describe "${IMAGE}" --project "${PROJECT}" >/dev/null 2>&1; then
  echo "Image ${IMAGE} already exists; not rebuilding."
else
  gcloud builds submit --project "${PROJECT}" --tag "${IMAGE}" .
fi

# 3. Deploy, closed.
gcloud run deploy "${SERVICE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --service-account "${SA}" \
  --set-env-vars "PUBLIC_API=true,TRUST_PROXY=true,UPLOAD_LLM=gemini,GCP_PROJECT=${PROJECT},GCP_REGION=${REGION},USAGE_LOG_FILE=false,PUBLIC_BOOK_CAP_USD=1.5,PUBLIC_TOTAL_CAP_USD=10,PUBLIC_IP_DAILY_CAP_USD=2,PUBLIC_ANSWER_DAILY_CAP_USD=2" \
  --execution-environment gen2 \
  --add-volume "name=data,type=cloud-storage,bucket=${BUCKET}" \
  --add-volume-mount "volume=data,mount-path=/app/data" \
  --min-instances 0 --max-instances 1 --no-cpu-throttling \
  --memory 1Gi --cpu 1 --timeout 120 \
  --no-allow-unauthenticated

echo
echo "Deployed ${SERVICE} CLOSED. URL (put it in .env.example as PUBLIC_API_URL):"
gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" --format 'value(status.url)'
echo "Open it:  gcloud run services add-iam-policy-binding ${SERVICE} --project ${PROJECT} --region ${REGION} --member=allUsers --role=roles/run.invoker"
echo "Close it: gcloud run services remove-iam-policy-binding ${SERVICE} --project ${PROJECT} --region ${REGION} --member=allUsers --role=roles/run.invoker"
