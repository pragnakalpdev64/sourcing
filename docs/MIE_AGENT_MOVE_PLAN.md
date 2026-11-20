# Moving SourcingAI into mie-agent: clean-up and move plan

Written 2026-11-20. Replaces the "Port to mie-agent" step of `SOURCING_WORKER_DEPLOY_PLAN.md`
(that plan predates the add-on, the sheet relay and IAP on `sourcing-ai-dev`).

Goal: one folder in the client's `Skylink-Inc/mie-agent` repo that holds the whole of
SourcingAI (library, service, worker, add-on, tests, docs), built as **one image** that runs
the login update page and the sourcing API on the **same Cloud Run service**,
`sourcing-ai-dev`. Nothing is committed or pushed to mie-agent until the user says so.

## 1. What mie-agent looks like today (checked 2026-11-20)

- `main` is at `02d76ce` (PR #85). The engine (`main.py`, `tools/`, `gateway/`) builds from the
  root `Dockerfile` through `cloudbuild.yaml` on every push to `main`. That build copies only
  named folders (`main.py tools/ prompts/ knowledge/ scripts/`), and its tests are named
  files in root `tests/`. **A new top-level folder is ignored by their build, tests and deploy.**
- Each side app is one top-level folder with its own `Dockerfile`, `requirements.txt` +
  `constraints.txt`, `deploy.sh`, `README.md` and `tests/`: `rfq_planner_web/`,
  `cloud_run/vendor_quote_watch/`, and ours, `sourcing_credentials_web/`.
- Their image rules: base `python:3.12-slim@sha256:…` pinned by digest, non-root user uid 1001,
  `pip install -r requirements.txt -c constraints.txt`, deploy scripts that refuse a dirty tree,
  tag the image with the commit, and use `--update-env-vars` only.
- **`sourcing_credentials_web/` is not on `main`.** It lives on our branch
  `feat/sourcing-credentials-update-page` (3 commits: `4c6189b`, `1368875`, `8773c69`), which is
  what `sourcing-ai-dev` serves now. It must be merged together with (or before) this move.
- Root `.gitignore` ignores **every `*.json`**. Our `apps_script/addon/appsscript.json` (the
  add-on manifest) would silently be left out; the folder needs its own `.gitignore` with
  `!appsscript.json`.
- Root `CLAUDE.md` is about the MIE engine (BigQuery, calculator). Our folder gets its own
  `CLAUDE.md`, which Claude Code reads when working inside the folder.

So the per-app folder shape is right for us, as long as nothing outside our folders changes.

## 2. One service for the login page and sourcing: yes

`sourcing-ai-dev` already runs the login page at 100% traffic, and our API already ran on it
as a tagged no-traffic revision (2026-11-19/20, `infra/deploy_test_revision.sh`). To run both
on the same address, the service needs **one image that serves both**:

| Path | Served by |
|---|---|
| `/`, `/credentials`, `/credentials/new`, `/credentials/{vendor}` | `sourcing_credentials_web` (unchanged, IAP sign-in) |
| `/trigger`, `/sheet/pull` (also returns progress), `/sheet/ack`, `/hitl/code`, `/chat/events`, `/health` | `service/api.py` |

How: `service/api.py` keeps its own routes and mounts the login page app last
(`app.mount("/", credentials_app)`), so its `/` and `/credentials*` answer and our `/health`
wins. Both already read the person from IAP's signed header with the same audience. The
image copies `sourcing_credentials_web/` next to our code through a named build context
(`docker buildx build --build-context creds=../sourcing_credentials_web`), so that folder is
not moved or edited.

Consequences:
- **One deploy script for the service.** `sourcing_credentials_web/deploy.sh` would put the
  page-only image back and drop the sourcing API. Its README gets one line pointing to ours,
  and its `deploy.sh` exits with that message (our own file on our branch, so allowed).
- Our older login page (`service/web_auth.py`, `service/credentials_ui.py`, Google OAuth
  sign-in, ~2,000 lines) is **removed**: behind IAP it duplicates the live page.
- Service account stays `sourcing-ai-dev@`. Extra rights the sourcing part needs (most were
  already used by the test revision): read `sourcing-anthropic-api-key`,
  `sourcing-browserbase-*`, `sourcing-chat-webhook-url`; `datastore.user` in
  `sourcing-skylink-addon` (relay Firestore); later `run.jobs.run` on the worker job.

**The search itself should not run inside the service for real use.** Today the test
revision starts the worker as a child process (`TRIGGER_RUN_LOCAL=1`, CPU always on, one
instance). A search can take 15+ minutes, and Cloud Run stops an instance on every deploy and
may stop an idle one: the run would be cut off mid-search and, worse, **without logging out of
the vendor portal**. For real use the worker is a **Cloud Run job from the same image**
(`python -m service.job_main`), started by `/trigger` (code exists: `_start_job`). Same folder,
same image, same service account; the client creates the empty job once and gives us
`run.developer` on it, as they did for the service. Until then, the in-service worker stays for
testing with a dummy or one small vendor run.

## 3. Clean-up (in this repo first, branch `sourcing-vendor-automation-agent`)

Done here so our history keeps every step, tests green after each group (module tests per
change, full suite at the end of each group).

### 3a. Delete (stays in our backup repo's history)

| What | Why |
|---|---|
| `service/web_auth.py`, `service/credentials_ui.py`, their tests, `cryptography` dependency, OAuth settings in `sourcing/config.py` | Replaced by `sourcing_credentials_web` behind IAP |
| `sourcing/credentials/access.py`, `scripts/manage_logins.py`, the login register in `store.py` | Only the old page used them; the live page has `CREDENTIAL_EDITORS` / `CREDENTIAL_ADMINS` |
| `/hitl/view`, `/hitl/done` (501 stubs) | Not built; plan step stays in docs |
| `apps_script/per_sheet/` | Retired 2026-11-20 (IAP refuses secret-only calls) |
| `cloudbuild.yaml`, `infra/setup_gcp.sh`, `infra/secrets.env.example` | Built for our own `sourcing-api` design; replaced by `deploy.sh` |
| `scripts/ils_probe.py`, `scripts/ils_part_search.py` | ILS exploration, no adapter uses them (kept in backup repo) |
| `test_3parts.xlsx`, `sub_wh_test_5.xlsx` (root) | Local test sheets; `samples/` has the clean one |
| Root `*.log`, `debug_pages*/` | Local only (already ignored) |
| Superseded docs: `CLOUD_RUN_DEPLOY_PLAN.md`, `NOT_YET_IN_MAIN_COPY.md`, `SourcingAI_Prototype_Implementation_Plan.md`, `IAP_TEST_IN_OWN_PROJECT.md`, `old_mcp_prototype_proponent_results.json`, `CREDENTIAL_UPDATE_UI_PLAN.md` | History; the code and the docs below say what is true now |
| `START_HERE.md`, `SETUP.md`, `AGENTS.md` | Merged into the new `README.md` |

Kept after a look: `scripts/passkey_spike.py` (the passkey scripts sign in with it; Aviall),
`docs/GENERIC_VENDOR_PLAN.md` (the generic adapter's code points to its phases).

### 3b. Decide and simplify

| Item | Decision |
|---|---|
| OpenAI / Gemini clients (`sourcing/llm/`) | Remove. Never tried on real pages, empty price tables. The provider interface stays, so one can be added back. |
| `/chat/events` (Chat-app card) | Keep, behind its existing check; it waits on the client's Chat app. |
| `REMOVE-IF-PER-PART-DROPPED` code | Keep; only goes if the senior drops per-part search. |
| Dead code | Run `vulture` + `ruff check --select F401,F841` and remove what nothing calls; review each hit, don't bulk-delete. |
| Settings | `Settings` secret fields `repr=False`; one place that lists every env var (`.env.example`), checked by a test. |
| `requirements.txt` | Ranges as now, plus `constraints.txt` with the exact versions the tests passed on (their convention). |
| `CLAUDE.md` | Shortened to context + rules + gotchas; the long per-vendor notes move to each adapter's docstring and `docs/VENDORS.md`. |
| `test_runs/` | Not moved (146 files, vendor and price data). One summary table, `docs/TEST_RUNS_SUMMARY.md`. |

### 3c. Security pass before anything leaves our repo

- `gitleaks detect` (or a grep for keys, `hooks.googleapis.com` webhook URLs, tokens) over the
  folder to be copied; the copy carries no history, so only the current files matter.
- `bandit -r sourcing service`, `pip-audit` inside the built image.
- One worker run's logs checked: no password, username, TOTP secret or API key values.

## 4. Target layout in mie-agent

```
sourcing_ai/                   new, everything SourcingAI except the login page
  README.md  CLAUDE.md
  Dockerfile  .dockerignore  .gcloudignore  .gitignore   (! appsscript.json)
  requirements.txt  constraints.txt  requirements-dev.txt  pytest.ini  .env.example
  deploy.sh                    service sourcing-ai-dev: API + login page, one image
  deploy_job.sh                worker job sourcing-worker (once the client creates it)
  sourcing/                    library: pipeline, vendors/, auth/, browser/, llm/, sheets, store…
  service/                     api.py (mounts the login page), job_main.py, iap.py
  apps_script/addon/           Sheets add-on
  scripts/                     check_access, onboard_vendor, manage_sheets, manage_vendors,
                               check_page_health, run_report, vendor_limits, passkey_*
  infra/                       firestore_ttl.sh
  samples/                     20-part request sheet
  docs/                        PIPELINE.md, COMMANDS.md, NEW_VENDOR_CHECKLIST.md, DATA_ENTRY_RULES.md,
                               VENDORS.md, CLIENT_IAP_AND_ACCESS_STEPS.md, TEST_RUNS_SUMMARY.md, …
  tests/
sourcing_credentials_web/      unchanged (from our branch), served inside the same image
```

The Python package names (`sourcing`, `service`) stay as they are: inside the folder they do
not clash with anything, and renaming them would touch every import and test for no gain.
Tests run from the folder (`cd sourcing_ai && pytest`); their root `pytest tests/` never sees them.

## 5. Order of work

1. **Finish open work**: commit the passkey scripts and test now untracked (`passkey_setup.py`,
   `passkey_from_devtools.py`, `test_passkey_from_devtools.py`) in our repo.
2. **Clean-up** (section 3) on `sourcing-vendor-automation-agent`; full suite green; push to our backup repo.
3. **One-image service** (section 2) here first: mount the login page app, Dockerfile in their
   style (pinned digest, uid 1001, constraints), `deploy.sh` replacing
   `infra/deploy_test_revision.sh`. Test locally with `IAP_DEV_EMAIL` for the page and the
   add-on mocks for the API.
4. **Test revision on `sourcing-ai-dev`** with no traffic (tag `sourcing`): page and API both
   answer on the tag address; dummy run from the add-on through the relay. The live page keeps
   100% traffic.
5. **mie-agent branch** `feat/sourcing-ai`, made from our credentials branch rebased on
   current `main`; copy `sourcing_ai/` in (files only, no history); tests green inside the
   folder; their root tests still green (nothing of theirs changed).
6. **User review, then push and PR** (only when the user says): one PR with the credentials
   page commits and `sourcing_ai/`, description in their style.
7. **Switch traffic**: deploy from the mie-agent checkout with `deploy.sh`, move 100% to it;
   check `/credentials` and the add-on's Search now. Rollback = previous revision to 100%.
8. **Worker job**: when the client has created `sourcing-worker` and the rights, `deploy_job.sh`
   and set `TRIGGER_RUN_LOCAL` off on the service.
9. **From then on** new work happens in mie-agent; this repo is kept read-only as history.

## 6. Asks to the client (only what the move itself needs)

| Ask | For |
|---|---|
| Review and merge the PR (they own `main`) | Code in their GitHub |
| Create empty job `sourcing-worker`, `run.developer` on it for us, `run.jobs.run` for `sourcing-ai-dev@` | Step 8 |
| `datastore.user` for `sourcing-ai-dev@` in `sourcing-skylink-addon`, if not already | Relay store |
