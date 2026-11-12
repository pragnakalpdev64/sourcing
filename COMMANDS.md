# Command reference

The commands you need day to day, in one place. Run everything from the repo root.
First-time install is in [SETUP.md](SETUP.md); how the code works is in
[docs/PIPELINE.md](docs/PIPELINE.md).

## Contents

1. [Install and test](#1-install-and-test)
2. [Run the worker locally](#2-run-the-worker-locally)
3. [Run flags](#3-run-flags)
4. [Common local runs](#4-common-local-runs)
5. [After a real run](#5-after-a-real-run)
6. [Local state and sheets](#6-local-state-and-sheets)
   - [Vendors and onboarding](#6b-vendors-and-onboarding)
7. [Access checks and vendor logins](#7-access-checks-and-vendor-logins)
8. [API service locally](#8-api-service-locally)
9. [Deploy (Cloud Run)](#9-deploy-cloud-run)

---

## 1. Install and test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium-headless-shell   # only for the adapter tests

.venv/bin/python -m pytest -q                        # whole suite, no network; must stay green
.venv/bin/python -m pytest -q tests/test_pipeline.py # one file
.venv/bin/python -m pytest -q -k bulk                # tests whose name matches "bulk"
```

## 2. Run the worker locally

Set these once in `.env` (copy from `.env.example`):

| Setting | What it is |
|---|---|
| `SHEET_ID` | the sheet to search: a Google Sheet id, or a local `.xlsx` path |
| `LLM_MODEL`, `LLM_EFFORT` | which Claude model reads the results pages, and how hard it thinks |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `ANTHROPIC_API_KEY` | keys from the project lead |
| `STORE_BACKEND=file` | keeps saved logins and run records in `.local_store.json` |
| `DUMMY_MODE=0` | `1` turns every run into a dry run |

Log in to Google once, so the worker can use the Google Sheet and Secret Manager:

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
```

Then start a search:

```bash
.venv/bin/python -m service.job_main
```

This command:

1. reads the `Requests` tab of `SHEET_ID` and picks up every row whose Status is empty or `NEW`
2. for each row's vendor, reads the login from GCP Secret Manager: the team member's own secret if
   there is one, otherwise the vendor's shared secret (see §7)
3. logs in to the vendor portal and searches the parts **in bulk** (25 per search; a run with one part searches it on its own)
4. writes the offers to the `RFQ` tab (client's RFQ layout, created when missing) and sets each
   row's Status; the `Results` tab only with `--results`

## 3. Run flags

All flags are optional, and each one applies to that run only. Everything else comes from `.env`.

| Flag | What it does |
|---|---|
| `--sheet ID` | search this sheet instead of `SHEET_ID` (Google Sheet id or `.xlsx` path) |
| `--bulk N` | bulk search is the default for runs of 2 or more parts; `--bulk N` = only from N parts. |
| `--one-by-one` | one search per part for this run instead of bulk (slower, about twice the LLM cost) |
| `--dummy` | dry run: fake vendor, no browser, Secret Manager or LLM, no keys needed |
| `--browser local\|browserbase` | browser for this run (else `BROWSER_BACKEND`, default browserbase). `local` = Chromium on this machine / in the container: no Live View for 2FA, no saved cookies. Add `--headed` to see the window. |
| `--record` | record the Browserbase session (replay link in the log). The recording shows the login page, so use it for local debugging only. |
| `--dump-pages` | save each results page the LLM read to `debug_pages/` (never commit it) |
| `--results` | also write each offer to the `Results` tab (with its Flag column, e.g. `NEW_VENDOR_CHECK`, `PRICE_NOT_ON_PAGE`). Without it only the `RFQ` tab is written: the client's RFQ layout, 24 columns, created when missing (see `docs/RFQ_Sample_Column_Review.md`); since 2026-10-29. |
| `--rfq` | no effect any more (the RFQ tab is always written); kept so older commands still run |
| `--all-sheets` | also process every sheet added with `manage_sheets.py add`. The Cloud Run job runs with this; you don't need it locally. |
| `--set [vendor:]key=value` | advanced: change one vendor config key for this run, e.g. `--set proponent:logout_after_run=false` |

To try another model, change `LLM_MODEL` / `LLM_EFFORT` in `.env`.

## 4. Common local runs

```bash
# Normal run: SHEET_ID from .env, bulk search
.venv/bin/python -m service.job_main

# Dry run, no keys needed: copy the sample sheet first (close it in Excel/LibreOffice during the run)
cp samples/requests_20-parts_proponent.xlsx my_test.xlsx
.venv/bin/python -m service.job_main --dummy --sheet my_test.xlsx

# Real search on a local Excel file instead of the Google Sheet
.venv/bin/python -m service.job_main --sheet my_test.xlsx

# One search per part instead of bulk
.venv/bin/python -m service.job_main --one-by-one

# Debug a bad result: record the session and keep the pages the LLM read
.venv/bin/python -m service.job_main --record --dump-pages

# Keep the log (for test_runs/)
.venv/bin/python -m service.job_main 2>&1 | tee run.log
```

To search the same rows again, set their Status back to `NEW` or clear it.

**Before any real run:** Skylink team members use the same Proponent login every day. Search only a few
parts per run, and run outside busy hours.

## 5. After a real run

```bash
.venv/bin/python scripts/run_report.py                 # per-part time, tokens, cost (latest run)
.venv/bin/python scripts/run_report.py <run_id>        # a given run (from the log)
.venv/bin/python scripts/run_report.py --csv report.csv
.venv/bin/python scripts/check_page_health.py          # per-vendor page-health history + alarms
.venv/bin/python scripts/check_page_health.py proponent
```

`check_page_health.py` shows each vendor's recent runs' health rates and flags the newest run
when it deviates far enough from that vendor's own history to suggest the pages changed (the
same check runs at the end of every pipeline run and warns in the Chat message; see
[sourcing/health.py](sourcing/health.py)). A warning means: re-run
`scripts/onboard_vendor.py <vendor> --fresh`, compare the offers with the portal, and update the
adapter if the pages moved.

Report a run before you re-run its rows: running a sheet row again overwrites its record.
Then save the log and report in a new `test_runs/NN_…` folder and add a row to its table (naming
rules in [test_runs/README.md](test_runs/README.md)).

## 6. Local state and sheets

With `STORE_BACKEND=file` the state lives in `.local_store.json`: logins (Contexts), learned
selectors, vendor config, the sheet registry and the row records.

The sheet registry is the list of sheets the Cloud Run job processes (`--all-sheets`). A local
run without that flag ignores it and only searches `SHEET_ID`.

```bash
.venv/bin/python scripts/manage_sheets.py add <sheet_id> --label "Team A"   # Cloud Run picks it up
.venv/bin/python scripts/manage_sheets.py list
.venv/bin/python scripts/manage_sheets.py disable <sheet_id>
.venv/bin/python scripts/manage_sheets.py enable <sheet_id>

rm .local_store.json          # start clean: forgets saved logins, selectors and registered sheets
```

To change vendor config permanently, edit `vendors.<vendor_id>` in `.local_store.json` (locally)
or Firestore `vendors/<vendor_id>` (Cloud Run). To change it for one run, use `--set` (§3).

Per-vendor login and search limits (docs/PIPELINE.md §5b) are in the same vendor record and
are changed with their own script, which refuses unknown keys and bad values:

```bash
.venv/bin/python scripts/vendor_limits.py list                       # which vendors have confirmed limits
.venv/bin/python scripts/vendor_limits.py show jamaica_bearings      # values, sources, today's counters
.venv/bin/python scripts/vendor_limits.py set jamaica_bearings vendor_lockout_after=1 login_safety_margin=0
.venv/bin/python scripts/vendor_limits.py unset jamaica_bearings max_searches_per_day   # back to default
.venv/bin/python scripts/vendor_limits.py reset-login jamaica_bearings [--owner email]  # after a person checked it
.venv/bin/python scripts/vendor_limits.py reset-login proponent --today  # also today's login count (tests)
.venv/bin/python scripts/vendor_limits.py resume jamaica_bearings    # end a "too many requests" pause
```

For one run only: `--set jamaica_bearings:limits.max_searches_per_run=10`. For a test run right
after a failed login: `--set proponent:limits.login_cooldown_minutes=0` (no wait after a failure)
and `--set proponent:limits.max_logins_per_day=20`. The lockout guard (`vendor_lockout_after`,
`login_safety_margin`) is better left alone: it is what keeps the shared login from being locked.

## 6b. Vendors and onboarding

**Vendors** are records in the store (`vendors/<vendor_id>`): name, aliases team members may type,
login URL / website, allowed hosts, adapter (`generic` or `proponent`), enabled, limits. The login
URL is never a sheet column. Proponent works without a record (built-in adapter).

**A new vendor needs only its login secret** (§7): `sourcing-vendor-<vendor>` with the vendor's
login page as `url` (plus `allowed_hosts` when the login passes through another site, e.g. single
sign-on). When a team member types the vendor's name in the sheet, the worker sets the vendor up
from that secret (generic adapter), and its first run's Results rows are flagged
`NEW_VENDOR_CHECK` (compare a few with the website). To try a new vendor first:

```bash
.venv/bin/python scripts/check_access.py --vendor wencor                    # secret readable?

# Onboarding run: log in, search 1-3 known parts, log out, print offers and how each
# element was found. Writes nothing to the sheet; saves selectors only with --save.
.venv/bin/python scripts/onboard_vendor.py wencor --part 35010-1 --part MS20995C32 \
    --browser local --fresh 2>&1 | tee onboarding.log
```

**2FA.** With `totp_secret` in the secret the code is typed automatically. Anything else (a code
sent by email or SMS, a push approval) is handed to a person over Google Chat, and the run waits
with the browser open:

- **Live View** (used when the browser has one): the message links to the live browser; the person
  opens it and logs in there; the run sees the sign-in and carries on. Browserbase gives a link
  anyone can open (so for real use it goes behind `/hitl/view`, not built yet); the local browser
  gives one only with `--live-view` / `LOCAL_BROWSER_LIVE_VIEW=1`: Chromium's DevTools on
  127.0.0.1, this machine only (if only DevTools shows, click "Toggle screencast").
- **Code form** (otherwise, when `PUBLIC_BASE_URL` is set and the API is running): the message
  links to the API's code form; the run types the code in when it arrives (one more request if
  the portal rejects it). With a Chat app
set up (`CHAT_APP_SPACE`, `CHAT_APP_AUDIENCE`; the client's to create) the same request also comes
as a card with a text box. No code within `HITL_CODE_WAIT_SECONDS` (600): the rows go
`NEEDS_ATTENTION`; set them back to NEW to try again. Push approvals and anything without a code
box still stop. A bot check stops the run too; a CAPTCHA is tried again (for now, 2026-11-03).

```bash
# Live View in the local browser (tested on Notion 2026-10-22)
.venv/bin/python scripts/onboard_vendor.py notion --login-only \
    --login-file .local_login_notion.json --browser local --live-view

# Code form instead. Terminal 1: the API (same STORE_BACKEND=file as the run)
.venv/bin/uvicorn service.api:app --port 8080
# Terminal 2: login-only check with a local test login and a visible browser
PUBLIC_BASE_URL=http://localhost:8080 .venv/bin/python scripts/onboard_vendor.py notion \
    --login-only --login-file .local_login_notion.json --browser local --headed \
    --notify you@co.com
```

A login file has a secret's keys (`url`, `username`, `password` or `"passwordless": true`,
`totp_secret`, `allowed_hosts`). It is for test accounts only; Skylink's vendor logins stay in
Secret Manager.

Checking or fixing a vendor by hand:

```bash
.venv/bin/python scripts/manage_vendors.py list                     # every vendor + state
.venv/bin/python scripts/manage_vendors.py add wencor --name Wencor \
    --login-url https://shop.wencor.com/login --hosts shop.wencor.com --aliases "Wencor Group"
.venv/bin/python scripts/manage_vendors.py show wencor              # record + learned selectors
.venv/bin/python scripts/manage_vendors.py set wencor currency=USD  # any record key (JSON values)
.venv/bin/python scripts/manage_vendors.py enable wencor            # after its onboarding run
.venv/bin/python scripts/manage_vendors.py disable proponent        # stop a vendor's rows (FAILED)
.venv/bin/python scripts/manage_vendors.py export vendors.json      # records to a file
.venv/bin/python scripts/manage_vendors.py import vendors.json      # seed a fresh store; existing kept
```

Vendors added with `add` start disabled; the login URL and website must be `https` on an allowed
host. Limits are set with `vendor_limits.py` (§6), not `manage_vendors.py set`. `--secret NAME`
reads the login from another secret (e.g. `sourcing-vendor-test`). Live portal rules apply: few
parts, outside the team's working hours.

## 7. Access checks and vendor logins

```bash
.venv/bin/python scripts/check_access.py                                   # every dependency, OK/FAIL
.venv/bin/python scripts/check_access.py --vendor proponent --user you@co.com
```

Vendor logins are kept in GCP Secret Manager, project `skylink-material-planning`, one secret per
vendor and user:

| Secret | Used for |
|---|---|
| `sourcing-vendor-<vendor>` | the vendor's shared login |
| `sourcing-vendor-<vendor>__<email-with-dashes>` | one team member's own login for that vendor (e.g. `sourcing-vendor-proponent__jane-skylink-com`); tried first, for rows whose Requested By is that team member |

A vendor can have any number of user secrets. Our account can't list secrets, so always use the
full secret name. Add a new version without putting the
password in your shell history:

```bash
gcloud secrets versions add sourcing-vendor-proponent --data-file=- --project skylink-material-planning
# paste {"vendor": "...", "url": "...", "username": "...", "password": "...", "totp_secret": "...",
#        "allowed_hosts": "..."}, then Ctrl-D   (totp_secret, allowed_hosts optional)
gcloud secrets versions access latest --secret=sourcing-vendor-proponent --project skylink-material-planning
```

Only the client creates new vendor secrets. Never put logins in `.env` or in code.

## 8. API service locally

```bash
.venv/bin/uvicorn service.api:app --reload --port 8080
curl localhost:8080/healthz
curl -X POST localhost:8080/trigger -H "X-Trigger-Secret: $TRIGGER_SECRET"   # what the Scheduler sweep sends
```

`/trigger` starts the worker job on Cloud Run, so a local run of it needs GCP access. Use it to
check routing and the secret; for a full local run use `service.job_main` (§2).

Testing the sheet's Apps Script before deploying (`apps_script/README.md`): `/trigger` starts the
worker on this machine, on the sheet that called (`--sheet`), and once more if rows came in meanwhile.

```bash
TRIGGER_RUN_LOCAL=1 TRIGGER_LOCAL_ARGS="--dummy" .venv/bin/uvicorn service.api:app --port 8080
ngrok http 8080        # its https address + /trigger goes in the script's TRIGGER_URL
```

**Vendor login update page** (`/credentials`, docs/CREDENTIAL_UPDATE_UI_PLAN.md). Saving writes a
real new version to Secret Manager (the secret `VENDOR_SECRET_OVERRIDES` points at), so test with
the test secret.

```bash
# Logins the page offers (the worker also adds one the first time it uses a login)
.venv/bin/python scripts/manage_logins.py list
.venv/bin/python scripts/manage_logins.py add proponent --owner shared --editors a@skylink.com
.venv/bin/python scripts/manage_logins.py grant proponent shared b@skylink.com   # or revoke

# Without an OAuth client: sign in as one email (refused on Cloud Run)
SESSION_SECRET=dev DEV_SIGNIN_EMAIL=a@skylink.com .venv/bin/uvicorn service.api:app --port 8080
# open http://localhost:8080/credentials
```

With Google sign-in: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`,
`ALLOWED_EMAIL_DOMAINS`, `CREDENTIAL_ADMINS`, and `PUBLIC_BASE_URL=http://localhost:8080` (the
OAuth client needs redirect URI `http://localhost:8080/auth/callback`).

## 9. Deploy (Cloud Run)

None of this has been run yet. Everything goes in Skylink's existing project; don't create a new
project.

```bash
export PROJECT_ID=skylink-material-planning REGION=us-central1
cp infra/secrets.env.example infra/secrets.env    # fill in, run, then delete the file
bash infra/setup_gcp.sh

gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<id>,_DUMMY_MODE=1
# Login update page: add _PUBLIC_BASE_URL=<sourcing-api URL>,_OAUTH_CLIENT_ID=<id>,
#   _ALLOWED_EMAIL_DOMAINS=skylink.com,_CREDENTIAL_ADMINS='a@skylink.com;b@skylink.com'
gcloud run jobs execute sourcing-worker --region $REGION --args=scripts/check_access.py --wait
gcloud run jobs execute sourcing-worker --region $REGION --wait
```

The rest of the deploy steps (real mode, Scheduler sweep) are in
[README.md → Deploy](README.md#deploy).

---

Never commit `.env`, `infra/secrets.env`, `.local_store.json`, `debug_pages/` or
`test_runs/**/vendor_pages/`.
