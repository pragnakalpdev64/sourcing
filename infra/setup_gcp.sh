#!/usr/bin/env bash
# Step 0: one-time GCP setup for the SourcingAI prototype. Safe to re-run.
#
#   export PROJECT_ID=skylink-material-planning REGION=us-central1
#   cp infra/secrets.env.example infra/secrets.env   # fill in values
#   bash infra/setup_gcp.sh
#
# Runs inside Skylink's existing project (billing is already set up there).
# Everything except the "Infra secrets" section needs project IAM admin rights,
# so a Skylink admin runs it; the secrets section only needs the `sourcing-*`
# secret access Skylink granted.
# Not automated here: Browserbase plan, Google Chat space + webhook.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
REGION="${REGION:-us-central1}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-nam5}"
REPO=sourcing
RUNTIME_SA="sourcing-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
BUILD_SA="sourcing-build@${PROJECT_ID}.iam.gserviceaccount.com"
SECRETS_FILE="$(dirname "$0")/secrets.env"

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== Enabling APIs"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com \
  sheets.googleapis.com drive.googleapis.com chat.googleapis.com iam.googleapis.com

echo "== Service accounts"
for sa in sourcing-runtime sourcing-build; do
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$sa" --display-name="SourcingAI ${sa#sourcing-}"
done

bind() { gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$1" \
           --role="$2" --condition=None >/dev/null; }

# The project holds other Skylink secrets, so secret roles only cover names with
# this prefix. Secret Manager resource names use the project number.
bind_prefix() { gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$1" \
                  --role="$2" --condition="expression=resource.name.startsWith(\"projects/${PROJECT_NUMBER}/secrets/$3\"),title=$4" >/dev/null; }

# Runtime: Firestore, secrets, start the worker job from /trigger, write logs.
bind "$RUNTIME_SA" roles/datastore.user
bind_prefix "$RUNTIME_SA" roles/secretmanager.secretAccessor sourcing- sourcing-secrets-read
# New password versions from the credential update flow; creating secrets stays with admins.
bind_prefix "$RUNTIME_SA" roles/secretmanager.secretVersionAdder sourcing-vendor- sourcing-vendor-write
bind "$RUNTIME_SA" roles/run.invoker
bind "$RUNTIME_SA" roles/logging.logWriter

# Build: push images, deploy Cloud Run, act as the runtime SA, write logs.
bind "$BUILD_SA" roles/artifactregistry.writer
bind "$BUILD_SA" roles/run.admin
bind "$BUILD_SA" roles/logging.logWriter
bind "$BUILD_SA" roles/storage.objectViewer   # read the uploaded source in gs://<project>_cloudbuild
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/iam.serviceAccountUser >/dev/null

echo "== Artifact Registry"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"

echo "== Firestore (Native mode)"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 \
  || gcloud firestore databases create --location="$FIRESTORE_LOCATION" --type=firestore-native

echo "== Infra secrets"
SECRETS=(BROWSERBASE_API_KEY BROWSERBASE_PROJECT_ID ANTHROPIC_API_KEY OPENAI_API_KEY
         GEMINI_API_KEY CHAT_WEBHOOK_URL TRIGGER_SECRET GOOGLE_OAUTH_CLIENT_SECRET SESSION_SECRET)
if [[ -f "$SECRETS_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$SECRETS_FILE"; set +a
fi
for name in "${SECRETS[@]}"; do
  secret="sourcing-$(printf '%s' "$name" | tr 'A-Z_' 'a-z-')"
  value="${!name:-}"
  if [[ "$name" == TRIGGER_SECRET && -z "$value" ]]; then
    value="$(openssl rand -hex 24)"
    echo "   generated TRIGGER_SECRET (the Scheduler sweep sends it as X-Trigger-Secret)"
  fi
  # Only when the secret has no version yet: a new one would sign everyone out.
  if [[ "$name" == SESSION_SECRET && -z "$value" ]] && \
     ! gcloud secrets versions list "$secret" --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    value="$(openssl rand -hex 32)"
    echo "   generated SESSION_SECRET (signs the login page's session cookie)"
  fi
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    gcloud secrets create "$secret" --replication-policy=automatic >/dev/null
  fi
  if [[ -n "$value" ]]; then
    printf '%s' "$value" | gcloud secrets versions add "$secret" --data-file=- >/dev/null
    echo "   $secret: new version added"
  elif ! gcloud secrets versions list "$secret" --limit=1 --format='value(name)' | grep -q .; then
    # Cloud Run refuses to deploy against a secret with no versions.
    printf 'unset' | gcloud secrets versions add "$secret" --data-file=- >/dev/null
    echo "   $secret: placeholder 'unset' added; fill it in later"
  fi
done

echo
echo "Done. Next:"
echo "  1. Share the Google Sheet with ${RUNTIME_SA} (Editor)."
echo "  2. gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<id>,_DUMMY_MODE=1"
echo "  3. gcloud run jobs execute sourcing-worker --region ${REGION} --args=scripts/check_access.py --wait"
