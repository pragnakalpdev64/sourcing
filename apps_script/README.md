# Sheet trigger (Apps Script)

A row added to a request sheet's Requests tab starts a search for that sheet. The
offers go back into the same sheet, so each person can work in their own sheet.
Requested By is filled in with the editor's email when it's empty. The Cloud
Scheduler sweep stays as the backstop for rows that were missed.

It is put in a sheet through the Sheets add-on (`addon/`): the admin installs it once for
Skylink, and each person turns it on in their own sheet. It proves each call with the user's
Google token (Skylink account, this add-on); the 90 s quiet wait is in the service, since
add-ons may run a timer only once an hour. Its Marketplace listing is in its own small GCP
project (no secrets, no billing).

(A script pasted into each sheet, signing its calls with `TRIGGER_SECRET`, was retired
2026-11-20: IAP refuses its calls. A sheet that still has it: delete its triggers in
Extensions > Apps Script > Triggers, then Extensions > SourcingAI > Turn on for this sheet.)

A search already running when the call comes reads the sheet once more
before it ends (`RERUN_FLAG`, `service/job_main.py`, at most 5 passes).

**Add-on: the service never opens the sheet** (client, 2026-11-13: Skylink's org
blocks the service account). With each call the add-on sends a copy of the Requests
tab; the search's writes (Status, Notes, Run ID, RFQ lines) wait in the store until
the add-on writes them into the sheet (`/sheet/pull`, `/sheet/ack`;
`sourcing/sheet_relay.py`, service setting `SHEET_ACCESS=relay`, needs Firestore).
Apps Script stops any one run at 6 minutes and a search can take longer (AAR, 11
parts: 15 min, 2026-11-17), so the results are written by whichever comes first:
- **Show progress** (sidebar; **Search new rows now** opens it): checks every 15 s
  while open and shows the worker's step, rows finished and last heartbeat
  (`sourcing/progress.py`); "Not responding" when the heartbeat is over 150 s old;
- an edit (keeps checking for about 5 minutes), opening the sheet, **Get results now**;
- an **hourly timer**, set up by Turn on (or the next time the sheet is opened): results
  of a search that ends while the sheet is closed are in the sheet within the hour.

Only one of them writes at a time (a lease in `/sheet/pull`), so RFQ lines are not
doubled. The timer opens the sheet by id, so the add-on's scope is `spreadsheets`
(was `spreadsheets.currentonly` until 2026-11-18): **the admin approves the new
scope once** after the update.

## Add-on (`addon/`)

### Once, by the developer

1. In the add-on's GCP project (the listing project, not `skylink-material-planning`:
   that one already has the MIE Assistant listing):
   - OAuth consent screen: **Internal**.
   - Enable **Google Workspace Marketplace SDK**.
   - Note the **project number**.
2. At script.google.com, a **standalone** project (New project, not from a sheet),
   owned by a Skylink account:
   - Paste `addon/Code.gs`, and add an HTML file named `Sidebar` with `addon/Sidebar.html`.
     In Project Settings tick "Show appsscript.json" and paste `addon/appsscript.json`.
   - Project Settings > Google Cloud Platform project > **Change project** > the project number.
   - Project Settings > Script properties: `TRIGGER_URL` (`https://<host>/trigger`),
     optional `REQUESTS_TAB`.
3. The service's settings (`.env` locally, env vars on Cloud Run):
   - `ADDON_OAUTH_CLIENT_IDS`: the OAuth client id Apps Script made in the listing
     project (Google Cloud console > APIs & Services > Credentials, the one named
     "Apps Script"). Not sure which one: make a test call; the API logs
     "token for OAuth client <id>, not the add-on's", and that id is the one.
     Better: `ADDON_OAUTH_PROJECT_NUMBERS` = the listing project's number. Apps Script
     makes a new client now and then (a third one after the scope change of
     2026-11-18), and any client of that project is then accepted.
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

### Behind Google sign-in (IAP, sourcing-ai-dev)

The client keeps IAP on. The add-on gets through it with the user's Google ID token
(`ScriptApp.getIdentityToken()`, scope `openid`) in `Proxy-Authorization`. IAP checks
that token and passes `Authorization` (the access token our own check uses) on
unchanged. The service then takes the email only from IAP's signed header
(`X-Goog-IAP-JWT-Assertion`, `service/iap.py`) and refuses the call if it isn't
the same person as the add-on's token. Set up once in `skylink-material-planning`:

- service setting `IAP_AUDIENCE`: `/projects/490776059622/locations/us-central1/services/<service>`
  (IAP on Cloud Run) or `/projects/490776059622/global/backendServices/<id>` (load balancer);
- IAP's **programmatic access** list: the add-on's OAuth client id (the one in
  `ADDON_OAUTH_CLIENT_IDS`), since the ID token is made for that client, not IAP's own;
- every user (or a Skylink group): role **IAP-secured Web App User** on the service;
- the Scheduler sweep: an OIDC token from its service account (and that account given
  the same role), plus `X-Trigger-Secret` as now.

Refused at IAP: the add-on shows "Google sign-in (IAP) did not let the call through".

A new version of `Code.gs`: paste it, make a new deployment, and point the Marketplace
SDK at it. People don't install anything again.

### Each person, in their own sheet

1. Extensions > SourcingAI > **Turn on for this sheet**. It checks the Requests tab
   has a Part Number column and installs the edit and open triggers. They run as this person.
2. Add rows. The search starts about 90 seconds after the last edit; statuses and
   offers appear as they come. **Search new rows now** starts at once, **Get results
   now** writes whatever is waiting.

## Check that a sheet is set up

In the add-on's script editor (test deployment):

- **Triggers** (clock icon): one `onRequestsEdit`, "From spreadsheet – On edit".
- **Executions**, after adding a row: `onRequestsEdit` with the log line
  "SourcingAI /trigger: ...".
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
   Live, through the add-on and the relay (Browserbase keys from Secret Manager):
   ```bash
   TRIGGER_RUN_LOCAL=1 TRIGGER_LOCAL_ARGS="--browser browserbase" SHEET_ACCESS=relay \
   STORE_BACKEND=firestore STORE_PROJECT=sourcing-skylink-addon INFRA_SECRETS=gsm \
   .venv/bin/uvicorn service.api:app --port 8080 2>&1 | tee api_live_$(date +%m%d_%H%M).log
   ```
4. The local worker searches the sheet the script sent (`--sheet <its id>`). The gcloud login needs the Sheets scope for it.

Stop ngrok when you're done: while it runs, anyone with the address and the secret
(or a Skylink token for the add-on) can start a run on this machine.

## Notes

- Status is never written by the script. The worker searches rows whose Status is empty or `NEW`, so setting a row back to `NEW` searches it again.
- Rows written through the Sheets API by another program don't fire `onEdit`; the sweep finds them.
- One search runs at a time across all sheets (`WORKER_LOCK`). Rows from a second sheet wait for the running search, which reads the sheets again before it ends.
- Per-sheet script: Apps Script time triggers fire about on time but not to the second. The search starts roughly 1.5–3 minutes after the last edit.
