# sourcing-proto

Cloud Run prototype for SourcingAI, built from
[`docs/SourcingAI_CloudRun_Prototype_Plan.md`](docs/SourcingAI_CloudRun_Prototype_Plan.md).
Flow: Google Sheet row → `/trigger` → Cloud Run Job → login (Context reuse / password + TOTP)
→ search Proponent → Claude extracts offers → `Results` tab.

**Start here:** [SETUP.md](SETUP.md) (install, tests, dry run, real run) ·
[COMMANDS.md](COMMANDS.md) (every command and run flag) ·
[docs/PIPELINE.md](docs/PIPELINE.md) (how a row moves through the system) ·
[test_runs/](test_runs/README.md) (every real run: results, cost, time) ·
[CLAUDE.md](CLAUDE.md) (one-page project context, also read by AI agents).

**Multiple sheets:** the worker isn't tied to one spreadsheet. On Cloud Run (`--all-sheets`) it
processes every sheet in the registry (Firestore `sheets` collection) plus `SHEET_ID`; a local run
searches only `SHEET_ID`. A new team's sheet joins with one
`scripts/manage_sheets.py add` and no redeploy — see "Adding a new sheet" below. Vendor logins and
account leases are shared across every sheet (so two sheets sharing one Proponent login still
queue correctly instead of racing); each sheet gets its own Results tab and row tracking.

## Status against the plan

| Plan step | State |
|---|---|
| 0. GCP + accounts setup | Script ready: `infra/setup_gcp.sh`, check with `scripts/check_access.py`. Not run yet. |
| 1. Skeleton on Cloud Run | Done in code: Dockerfile, `cloudbuild.yaml`, sheet read/status/write, `--dummy` mode. Not deployed yet. |
| 2. Proponent search | Done in code: Secret Manager `(vendor, user)` + shared login, Browserbase Contexts, auth manager steps 0–1, Proponent adapter, Claude extraction, `SheetSink`, cost/time per row. **Tested on the real portal** (2026-10-12 to 10-14, up to 106 parts per run): see [test_runs/](test_runs/README.md). |
| 3. Automatic trigger | `/trigger` (secret, debounce, worker lock) called by the Sheets add-on or a per-sheet script (`apps_script/`, search starts after 90s without edits) and a Cloud Scheduler sweep every 15 min (command below). Rows added during a run are read before the worker ends. |
| 4. Session robustness | Partly done: expiry detection, re-login once + retry, `NOT_FOUND` only after the auth probe passes, account leases, login attempt cap → `SUSPENDED`. Not done: morning warm-up, measuring session lifetime. |
| 5. Human 2FA handoff | Not built. Today, 2FA that TOTP can't handle sets rows to `NEEDS_ATTENTION` and posts a Chat message. `/hitl/*` return 501. |
| 6. Credential update flow | Detection done (wrong password → `NEEDS_CREDENTIAL_UPDATE`, rows `NO_CREDENTIALS`, Chat message). `CredentialProvider.update()` + Secret Manager write (new version) done. Form (`/credentials/update`) not built. |
| 7. Passkey spike | Not started. |

## Layout

```
sourcing/
  config.py            env settings (Secret Manager values arrive as env on Cloud Run)
  models.py            RequestRow, Offer, ResultRow, RowStatus, AccountState
  store.py             Firestore + in-memory store: locks, accounts/leases, sheet registry, selectors, jobs, rows, audit
  sheets.py            Requests/Results tabs (columns found by header name)
  pipeline.py          NEW rows → group by account → lease → login → search → sink
  extract.py           Claude: offers from page text (strict JSON schema), find search box; cost per call
  auth/manager.py      login ladder + account states
  browser/browserbase.py  sessions with Context, keep-alive, Live View, release
  browser/local.py        BROWSER_BACKEND=local: Chromium in the container (no Live View, no saved cookies)
  credentials/         CredentialProvider interface, GCP Secret Manager provider
  vendors/             VendorAdapter base, Proponent, dummy
  sinks/               ResultSink interface, SheetSink
  notify/chat.py       Google Chat webhook
service/api.py         FastAPI service: /trigger (+ 501 stubs for /hitl/*, /credentials/update)
service/job_main.py    worker entrypoint: SHEET_ID (or every registered sheet with --all-sheets)
infra/setup_gcp.sh     Step 0 setup
scripts/check_access.py  checks every dependency without printing secrets
scripts/manage_sheets.py  add / list / enable / disable sheets in the registry
samples/               clean 20-part Requests sheet for local runs
test_runs/             logs + reports of every real run, named by outcome
docs/                  plan docs, PIPELINE.md
tests/                 133 tests: pipeline (fakes), sheets, credentials (incl. Secret Manager), API, sheet registry, Proponent adapter on mock pages
```

## Run locally

```bash
cd sourcing-proto
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium-headless-shell   # only for the adapter tests
.venv/bin/python -m pytest -q
```

Against a real sheet (your Google login, no Firestore needed):

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
cp .env.example .env        # set SHEET_ID; STORE_BACKEND=memory
.venv/bin/python -m service.job_main --dummy
```

**Without Google Sheets or Firestore:** copy `samples/requests_20-parts_proponent.xlsx` to e.g. `my_test.xlsx` and set
`SHEET_ID=my_test.xlsx` (any `.xlsx` path, relative to this folder; same `Requests`/`Results` tabs and headers) and `STORE_BACKEND=file`. Status
updates and results are saved straight into the file, and logins/Contexts/selectors are kept
between runs in `.local_store.json` (delete it to start fresh). Close the `.xlsx` in Excel /
LibreOffice before a run.

Drop `--dummy` and fill in the Browserbase key and an LLM key (Anthropic by default; set
`LLM_PROVIDER`/`LLM_MODEL` to use OpenAI or Gemini instead) to run the real Proponent flow from
your machine. Vendor logins are read from Secret Manager with your `gcloud auth
application-default login` account, which needs read access to `sourcing-vendor-*`. The browser still runs in Browserbase.

## Sheet

`Requests` tab headers (any order): `RFQ ID | Vendor | Part Number | Qty Needed | Condition Wanted | Requested By | Status | Run ID | Notes`.
The worker picks up rows whose Status is `NEW` **or empty**. The `Results` header is written on first use.

### Adding a new sheet (no redeploy)

1. Create the sheet (`Requests` + `Results` tabs, same headers as above) and share it with
   `sourcing-runtime@<project>.iam.gserviceaccount.com` as Editor.
2. Register it (with the same `STORE_BACKEND`/`GCP_PROJECT` the worker uses):

```bash
python scripts/manage_sheets.py add <sheet_id> --label "Team A"
python scripts/manage_sheets.py list
python scripts/manage_sheets.py disable <sheet_id>
```

New rows are picked up by the next Scheduler sweep (every 15 min).

## Vendor logins (GCP Secret Manager)

Everything runs in Skylink's existing project `skylink-material-planning`; no separate project.
Our access covers secrets whose names start with `sourcing-`. It does not include listing, so the
console list won't show them: always use the names directly.

| Secret | Value |
|---|---|
| `sourcing-vendor-proponent` | shared login: `{"vendor": "...", "url": "...", "username": "...", "password": "...", "totp_secret": "BASE32..."}` (`totp_secret` optional) |
| `sourcing-vendor-proponent__jane-skylink-com` | optional per-person login (email lowercased, other characters → `-`); tried before the shared one |

Add or change a login without the password landing in shell history:

```bash
gcloud secrets versions add sourcing-vendor-proponent --data-file=- --project skylink-material-planning
# paste the JSON, then Ctrl-D
gcloud secrets versions access latest --secret=sourcing-vendor-proponent --project skylink-material-planning
```

New vendor secrets are created by the Skylink admin for now. To point a vendor at another secret
(e.g. the test secret), set `VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test`
(locally in `.env`, or `_VENDOR_SECRET_OVERRIDES` in Cloud Build).

Infra keys are secrets too: `sourcing-browserbase-api-key`, `sourcing-browserbase-project-id`,
`sourcing-anthropic-api-key`, `sourcing-chat-webhook-url`, `sourcing-trigger-secret`. The runtime
service account can read `sourcing-*` and add versions to `sourcing-vendor-*` only.

## Deploy

```bash
export PROJECT_ID=skylink-material-planning REGION=us-central1
cp infra/secrets.env.example infra/secrets.env    # fill in, run, then delete the file
bash infra/setup_gcp.sh
# share the sheet with sourcing-runtime@$PROJECT_ID.iam.gserviceaccount.com

# Step 1: dummy deploy, then run the job by hand
gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<id>,_DUMMY_MODE=1
gcloud run jobs execute sourcing-worker --region $REGION --args=scripts/check_access.py --wait
gcloud run jobs execute sourcing-worker --region $REGION --wait

# Step 2: real Proponent
gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<id>,_DUMMY_MODE=0

# Step 3: Scheduler sweep every 15 minutes
API_URL=$(gcloud run services describe sourcing-api --region $REGION --format='value(status.url)')
gcloud scheduler jobs create http sourcing-sweep --location $REGION --schedule='*/15 * * * *' \
  --uri="$API_URL/trigger" --http-method=POST \
  --headers="X-Trigger-Secret=$(gcloud secrets versions access latest --secret=sourcing-trigger-secret)"
```

## Login URL and recording

The login start page is taken from, in order: the vendor config `login_url` (Firestore
`vendors/<id>`, or `vendors` in `.local_store.json`), the login page found on an earlier run
(saved with the selectors as `login_page_url`), the secret's `url`, then the adapter's default.
Each one must be `https` on one of the vendor's own hosts (`allowed_hosts` on the adapter, or in
the vendor config); others are skipped with a warning. If the page has no login form, the adapter
follows its Login link and saves the page it lands on. Credentials are only typed on the vendor's
hosts, even after a redirect.

`BROWSERBASE_RECORD_SESSIONS=1` records sessions and logs a replay link per session. Recordings
show the login pages, so keep it for local debugging and off on Cloud Run.

## Bulk search and logout

- **Bulk search is the default** (since 2026-10-16, client's choice): an adapter can search several parts at once
  (`search_many`). Proponent uses its BULK SEARCH box (one "PART QTY" per line, portal limit 500),
  in batches of `bulk_batch_size` (default 25, so one results page stays readable in a single LLM
  call). `job_main` uses it for runs of at least 2 parts (`--bulk N` changes that), whatever
  `bulk_min_parts` in the vendor config says. If the bulk search fails twice in a row, the run switches to one-by-one search for the
  rest of the run; parts it missed are searched again one by one before logout.
- **One by one** (`--one-by-one`, and always for a single part, the fallback and the retry): each
  part's results page is opened directly (`PartSearchResults.aspx?id=<part>`), falling back to the
  site's search form.
- **Logout:** at the end of each account's work the adapter signs out of the portal
  (`logout_after_run`, default true), then the Browserbase session is released. The next run then
  logs in again. Set `logout_after_run: false` to keep the sign-in in the saved Context instead
  (fewer logins and 2FA prompts, but the vendor session stays open until the portal expires it).

## Real Proponent runs: what was learned

All runs are in [test_runs/](test_runs/README.md) (logs, per-part reports, cost and time).
The questions open before the first run are answered:

1. `probe_url`: `https://procart.proponent.com/` works as the auth probe (signed in = no Login
   button in the header).
2. Results show price/qty/condition in a jqGrid on the results page (detail rows expanded); no
   click into a part page is needed. The grid's records are read directly (`sourcing/vendors/grid.py`).
3. Still open: whether the old MCP run's 6 `not_found` results
   (`docs/old_mcp_prototype_proponent_results.json`) were real.

## Choices worth knowing

- **LLM provider is not fixed to Claude.** `sourcing/llm/` defines one interface
  (`LlmClient.json_call`); `extract.py` builds the prompt and JSON schema once and doesn't care
  which provider answers. Switch with `LLM_PROVIDER=anthropic|openai|gemini` + `LLM_MODEL=<model>`
  and that provider's API key — no code change. `openai`/`gemini` need their SDK installed
  (commented out in `requirements.txt`, since most runs only use one provider) and haven't been
  run against a real Proponent page yet; check the first real response against
  `OFFERS_SCHEMA`/`PICK_INPUT_SCHEMA` in `extract.py`, and see the warning in `gemini_client.py`
  about its schema dialect before trusting it. Each provider's pricing table
  (`sourcing/llm/*_client.py`) needs filling in before its cost numbers mean anything — only
  Anthropic's is filled in so far.
- **Default:** `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-haiku-5-5`, `LLM_EFFORT=low` — cheaper
  per token than the Azure gpt-4o-mini setup this replaces (~$0.10/$0.50 per 1M vs ~$0.15/$0.60).
  Every row's tokens and cost are saved to Firestore `rows/*` and summed per run in `jobs/*`, so
  this can be checked against real pages; if Haiku misses fields on Proponent's actual markup,
  step up to `claude-sonnet-5-5` before `claude-opus-5-5` (~14x and ~28x pricier per token
  respectively). Anthropic requests use the server-side refusal fallback (`fallbacks: "default"`).
- **Row ids** are sheet row numbers. Deleting rows in the middle of the sheet shifts them; fine for
  the prototype.
- **`/trigger` is public** and protected by `X-Trigger-Secret`. Step 5 adds Google sign-in for
  the handoff pages.
- **Long results pages** are cut at `MAX_PAGE_CHARS` (60k characters), with a warning in the log.
