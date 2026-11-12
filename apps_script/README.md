# Sheet trigger (Apps Script)

`Code.gs` goes into the request sheet. A row added to the Requests tab starts a
search: once nobody has edited the tab for 90 seconds, the script calls
SourcingAI's `/trigger` once. Requested By is filled in with the editor's email
when it's empty. The Cloud Scheduler sweep stays as the backstop for rows the
script misses.

If a search is already running when the call comes, the running worker reads the
sheet once more before it ends (`RERUN_FLAG`, `service/job_main.py`, at most 5 passes).

## Install in the sheet (once per sheet)

1. Open the sheet, then **Extensions > Apps Script**.
2. Replace `Code.gs` with this folder's `Code.gs`.
3. In **Project Settings**, tick "Show appsscript.json". Replace that file with this folder's `appsscript.json`.
4. Still in **Project Settings > Script properties**, add:
   - `TRIGGER_URL`: `https://<host>/trigger` (the Cloud Run URL, or the ngrok URL while testing)
   - `TRIGGER_SECRET`: the service's `TRIGGER_SECRET`
   - optional `REQUESTS_TAB` (default `Requests`) and `DELAY_SECONDS` (default `90`)
5. In the editor, pick `setup` and press **Run**. Allow the permissions it asks for.
   The trigger runs as the person who ran `setup`.
6. Reload the sheet. A **SourcingAI > Search new rows now** menu appears. It calls `/trigger` without the wait.

The script's log is under **Executions** in the Apps Script editor.

Anyone who can edit the script can read `TRIGGER_SECRET`. All it does is start a search run.

## Test locally with ngrok (before anything is deployed)

```bash
# terminal 1: the API, starting the worker on this machine instead of Cloud Run
TRIGGER_RUN_LOCAL=1 TRIGGER_LOCAL_ARGS="--dummy" .venv/bin/uvicorn service.api:app --port 8080
# terminal 2: a public address for it
ngrok http 8080
```

1. Put `https://<id>.ngrok-free.app/trigger` in `TRIGGER_URL`. On the free plan the address changes each time ngrok starts.
2. Add a row in the Requests tab and wait about 2 minutes. The worker's log appears in terminal 1.
3. `TRIGGER_LOCAL_ARGS="--dummy"` writes made-up offers and doesn't touch a portal. Drop it for a real search, which uses the `.env` settings and is a live portal search, so keep these few.
4. The local worker searches the sheet the script sent (`--sheet <its id>`). The gcloud login needs the Sheets scope for it.

Stop ngrok when you're done: while it runs, anyone with the address and the secret can start a run on this machine.

## Notes

- Apps Script time triggers fire about on time but not to the second. The search starts roughly 1.5–3 minutes after the last edit.
- Status is never written by the script. The worker searches rows whose Status is empty or `NEW`, so setting a row back to `NEW` searches it again.
- Rows written through the Sheets API by another program don't fire `onEdit`; the sweep finds them.
