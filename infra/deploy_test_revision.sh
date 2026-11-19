#!/usr/bin/env bash
# Test deploy of the SourcingAI API onto sourcing-ai-dev as a revision with NO traffic
# (2026-11-19). The vendor login page (mie-agent sourcing_credentials_web) keeps 100%
# of the service's traffic; ours is reached only through its tag address:
#   https://sourcing---sourcing-ai-dev-490776059622.us-central1.run.app
# IAP covers the tag address too (same service).
#
#   infra/deploy_test_revision.sh            dummy worker (no vendor portal touched)
#   infra/deploy_test_revision.sh --real     real searches (only after the client has the code)
#
# The worker runs inside the service (TRIGGER_RUN_LOCAL=1): we can deploy the service but
# can't create a Cloud Run job. CPU stays on after a response (--no-cpu-throttling) so the
# worker keeps running; fine for a test, a job is the way for real use.
#
# Never --set-env-vars / --set-secrets: they drop what the login page needs. Note that the
# next deploy of the login page starts from the newest revision's settings (ours), so it
# inherits these extra env vars and secrets; harmless to the page.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="skylink-material-planning"
PROJECT_NUMBER="490776059622"
REGION="us-central1"
SERVICE="sourcing-ai-dev"
TAG="sourcing"
SA="sourcing-ai-dev@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/mie-dev/sourcing-ai"
# Firestore for the add-on relay lives in the add-on's own project (no billing, no secrets).
STORE_PROJECT="sourcing-skylink-addon"
ADDON_PROJECT_NUMBER="1011595872116"
# The add-on's current client (since the spreadsheets scope, 2026-11-19) and the earlier one;
# the project number covers any later one Apps Script makes.
ADDON_CLIENT_ID="1011595872116-1dnm2qrepuqh80bkg2k0kbnu3meqq1iv.apps.googleusercontent.com;1011595872116-n7pglcfpupq3o51ju3jcj00vd5q979em.apps.googleusercontent.com"

WORKER_ARGS="--dummy"
if [[ "${1:-}" == "--real" ]]; then
  WORKER_ARGS="--browser browserbase"
  : "${LLM_MODEL:?set LLM_MODEL (and LLM_EFFORT) for a real run}"
  : "${LLM_EFFORT:?set LLM_EFFORT for a real run}"
fi

# 1. Refuse a dirty tree: the revision is stamped with the commit it was built from.
if [[ -n "$(git status --porcelain -- sourcing service scripts Dockerfile requirements.txt)" ]]; then
  echo "ERROR: uncommitted changes in the code that goes into the image. Commit first." >&2
  exit 1
fi
GIT_SHA="$(git rev-parse --short HEAD)"
IMAGE="${REPO}:${GIT_SHA}"

# 2. The traffic split before: it must be the same after.
before="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" \
          --format="value(status.traffic)")"
echo "Traffic before: ${before}"

# 3. Build for Cloud Run's CPU and push to mie-dev.
docker build --platform linux/amd64 -t "${IMAGE}" .
docker push "${IMAGE}"

# 4. Deploy without traffic, reachable only by the tag.
env_vars="GCP_PROJECT=${PROJECT},GCP_REGION=${REGION},GIT_SHA=${GIT_SHA}"
env_vars+=",STORE_BACKEND=firestore,STORE_PROJECT=${STORE_PROJECT},SHEET_ACCESS=relay"
env_vars+=",IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE}"
env_vars+=",ADDON_OAUTH_CLIENT_IDS=${ADDON_CLIENT_ID},ADDON_OAUTH_PROJECT_NUMBERS=${ADDON_PROJECT_NUMBER}"
env_vars+=",ALLOWED_EMAIL_DOMAINS=skylinkintl.com"
env_vars+=",TRIGGER_RUN_LOCAL=1,TRIGGER_LOCAL_ARGS=${WORKER_ARGS}"
env_vars+=",BROWSER_BACKEND=browserbase,VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test"
if [[ "${1:-}" == "--real" ]]; then
  env_vars+=",LLM_PROVIDER=anthropic,LLM_MODEL=${LLM_MODEL},LLM_EFFORT=${LLM_EFFORT}"
fi

gcloud run deploy "${SERVICE}" \
  --image="${IMAGE}" \
  --region="${REGION}" --project="${PROJECT}" \
  --service-account="${SA}" \
  --no-traffic --tag="${TAG}" \
  --min-instances=0 --max-instances=1 \
  --memory=2Gi --cpu=2 --timeout=300s --no-cpu-throttling \
  --update-env-vars="^@^${env_vars//,/@}" \
  --update-secrets="ANTHROPIC_API_KEY=sourcing-anthropic-api-key:latest,BROWSERBASE_API_KEY=sourcing-browserbase-api-key:latest,BROWSERBASE_PROJECT_ID=sourcing-browserbase-project-id:latest,CHAT_WEBHOOK_URL=sourcing-chat-webhook-url:latest" \
  --quiet

# 5. The checks that must never be removed: IAP on, nobody public, traffic unchanged.
iap="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" \
       --format="value(metadata.annotations['run.googleapis.com/iap-enabled'])")"
[[ "${iap}" == "true" ]] || { echo "FATAL: IAP is not enabled on ${SERVICE}. Tell the Skylink admin now." >&2; exit 1; }
if gcloud run services get-iam-policy "${SERVICE}" --region="${REGION}" --project="${PROJECT}" \
     --format=json | grep -qE '"allUsers"|"allAuthenticatedUsers"'; then
  echo "FATAL: ${SERVICE} is open to the public. Tell the Skylink admin now." >&2; exit 1
fi
after="$(gcloud run services describe "${SERVICE}" --region="${REGION}" --project="${PROJECT}" \
         --format="value(status.traffic)")"
echo "Traffic after:  ${after}"
echo "IAP ok: on, not public."
echo "Test address: https://${TAG}---${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
echo "Add-on script property TRIGGER_URL = that address + /trigger"
echo "Remove the test revision's address: gcloud run services update-traffic ${SERVICE} --remove-tags=${TAG} --region=${REGION} --project=${PROJECT}"
