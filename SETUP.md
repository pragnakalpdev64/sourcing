# Developer setup

Takes about 10 minutes to a passing test suite and a dry run. Real portal runs need keys from the
project lead (step 4).

## 1. Prerequisites

- Linux or macOS, `git`
- Python **3.11 or 3.12** (3.10 works but Google's libraries print a deprecation warning; the Docker image uses 3.12)
- For real runs only: the Google Cloud CLI (`gcloud`) and access granted by the project lead

## 2. Install and run the tests

```bash
git clone <repo-url> sourcing-proto
cd sourcing-proto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium-headless-shell   # used by the Proponent adapter tests
.venv/bin/python -m pytest -q                                    # expect: 133 passed
```

The tests use fakes and saved mock pages: no network, no keys.

## 3. Dry run (no keys needed)

Runs the whole pipeline on a local Excel file with a fake vendor. The run writes statuses and
results **into** the file, so work on a copy:

```bash
cp .env.example .env
cp samples/requests_20-parts_proponent.xlsx my_test.xlsx
```

In `.env` set:

```
SHEET_ID=my_test.xlsx
STORE_BACKEND=memory
```

```bash
.venv/bin/python -m service.job_main --dummy
```

Expected end of the log: `Run summary: {... 'rows_total': 20, 'counts': {'DONE': 20} ...}`.
Open `my_test.xlsx`: Status is `DONE` on every row and `Results` has fake offers.
To run it again, clear the Status column (or copy the sample again). Close the file in Excel/LibreOffice before a run.

## 4. Real Proponent run

Ask the project lead for these (sent privately, **never committed**):

| `.env` key | What |
|---|---|
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | Browserbase account (remote browser) |
| `ANTHROPIC_API_KEY` | Claude API key |
| Google account with read access to `sourcing-vendor-*` secrets in GCP project `skylink-material-planning` | Proponent login is read from Secret Manager, never stored locally |

Then:

```bash
gcloud auth application-default login      # the Google account that was given access
```

`.env`:

```
DUMMY_MODE=0
STORE_BACKEND=file                 # keeps the browser Context + learned selectors between runs
SHEET_ID=my_test.xlsx              # or a Google Sheet, see README "Sheet" section
VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test
LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-5-5         # cheapest; see test_runs/README.md for sonnet/opus numbers
LLM_EFFORT=medium
```

Check every dependency without printing any secret:

```bash
.venv/bin/python scripts/check_access.py
```

Run, keep the log, and make the per-part report:

```bash
.venv/bin/python -m service.job_main 2>&1 | tee run.log
.venv/bin/python scripts/run_report.py --csv report.csv
```

Useful debug switches in `.env`: `BROWSERBASE_RECORD_SESSIONS=1` (replay link per session in the log;
shows the login page, local use only) and `DEBUG_DUMP_PAGES=1` (saves what the model read to `debug_pages/`).

### Rules for the live portal (from the client)

- Team members use the **same Proponent login every day**. Run only a few searches at a time, outside their busy hours.
- Everything runs in Skylink's GCP project `skylink-material-planning`. Do not create or bill another GCP project.
- New vendor secrets are created by the Skylink admin; our access cannot list secrets, so always use the exact name.

## 5. Where things are

| Read this | For |
|---|---|
| `README.md` | Features, sheet format, Secret Manager, deploy to Cloud Run |
| `docs/PIPELINE.md` | How a row moves through the system, step by step |
| `docs/SourcingAI_CloudRun_Prototype_Plan.md` | The plan this code follows (steps 0–7) |
| `test_runs/README.md` | Every real run so far: results, cost, time per part |
| `CLAUDE.md` | Project context for AI coding assistants (also a good one-page overview) |

## 6. Deploying

See "Deploy" in `README.md` (`infra/setup_gcp.sh`, then `gcloud builds submit --config cloudbuild.yaml`).
Not deployed yet; coordinate with the project lead before running anything in `skylink-material-planning`.
