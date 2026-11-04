# Login update page on Cloud Run: deploy plan

> **Superseded 2026-10-30** by `SOURCING_WORKER_DEPLOY_PLAN.md`: the login page went to `sourcing-ai-dev` (IAP, mie-agent `sourcing_credentials_web/`), and the worker plan follows the client's setup. Kept for history.

Goal: run the vendor login update page (`/credentials`, plan step 6,
`docs/CREDENTIAL_UPDATE_UI_PLAN.md`) on the `sourcing-api` Cloud Run service in
`skylink-material-planning`, so a team member can change a vendor password from
a Chat link without the GCP console. Written 2026-10-26.

State: the page works locally (dev sign-in and real Google sign-in through
ngrok), tested with fakes; UI restyled 2026-10-26. Nothing is deployed. The
deploy files exist (`Dockerfile`, `cloudbuild.yaml`, `infra/setup_gcp.sh`) and
already carry the page's settings.

## What runs where

```
team member ──https──> sourcing-api (Cloud Run service, public URL, runs as sourcing-runtime SA)
                        ├─ /auth/*        Google sign-in (OAuth client in skylink-material-planning)
                        ├─ /credentials*  reads account records ──> Firestore
                        │                 adds secret version   ──> Secret Manager sourcing-vendor-*
                        └─ /trigger       starts the worker job (Cloud Scheduler)
sourcing-worker (Cloud Run job) reads versions/latest of the same secret on its next run
```

One image, one service: the page has no state of its own (signed cookies), so
`--max-instances=2` and cold starts need nothing extra.

## Phase 1: code changes before the first deploy (us)

| # | Change | Why |
|---|---|---|
| 1 | `Settings` secret fields `repr=False` (`sourcing/config.py`): API keys, OAuth client secret, session secret, trigger secret, Chat webhook | Any error or log line that prints the settings writes every key into Cloud Logging. Seen 2026-10-26 (a wrong `make_store` call printed them all). |
| 2 | `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` in `service/api.py` | The service is public (`--allow-unauthenticated`); `/docs` and `/openapi.json` list every endpoint to anyone. |
| 3 | Headers on the sign-in and password pages: `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` | Stops the password form being framed by another site; the pages use no scripts or outside files, so this policy costs nothing. |
| 4 | A `pytest` step in `cloudbuild.yaml` before `build` | A red test suite never reaches Cloud Run. |
| 5 | Startup log line in the API: which sign-in settings are set (names only, never values), and a 503 page that names the missing one | First deploy problems show up in the log instead of as a blank sign-in. |
| 6 | `scripts/check_access.py --credentials-page`: as the runtime SA, read `versions/latest` of the test secret and add one version with the same value | Proves the two IAM roles before a person tries it. Runs as a job execution (Phase 3). |

Every change keeps `pytest -q` green and adds a test where it changes behaviour (1, 2, 3, 5).

## Phase 2: client setup (Skylink admin, one time)

Needs project IAM admin in `skylink-material-planning`. We send these steps
and the answers we need; they run them.

1. **Answers we need first** (open questions in `CREDENTIAL_UPDATE_UI_PLAN.md`):
   - Sign-in email domain(s). The `.env` admin is `@skylinkintl.com`, so likely
     `skylinkintl.com`, not `skylink.com`.
   - Admin emails (`CREDENTIAL_ADMINS`) and who may change each shared login.
   - Is the project inside Skylink's Workspace organization? (needed for an "Internal" consent screen)
2. **OAuth consent screen**: Internal; scopes `openid`, `email`, `profile` only.
3. **OAuth client**: "Web application"; redirect URI
   `https://sourcing-api-<PROJECT_NUMBER>.us-central1.run.app/auth/callback`.
   Cloud Run's URL is fixed by service name, project number and region, so it is
   known before the first deploy. Check it after deploy with
   `gcloud run services describe sourcing-api --region us-central1 --format='value(status.url)'`.
4. **`infra/setup_gcp.sh`** with `infra/secrets.env` holding `GOOGLE_OAUTH_CLIENT_SECRET`
   (leave `SESSION_SECRET` empty: generated once). Gives `sourcing-runtime`:
   `secretAccessor` on `sourcing-*`, `secretVersionAdder` on `sourcing-vendor-*`,
   `datastore.user`, `run.invoker`, `logging.logWriter`; creates Firestore and the build SA.
5. **Public access check**: an org policy "Domain restricted sharing"
   (`iam.allowedPolicyMemberDomains`) blocks `--allow-unauthenticated`. If it is on,
   the admin adds an exception for this service, or we fall back to IAP in front of
   the service (bigger change, decide then).
6. **Vendor secrets**: the client creates `sourcing-vendor-<vendor>` (and personal
   ones) in Secret Manager. A test secret `sourcing-vendor-test` already exists.

## Phase 3: first deploy against the test secret

```bash
gcloud builds submit --config cloudbuild.yaml --substitutions=\
_SHEET_ID=<sheet id>,_DUMMY_MODE=1,\
_PUBLIC_BASE_URL=https://sourcing-api-<PROJECT_NUMBER>.us-central1.run.app,\
_OAUTH_CLIENT_ID=<client id>,\
_ALLOWED_EMAIL_DOMAINS=<domain>,\
_CREDENTIAL_ADMINS=<a@domain>;<b@domain>,\
_VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test
```

`_DUMMY_MODE=1` keeps the worker off the live portal; the page does not depend on it.
`_VENDOR_SECRET_OVERRIDES` makes both the page and the worker use the test secret.

Then:

```bash
# IAM check as the runtime SA (Phase 1 #6)
gcloud run jobs execute sourcing-worker --region us-central1 \
  --args=scripts/check_access.py,--credentials-page --wait
# Account records the page lists (Firestore)
gcloud run jobs execute sourcing-worker --region us-central1 \
  --args=scripts/manage_logins.py,add,proponent,--owner,shared,--editors,<a@domain> --wait
```

## Phase 4: acceptance tests on Cloud Run

Run with one admin and one non-admin Skylink account.

| # | Test | Expected |
|---|---|---|
| 1 | Open `/credentials` signed out | Sign-in page, 401 |
| 2 | Sign in with a Skylink account | Back on `/credentials`, email shown at the top |
| 3 | Sign in with a Gmail account | "not a Skylink Google account", 403 |
| 4 | Non-editor opens the shared login's form URL | 403 |
| 5 | Admin saves a new password on the test login | "Saved"; new version in Secret Manager (`gcloud secrets versions list sourcing-vendor-test`); audit record `credential_updated` in Firestore; no password in Cloud Logging |
| 6 | Mismatched passwords | Error box, nothing written |
| 7 | Chat link: set the test account to `NEEDS_CREDENTIAL_UPDATE`, run the worker | Chat message with `/credentials/update?...` link that opens the form after sign-in |
| 8 | Worker run after a change | Uses the new version (`--dummy` off, only with the test login and outside busy hours, client rule 4) |
| 9 | `/docs`, `/openapi.json` | 404 |
| 10 | Phone width | Login list stacks, buttons reachable |

## Phase 5: switch to the real logins

1. Deploy again with `_VENDOR_SECRET_OVERRIDES=` (empty) and the real LLM settings.
2. `manage_logins.py add` for every real login, `grant` editors on shared ones.
3. Tell the team: the Chat message after a rejected password has the link; after
   saving, set the stopped rows' Status back to `NEW`.

**Rollback of a bad password change**: a disabled latest version breaks reads, so
the admin adds the previous value as a new version (Secret Manager console or
`gcloud secrets versions add`). **Rollback of a bad deploy**: `gcloud run services
update-traffic sourcing-api --to-revisions=<previous>=100`.

## Settings on Cloud Run (all already in `cloudbuild.yaml`)

| Setting | Source |
|---|---|
| `GCP_PROJECT`, `STORE_BACKEND=firestore` | set by cloudbuild |
| `PUBLIC_BASE_URL` | `_PUBLIC_BASE_URL` |
| `GOOGLE_OAUTH_CLIENT_ID` | `_OAUTH_CLIENT_ID` |
| `ALLOWED_EMAIL_DOMAINS`, `CREDENTIAL_ADMINS` | substitutions, `;`-separated |
| `VENDOR_SECRET_OVERRIDES` | `_VENDOR_SECRET_OVERRIDES` (same value on service and job) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | secret `sourcing-google-oauth-client-secret` |
| `SESSION_SECRET` | secret `sourcing-session-secret` (a new version signs everyone out) |
| `DEV_SIGNIN_EMAIL`, `ALLOWED_EMAIL_DOMAINS=*` | never; refused on Cloud Run |

## Later (not needed for go-live)

- Admin page to add a login (needs the admin's own Google access, see the UI plan).
- Sign-in in front of `/hitl/view` and the 2FA code form.
- Automatic re-queue of `NO_CREDENTIALS` rows after a password change.
- Monthly password reminder in Chat.
