# SourcingAI: Vendor Part Price Extraction

Looks up aviation spare-part prices on vendor portals for the Skylink team.

A team member adds rows to a Google Sheet: part number, qty, condition and vendor. SourcingAI then:

1. signs in to the vendor's portal with Skylink's login
2. searches the parts
3. has an LLM read the results page into a fixed schema
4. checks every value against the page
5. writes the offers to the sheet's `RFQ` tab

**SourcingAI recommends. A person decides.** It never places an order, sends an RFQ or changes
anything on a vendor portal. It only searches and reads. It has no write path to AvSight.

## Where it runs

| What | Where |
|---|---|
| Service | Cloud Run `sourcing-ai-dev`, `skylink-material-planning`, `us-central1`, behind IAP (Google sign-in, @skylinkintl.com only), runs as `sourcing-ai-dev@` |
| Worker | Cloud Run job `sourcing-worker`, same image and service account |
| Images | `us-central1-docker.pkg.dev/skylink-material-planning/mie-dev/sourcing-ai` |
| Vendor logins and keys | Secret Manager, `skylink-material-planning`, names `sourcing-*` |
| State | Firestore in `sourcing-skylink-addon` (Skylink's add-on project: no billing, no secrets) |
| Sheets add-on | `apps_script/addon/`, listed in `sourcing-skylink-addon` |

The engine's `cloudbuild.yaml` does not build, test or deploy this folder.

## How it works

```
Google Sheet ── add-on: on edit, POST /trigger with a copy of the rows
     ▲                      │ HTTPS + IAP
     │ add-on writes         ▼
     │ the results    sourcing-ai-dev (one image)
     └── /sheet/pull ── service/api.py            /trigger /sheet/pull /sheet/ack /hitl/code /health
                        sourcing_credentials_web  /credentials (team changes vendor passwords)
                              │ starts
                              ▼
                      job sourcing-worker (service/job_main.py)
                        rows → vendor login → search → LLM reads page → values checked → RFQ lines
                          ├─ Secret Manager  sourcing-vendor-<vendor> logins, sourcing-* keys
                          ├─ Browser         Browserbase, or Chromium in the container
                          ├─ Anthropic API   reads result pages
                          ├─ Firestore       leases, relay, progress
                          └─ Google Chat     run summary; "a person must sign in" messages
```

The service never opens the sheet, because Skylink's organisation blocks the service account:
- the add-on sends the rows with each call;
- the worker's results wait in Firestore;
- the add-on writes them back to the sheet (`sourcing/sheet_relay.py`).

A run takes from seconds to 15+ minutes, so the search runs as a job, not inside a request.

Details: [docs/PIPELINE.md](docs/PIPELINE.md).

## Vendors

Type one of these names in the sheet's **Vendor** column. Case, spaces and punctuation don't
matter (`Aircraft Spruce`, `aircraft-spruce` and `AIRCRAFT SPRUCE` are the same).

| Vendor | Names accepted in the sheet | Portal | Login secret |
|---|---|---|---|
| Proponent | Proponent, ProCart | procart.proponent.com | `sourcing-vendor-proponent` |
| DASI | DASI | store.dasi.com | `sourcing-vendor-dasi` |
| Wencor | Wencor | fly.wencor.com | `sourcing-vendor-wencor` |
| Incora (Wesco) | Incora, Shop Incora, Wesco, Wesco Aircraft | shop.incora.com | `sourcing-vendor-incora` |
| Textron Aviation | Textron Aviation, Textron, TxtAv | ww2.txtav.com | `sourcing-vendor-textron-aviation` |
| AAR | AAR, AAR Corp, AARCorp | shop.aarcorp.com | `sourcing-vendor-aar` |
| Monroe Aerospace | Monroe Aerospace, Monroe | monroeaerospace.com | `sourcing-vendor-monroe` |
| Aircraft Spruce | Aircraft Spruce, Aircraft Spruce Specialty, Spruce | aircraftspruce.com | `sourcing-vendor-aircraft-spruce` |
| Aviall (Boeing Distribution) | Aviall | shop.boeing.com | `sourcing-vendor-aviall` |

Any other vendor name: if an admin has added that vendor's login (secret `sourcing-vendor-<name>`
with the portal's `url`, or `scripts/manage_vendors.py`), the generic adapter tries it; check it
first with `scripts/onboard_vendor.py <name>`. Otherwise its rows stop with the note
"Vendor '<name>' is not supported yet".

### Progress (2026-11-20)

| Vendor | State | Real runs so far | Known limits |
|---|---|---|---|
| Proponent | **Working** | Many runs since 2026-10-12, up to 106 parts; also through the add-on (2026-11-16) | Offers not yet hand-checked against the portal |
| DASI | **Working** | 10-part run: 9 done, 1 not found (2026-10-29) | DASI Only + Miami HQ stock only (team's rule) |
| Wencor | **Working** | 10-11 parts, all done (2026-11-03); 3 parts (2026-11-04) | Substitutes off; alternates go in Internal Comments |
| Incora (Wesco) | **Working** | 17 parts with price breaks (2026-11-05); with Proponent and Monroe (2026-11-06) | Delivery set to 7 days for in-stock lines (client) |
| Textron Aviation | **Working, needs a person for 2FA** | 3 and 19 parts (2026-11-12) | A team member types the emailed code; the program sends it |
| AAR | **Working, login can be blocked** | 4 parts, all done (2026-11-04) | reCAPTCHA sometimes refuses the login; retried, then the rows wait |
| Monroe Aerospace | **Working** | 3 parts (2026-11-06) | Only exact part-number matches are kept |
| Aircraft Spruce | **Built, not yet run signed in** | None | Its image CAPTCHA goes to a person; login secret not created yet |
| Aviall (Boeing) | **Blocked: passkey sign-in** | 20 parts (2026-11-02, before Boeing's change) | Boeing now signs in with a passkey only; the program needs its own passkey set up |

Each vendor has its own adapter (`sourcing/vendors/<vendor>.py`: login, search, logout) and its
own extraction prompt (`<vendor>_prompt.py`). Per-vendor detail is in
[docs/VENDORS.md](docs/VENDORS.md). Before adding a vendor, read
[docs/NEW_VENDOR_CHECKLIST.md](docs/NEW_VENDOR_CHECKLIST.md).

## Layout

```
sourcing_ai/
  sourcing/            the library
    pipeline.py          rows → accounts → login → search → record (start here)
    extract.py           LLM prompt, JSON schema, value check against the page
    vendors/             one adapter (+ prompt) per vendor; base.py, web.py, generic.py
    auth/                login ladder, account states, 2FA handoff
    browser/             browserbase.py | local.py
    llm/                 Anthropic client + price table
    credentials/         Secret Manager reader
    sheet_relay.py, progress.py, store.py, sheets.py, sinks/, notify/
  service/
    api.py               FastAPI service; mounts sourcing_credentials_web
    job_main.py          worker entrypoint
    iap.py               checks IAP's signed header
  apps_script/addon/   Google Sheets add-on
  scripts/             admin and onboarding tools (laptop only, not in the image)
  infra/               Firestore TTL setup
  samples/             20-part request sheet for local runs
  docs/                how it works, rules, vendors, test-run summary
  tests/               fakes and saved mock pages; no network
  deploy.sh            service: API + password page
  deploy_job.sh        worker job
```

## Run it locally

Python 3.12, from this folder:

```bash
cd sourcing_ai
python -m venv .venv
.venv/bin/pip install -r requirements.txt -c constraints.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium-headless-shell
.venv/bin/python -m pytest -q          # must stay green before any deploy
```

### Dry run

This is a fake vendor with no browser, no keys and no cost. The run writes into the file, so work on a copy:

```bash
cp .env.example .env                   # set SHEET_ID=my_test.xlsx, STORE_BACKEND=memory
cp samples/requests_20-parts_proponent.xlsx my_test.xlsx
.venv/bin/python -m service.job_main --dummy
```

Every row should end `DONE`, with fake offers.

Run the tests from this folder, not from the mie-agent root: a bare `pytest` there would
also collect these tests with the engine's (the engine's CI runs only its own named files).

### Real runs

A real run needs:
- `LLM_PROVIDER`, `LLM_MODEL` and `LLM_EFFORT`. None has a default: a run stops if one is missing.
- A gcloud login that can read the `sourcing-vendor-*` secrets.

```bash
.venv/bin/python -m service.job_main --browser local      # real run on this machine
.venv/bin/python scripts/run_report.py                     # time, tokens, cost per part
.venv/bin/python scripts/onboard_vendor.py <vendor>        # first run for a new vendor
```

Every command and flag: [docs/COMMANDS.md](docs/COMMANDS.md).

**Live portals:** do few searches, outside busy hours. Team members use the same logins daily, and
a session that isn't logged out can block their sign-in.

## Settings and secrets

All settings come from environment variables. [.env.example](.env.example) lists every one.
Nothing secret is in code, in `.env` on Cloud Run, or in this repo.

| Secret | Holds |
|---|---|
| `sourcing-vendor-<vendor>` | Shared login: `{"url", "username", "password", "totp_secret"?}` |
| `sourcing-vendor-<vendor>__<email-with-dashes>` | One person's own login, tried first |
| `sourcing-anthropic-api-key`, `sourcing-browserbase-api-key`, `sourcing-browserbase-project-id`, `sourcing-chat-webhook-url` | Infra keys |

The team changes vendor passwords on `/credentials`. A save adds a new secret version, and the
worker reads the latest version at its next login. Skylink's admin creates new vendor secrets.

## Deploy

Same rules as the other apps in this repo:
- deploy from a clean checkout;
- the image is tagged with the commit;
- settings change with `--update-env-vars` / `--update-secrets` only, never `--set-*`.

```bash
./sourcing_ai/deploy.sh                # tests, build, push, new revision of sourcing-ai-dev
./sourcing_ai/deploy_job.sh            # tests, build, push, update job sourcing-worker
```

Check:

```bash
gcloud run services describe sourcing-ai-dev --region=us-central1 \
  --project=skylink-material-planning \
  --format="value(status.latestReadyRevisionName,spec.template.spec.containers[0].image)"
```

Then open `/credentials` and run "Search now" from the add-on on a test sheet.

### Roll back

Move traffic back to the previous revision:

```bash
gcloud run services update-traffic sourcing-ai-dev --region=us-central1 \
  --project=skylink-material-planning --to-revisions=<REVISION>=100
```

For the job, run `deploy_job.sh` from the previous commit.

IAP setup (who may sign in, the add-on's client):
[docs/CLIENT_IAP_AND_ACCESS_STEPS.md](docs/CLIENT_IAP_AND_ACCESS_STEPS.md).

## Rules that must hold

- Vendor logins live only in Secret Manager, project `skylink-material-planning`.
- No new GCP project, and no billing outside Skylink's projects.
- A live portal session is logged out, and the logout confirmed, before the browser closes.
- Every value the LLM returns must be checkable against the page.
- Vendor-specific behaviour lives in the adapter or the vendor config, never in the pipeline.
- The client's data entry rules ([docs/DATA_ENTRY_RULES.md](docs/DATA_ENTRY_RULES.md)) are tests:
  `tests/test_data_entry_rules.py`, one test per rule.

## More

- [CLAUDE.md](CLAUDE.md): context, status and gotchas for this folder (also read by AI agents)
- [docs/PIPELINE.md](docs/PIPELINE.md): how a row moves through the code
- [docs/COMMANDS.md](docs/COMMANDS.md): every command and flag
- [docs/NEW_VENDOR_CHECKLIST.md](docs/NEW_VENDOR_CHECKLIST.md): adding a vendor
- [docs/TEST_RUNS_SUMMARY.md](docs/TEST_RUNS_SUMMARY.md): what the real runs showed (time, cost, results)

Proprietary: Skylink, Inc. All rights reserved.
