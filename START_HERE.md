# SourcingAI prototype: quick start

The `.env` with working keys is included. Do not commit it or forward it.

## 1. Install (about 5 minutes, Python 3.11 or 3.12)

```bash
cd sourcing-proto
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m playwright install chromium-headless-shell
.venv/bin/python -m pytest -q          # expect: all tests pass (no network, no keys)
```

## 2. Dry run (fake vendor, no portal, no cost)

```bash
cp samples/requests_20-parts_proponent.xlsx my_test.xlsx
.venv/bin/python -m service.job_main --dummy --sheet my_test.xlsx
```

Open `my_test.xlsx`: every row should be `DONE`, with fake offers in the `Results` tab.

## 3. Real Proponent run (optional)

Needs Google access to Secret Manager in GCP project `skylink-material-planning` (the vendor login
is read from there). Please search only a few parts, outside the team's busy hours.

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets
.venv/bin/python scripts/check_access.py                    # checks every dependency, prints no secrets
.venv/bin/python -m service.job_main --sheet my_test.xlsx   # bulk search (--one-by-one for per part)
.venv/bin/python scripts/run_report.py                      # time / tokens / cost per part
```

`--browser local` uses Chromium on your machine instead of Browserbase.

## What to read

- `CLAUDE.md`: overview and status against the plan
- `docs/PIPELINE.md`: how the code works
- `test_runs/README.md`: results of real runs so far
- `SETUP.md`, `COMMANDS.md`: full setup and every command and flag
