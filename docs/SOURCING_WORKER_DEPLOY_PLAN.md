# SourcingAI worker on Cloud Run: deploy plan (Proponent, DASI, Aviall)

Goal: the worker that searches vendor portals runs in `skylink-material-planning` on a
schedule, for Proponent, DASI and Aviall, and writes offers to the team's sheet.
Written 2026-10-30. Replaces `CLOUD_RUN_DEPLOY_PLAN.md`, which was for our own
`sourcing-api` design (OAuth page, public `/trigger`, our own registry and service
accounts). The login update page is already live on `sourcing-ai-dev` (mie-agent
`sourcing_credentials_web/`, commit 4c6189b), so the worker only needs to search.

## Shape

```
Cloud Scheduler ──(OAuth, every 30 min in working hours)──> Cloud Run job sourcing-worker
   sourcing-worker (runs as its own service account)
     ├─ reads NEW rows      ──> Google Sheet (shared with the service account as Editor)
     ├─ reads vendor logins ──> Secret Manager sourcing-vendor-proponent / -dasi / -aviall
     ├─ leases, counters    ──> Firestore
     ├─ browser             ──> Chromium inside the job (Browserbase only if chosen, see Aviall)
     ├─ reads result pages  ──> Anthropic API (key in sourcing-anthropic-api-key)
     └─ writes RFQ lines, statuses ──> same sheet; problems ──> Google Chat webhook
sourcing-ai-dev (already live, IAP) ──> adds new versions of the same login secrets
```

No public service. Scheduler starts the job directly, as mie-agent's
`cloud_run/vendor_quote_watch/deploy.sh` does, so `/trigger`, its secret and
`--allow-unauthenticated` all go away. A run with no NEW rows opens no browser and
logs in nowhere. The job's lock (`JOB_LOCK_SECONDS=3600`) stops two runs overlapping.

## Decisions made here

| Topic | Choice | Why |
|---|---|---|
| Browser | Local Chromium in the job | Same offers as Browserbase, about 2x faster, no outside account (run 09, run 11). Our Browserbase account is ours, not the client's. |
| State store | Firestore, Native mode, same project | The code is built for it (leases, login counters, unsaved RFQ lines). BigQuery is append-only and has no transactions; a port would be a rewrite. The Firestore API is **disabled** in the project today (checked 2026-10-30). |
| Service account | New `sourcing-worker@`, separate from `sourcing-ai-dev@` | The worker only reads secrets (checked: nothing in the worker writes one); the page only writes them. Each keeps only its own rights. |
| Code location | mie-agent, new folder `sourcing_worker/` (their per-app layout), own `deploy.sh`, image in `mie-dev` | Client's rule: code in their GitHub. Their Cloud Build doesn't build this folder. |
| What is not ported | `service/api.py`, `web_auth.py`, `credentials_ui.py`, OAuth settings, `/trigger`, `cloudbuild.yaml`, `infra/setup_gcp.sh` | Replaced by `sourcing_credentials_web` and Scheduler. |
| Proponent secret during testing | `VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test` on the job, the same as on `sourcing-ai-dev` | The page and the worker must point at the same secret, or a saved password never reaches the worker. |
| Aviall 2FA | No Live View at first: on the 2FA box the adapter opens the login URL again once (team's way, 2026-11-02), then the rows go `NEEDS_ATTENTION` with a Chat message | Live View needs Browserbase and a sign-in check in front of the link (`/hitl/view`, not built). Add both later if Aviall asks for 2FA often. |

## Readiness per vendor (2026-10-30)

| Vendor | Adapter | Live runs | Login secret | Ready for Cloud Run? |
|---|---|---|---|---|
| Proponent | `ProponentAdapter` | Runs 01-11 (up to 106 parts), bulk and one by one | Real login in `sourcing-vendor-test`; `sourcing-vendor-proponent` does not exist | Yes, after a hand check of offers against the portal (never done) |
| DASI | `DasiAdapter` + `dasi_prompt.py` | Run 12 (2026-10-29): 10-part bulk, 9 done, 1 not found; an earlier batch stopped at "couldn't tell whether the sign-in worked" (11 rows) | `sourcing-vendor-dasi` exists | After one clean run with the DASI adapter as built now, plus a hand check |
| Aviall | `AviallAdapter` (generic + Boeing Distribution / OneLogin buttons, login URL again on 2FA) | **None** | **`sourcing-vendor-aviall` does not exist** | No: needs the secret, then `scripts/onboard_vendor.py aviall` with 1-3 parts |

`scripts/check_access.py --vendor proponent,dasi,aviall` run locally 2026-10-30:
12/13 OK. The only failure is the missing Aviall secret.

## Phase 1: our work before anything is deployed

1. **Aviall onboarding** (after Skylink creates the secret): `onboard_vendor.py aviall`
   with 1-3 parts, outside busy hours. Save it as a `test_runs/13_…` folder.
2. **DASI**: one 10-20 part bulk run with the current adapter; the sign-in check must pass
   on every batch. Save it as a test run.
3. **Hand check of values** (never done so far): for 10 offers per vendor, compare
   price, qty, condition, lead time and trace against the portal page. Write the result
   into the test run folder. Any field that is wrong in 2 or more of the 10 blocks go-live
   for that vendor.
4. **Port to mie-agent** on a new branch `feat/sourcing-worker`:
   - `sourcing_worker/` gets `sourcing/`, `service/job_main.py`, the scripts the job runs,
     and the tests.
   - Dockerfile on their pinned `python:3.12-slim` digest plus Playwright Chromium,
     uid 1001; `requirements.txt` with ranges plus `constraints.txt` with exact pins.
   - `deploy.sh` in their style:
     - pytest first; refuse a dirty tree
     - `GIT_SHA` tag; `--update-env-vars` only
     - after deploy, check that the job runs as `sourcing-worker@`
   - README in their style.
   - No existing mie-agent file changes.
5. **Security pass**, as on the page:
   - `pip-audit` inside the image
   - bandit
   - `Settings` secret fields `repr=False` (a stray print must not log every key)
   - logs checked for password/username/TOTP values (only field names allowed)
6. **Add Aviall to the page**: one line in `sourcing_credentials_web/vendors.py`
   (`"aviall": {"name": "Aviall", "secret": "sourcing-vendor-aviall"}`), then a PR and
   redeploy of `sourcing-ai-dev`.

## Phase 2: Skylink's setup (their admin, one time)

| # | Ask | Needed for |
|---|---|---|
| 1 | Enable the Firestore API and create the `(default)` database, Native mode, `nam5` | Leases, counters, unsaved RFQ lines |
| 2 | Create service account `sourcing-worker@` | Job identity |
| 3 | Give it `secretmanager.secretAccessor` on `sourcing-*` only (condition on the name prefix) | Read logins and its API keys |
| 4 | Give it `datastore.user` | Firestore |
| 5 | Share the request sheet with `sourcing-worker@` as Editor; tell us the sheet id | Read rows, write RFQ lines |
| 6 | Create secret `sourcing-vendor-aviall` (`username`, `password`, `url`) | Aviall login |
| 7 | Create `sourcing-anthropic-api-key` from Skylink's Anthropic account | Reading result pages |
| 8 | Create `sourcing-chat-webhook-url` (incoming webhook into the team's space) | Problem and 2FA messages |
| 9 | Create an empty job `sourcing-worker` (any image) and give us `run.developer` on it, plus `iam.serviceAccountUser` on `sourcing-worker@` | We deploy new versions, as on `sourcing-ai-dev` |
| 10 | Create Scheduler job `sourcing-worker-sweep` (OAuth as `sourcing-worker@`, which needs `run.invoker` on the job), created **paused** | Automatic runs |
| 11 | Confirm the Sheets API is enabled (we cannot list services) | Sheet access |
| 12 | Busy hours for each portal, and the schedule (proposed: every 30 min, 06:00-20:00 ET, Mon-Fri) | Client rule 4 |
| 13 | Go-live: create `sourcing-vendor-proponent` with the real login, or keep using the test secret | Proponent |

## Phase 3: first deploy and checks on Cloud Run

Every gate must pass before the next. A failure stops the rollout until it's fixed.

| Gate | Command / action | Pass when |
|---|---|---|
| G1 tests | `deploy.sh` runs pytest | All green (`test_data_entry_rules.py` included) |
| G2 image | `deploy.sh` builds and pushes to `mie-dev`, tag = commit | Pushed; not `-dirty` |
| G3 dry run | `gcloud run jobs execute sourcing-worker --update-env-vars=DUMMY_MODE=1 --wait` | Exit 0; fake offers on a **test** sheet; no portal touched |
| G4 access, as the job | `gcloud run jobs execute sourcing-worker --args=scripts/check_access.py,--vendor,proponent,dasi,aviall --wait` | 13/13 OK: env, sheet, Firestore, 3 secrets, 3 adapters, Chromium in 2Gi, LLM |
| G5 one part per vendor | 3 rows (one per vendor) on the test sheet, run once, outside busy hours | Each row `DONE` or `NOT_FOUND` with RFQ lines; values match the portal by hand |
| G6 logs | `gcloud logging read` for the run | No password, username, 2FA key or API key in any line |
| G7 failure path | Set a wrong password version on the **test** secret, run one Proponent row, then restore it through the page | Row stops with a Chat message and no second login attempt (lockout guard); restored login works |
| G8 schedule | Resume `sourcing-worker-sweep` for one day on the test sheet | Runs at the set times; empty sweeps open no browser; no overlap |
| G9 real sheet | Point `SHEET_ID` at the team's sheet; tell the team | First real rows done; team confirms results |

## Phase 4: go-live switch

1. Proponent: if Skylink made `sourcing-vendor-proponent`, remove `VENDOR_SECRET_OVERRIDES`
   **on the job and on `sourcing-ai-dev` in the same hour** (otherwise the page saves to
   one secret while the worker reads another).
2. Set the real LLM model/effort on the job (run 12: opus medium ≈ $0.017 per part,
   DASI ≈ $0.04 per part).
3. Tell the team: add rows with Status `NEW`; Chat says when a login needs a person; the
   page link changes a password; after a password change, set stopped rows back to `NEW`.

## Rollback

- Stop searching: `gcloud scheduler jobs pause sourcing-worker-sweep` (Skylink, or us if granted).
- Bad build: redeploy the previous tag (`deploy.sh` prints it); a job has no traffic split.
- Bad password save: the page adds a new version with the old value (older versions stay).
- Stuck rows: leases expire after `JOB_LOCK_SECONDS`; a row left `RUNNING` goes back to `NEW` by hand.

## Cost (per month, rough)

- Cloud Run job: a few minutes of CPU per run with rows; empty sweeps ≈ seconds. Under $5.
- Firestore: small documents, far under the free tier.
- LLM: $0.002-0.04 per part, depending on model and vendor.
- Browserbase: $0 (not used).

## Known risks

- Aviall has never been tried on the live portal; the sign-in button wording
  ("Log in to Boeing Distribution", "Log in with Boeing OneLogin ID") and the home page
  address are from the team's description.
- All three logins are shared with team members who use them daily. A run may sign them
  out if the portal allows one session. Not seen on Proponent so far; unknown for DASI
  and Aviall.
- Sheets quota is 60 reads per minute per user; bulk batches are saved in 3 writes, and
  a 429 waits and retries.
- `--task-timeout=3600`: one run handles at most about 400 bulk parts; the next sweep
  takes the rest.
