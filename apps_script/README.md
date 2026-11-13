# Sheet trigger (Apps Script)

A row added to a request sheet's Requests tab starts a search for that sheet. The
offers go back into the same sheet, so each person can work in their own sheet.
Requested By is filled in with the editor's email when it's empty. The Cloud
Scheduler sweep stays as the backstop for rows that were missed.

Two ways to put it in a sheet:

| | `addon/` (Sheets add-on, the plan) | `per_sheet/` (script inside one sheet) |
|---|---|---|
| Who sets it up | The admin installs it once for Skylink | Someone, in every sheet |
| Each person | Extensions > SourcingAI > Turn on for this sheet | — |
| Proves the call | The user's Google token (Skylink account, this add-on) | `TRIGGER_SECRET`, readable by anyone who can edit the script |
| 90 s quiet wait | In the service (add-ons may run a timer only once an hour) | In the script (time trigger) |
| Needs | Its own small GCP project for the Marketplace listing (no secrets, no billing) | Nothing |

Either way, a search already running when the call comes reads the sheet once more
before it ends (`RERUN_FLAG`, `service/job_main.py`, at most 5 passes).

**Every sheet must be shared with the worker's service account as Editor.** The
worker opens it later as that account, not as the person.

## Add-on (`addon/`)

### Once, by the developer

1. In the add-on's GCP project (the listing project, not `skylink-material-planning`:
   that one already has the MIE Assistant listing):
   - OAuth consent screen: **Internal**.
   - Enable **Google Workspace Marketplace SDK**.
   - Note the **project number**.
2. At script.google.com, a **standalone** project (New project, not from a sheet),
   owned by a Skylink account:
   - Paste `addon/Code.gs`. In Project Settings tick "Show appsscript.json" and paste
     `addon/appsscript.json`.
   - Project Settings > Google Cloud Platform project > **Change project** > the project number.
   - Project Settings > Script properties: `TRIGGER_URL` (`https://<host>/trigger`),
     `SERVICE_ACCOUNT_EMAIL` (shown to people when they turn it on), optional `REQUESTS_TAB`.
3. The service's settings (`.env` locally, env vars on Cloud Run):
   - `ADDON_OAUTH_CLIENT_IDS`: the OAuth client id Apps Script made in the listing
     project (Google Cloud console > APIs & Services > Credentials, the one named
     "Apps Script"). Not sure which one: make a test call; the API logs
     "token for OAuth client <id>, not the add-on's", and that id is the one.
   - `ALLOWED_EMAIL_DOMAINS`: Skylink's domain (shared with the login update page).
   - `TRIGGER_QUIET_SECONDS`: default 90.
4. Try it: **Deploy > Test deployments > Install type: Editor add-on**, pick a test
   sheet, open it, then Extensions > SourcingAI > Turn on for this sheet.
   **A test deployment doesn't run the edit trigger** (Google: installable triggers
   aren't supported when testing; seen 2026-11-13: Search new rows now worked, a new
   row started nothing). Test the call and the run with **Search new rows now**; the
   automatic start only works once the add-on is installed (step 5–6).
5. Publish: **Deploy > New deployment** (type Add-on). In the Marketplace SDK's App
   Configuration: Editor add-on, Sheets, the script ID and that deployment, visibility
   Private. Fill in the Store listing.
6. The admin installs it for the domain from the Admin console.

A new version of `Code.gs`: paste it, make a new deployment, and point the Marketplace
SDK at it. People don't install anything again.

### Each person, in their own sheet

1. Extensions > SourcingAI > **Turn on for this sheet**. It checks the Requests tab
   has a Part Number column and installs the edit trigger. The trigger runs as this person.
2. Share the sheet with the service account it shows, as Editor (once per sheet).
3. Add rows. The search starts about 90 seconds after the last edit. **Search new
   rows now** starts at once.

## Per-sheet script (`per_sheet/`)

1. Open the sheet, then **Extensions > Apps Script**.
2. Replace `Code.gs` with `per_sheet/Code.gs`.
3. In **Project Settings**, tick "Show appsscript.json". Replace that file with `per_sheet/appsscript.json`.
4. Still in **Project Settings > Script properties**, add:
   - `TRIGGER_URL`: `https://<host>/trigger` (the Cloud Run URL, or the ngrok URL while testing)
   - `TRIGGER_SECRET`: the service's `TRIGGER_SECRET`
   - optional `REQUESTS_TAB` (default `Requests`) and `DELAY_SECONDS` (default `90`)
5. In the editor, pick `setup` and press **Run**. Allow the permissions it asks for.
   The trigger runs as the person who ran `setup`.
6. Reload the sheet. A **SourcingAI > Search new rows now** menu appears. It calls `/trigger` without the wait.

Anyone who can edit the script can read `TRIGGER_SECRET`. All it does is start a search run.

## Check that a sheet is set up

In the script editor (the sheet's own, or the add-on's for a test deployment):

- **Triggers** (clock icon): one `onRequestsEdit`, "From spreadsheet – On edit".
- **Executions**, after adding a row: `onRequestsEdit` with the log line
  "SourcingAI /trigger: ..." (per-sheet script: `sendTrigger` about 90 s later).
- **Project Settings > Script properties**: the properties above.

On our side: `.venv/bin/python scripts/manage_sheets.py list` shows the sheet once it
has called `/trigger` (local mode doesn't register it).

## Test locally with ngrok (before anything is deployed)

```bash
# terminal 1: the API, starting the worker on this machine instead of Cloud Run
TRIGGER_RUN_LOCAL=1 TRIGGER_LOCAL_ARGS="--dummy" .venv/bin/uvicorn service.api:app --port 8080
# terminal 2: a public address for it
ngrok http 8080
```

1. Put `https://<id>.ngrok-free.app/trigger` in `TRIGGER_URL`. On the free plan the address changes each time ngrok starts.
2. Add a row in the Requests tab and wait about 2 minutes. The worker's log appears in terminal 1.
   With the add-on, the API waits for the quiet time itself (`TRIGGER_QUIET_SECONDS`).
3. `TRIGGER_LOCAL_ARGS="--dummy"` writes made-up offers and doesn't touch a portal. Drop it for a real search, which uses the `.env` settings and is a live portal search, so keep these few.
4. The local worker searches the sheet the script sent (`--sheet <its id>`). The gcloud login needs the Sheets scope for it.

Stop ngrok when you're done: while it runs, anyone with the address and the secret
(or a Skylink token for the add-on) can start a run on this machine.

## Notes

- Status is never written by the script. The worker searches rows whose Status is empty or `NEW`, so setting a row back to `NEW` searches it again.
- Rows written through the Sheets API by another program don't fire `onEdit`; the sweep finds them.
- One search runs at a time across all sheets (`WORKER_LOCK`). Rows from a second sheet wait for the running search, which reads the sheets again before it ends.
- Per-sheet script: Apps Script time triggers fire about on time but not to the second. The search starts roughly 1.5–3 minutes after the last edit.
