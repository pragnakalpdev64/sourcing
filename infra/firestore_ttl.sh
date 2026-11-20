#!/usr/bin/env bash
# Turns on Firestore's automatic deletion (TTL) for the collections that grow
# (sourcing/store.py EXPIRE_DAYS): a document is deleted once its `expire_at` date
# has passed (usually within 24 hours after). Run once per Firestore project, by
# someone with Firestore admin rights. Documents written before the code set
# `expire_at` have none and are never deleted by it.
#
#   infra/firestore_ttl.sh <project>        e.g. sourcing-skylink-addon
#   infra/firestore_ttl.sh <project> --off  turn it off again
set -euo pipefail
PROJECT="${1:?usage: infra/firestore_ttl.sh <project> [--off]}"
ACTION="--enable-ttl"
[[ "${2:-}" == "--off" ]] && ACTION="--disable-ttl"
for group in rows jobs hitl audit sheet_ops sheet_snapshots sheet_progress; do
  echo "== ${group}"
  gcloud firestore fields ttls update expire_at --collection-group="${group}" \
    "${ACTION}" --project="${PROJECT}" --async --quiet
done
gcloud firestore fields ttls list --project="${PROJECT}"
