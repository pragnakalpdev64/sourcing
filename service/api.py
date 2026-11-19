"""Cloud Run service `sourcing-api`.

  POST /trigger              the Sheets add-on (apps_script/, the user's Google
                              token), an older per-sheet script or the Cloud
                              Scheduler sweep (X-Trigger-Secret) -> register the caller's sheet (if
                              it sent one) -> start the worker job (or have the
                              running one read the sheet again)
  GET  /hitl/code?t=...      2FA code form (link sent in Chat, auth/hitl.py)
  POST /hitl/code            the person's code -> store -> the waiting worker
  POST /chat/events          Chat app events: the code card's "Send code"
  GET  /hitl/view            Step 5 (2FA handoff Live View), not built
  GET  /hitl/done            Step 5, not built
  /credentials, /credentials/update   Step 6: vendor login update page, behind
                              Google sign-in (/auth/*): credentials_ui.py, web_auth.py

Sheets join through scripts/manage_sheets.py add (or a /trigger call with a
sheet_id) — no redeploy.
"""
from __future__ import annotations

import hmac
import html
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from sourcing.config import get_settings, quiet_http_logs
from sourcing.store import (RERUN_FLAG, RERUN_HOLDER, RERUN_SECONDS, TRIGGER_LOCK,
                            WORKER_LOCK, make_store)

logger = logging.getLogger("sourcing.api")
logging.basicConfig(level=logging.INFO)
quiet_http_logs()

app = FastAPI(title="sourcing-api")


# The pages use only inline CSS and script from this service; nothing outside
# may load into them or frame them (the login form takes vendor passwords).
@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


def _add_pages() -> None:
    from . import credentials_ui, web_auth

    app.include_router(web_auth.router)
    app.include_router(credentials_ui.router)

TRIGGER_DEBOUNCE_SECONDS = 60

_store = None


def store():
    global _store
    if _store is None:
        s = get_settings()
        _store = make_store(s.store_backend, s.store_project, s.store_file)
    return _store


def _start_job() -> str:
    from google.cloud import run_v2

    s = get_settings()
    name = f"projects/{s.gcp_project}/locations/{s.region}/jobs/{s.job_name}"
    operation = run_v2.JobsClient().run_job(name=name)  # don't wait for completion
    return operation.metadata.name if operation.metadata else name


# Local mode (TRIGGER_RUN_LOCAL=1): the worker is a child process of this API.
# The file store keeps no locks between processes, so this process tracks its
# own child and runs it once more when rows came in meanwhile.
_local = {"proc": None, "again": False, "sheet_id": None, "start_at": 0.0, "waiting": False}
_local_lock = threading.Lock()


def _start_local_when_quiet() -> None:
    """Local mode's wait for a quiet sheet (on Cloud Run the worker waits: job_main)."""
    while True:
        with _local_lock:
            left = _local["start_at"] - time.time()
            if left <= 0:
                _local["waiting"] = False
                proc = _local["proc"]
                if proc is not None and proc.poll() is None:
                    _local["again"] = True   # "Search now" started one meanwhile
                else:
                    _start_local_job(_local["sheet_id"])
                return
        time.sleep(min(left, 1.0))


def _start_local_job(sheet_id: str | None) -> str:
    s = get_settings()
    cmd = [sys.executable, "-m", "service.job_main", *shlex.split(s.trigger_local_args)]
    if sheet_id:
        cmd += ["--sheet", sheet_id]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(cmd, cwd=root)  # its log goes to this terminal
    _local.update(proc=proc, again=False, sheet_id=sheet_id)
    threading.Thread(target=_after_local_job, args=(proc,), daemon=True).start()
    logger.info("Started local worker (pid %d): %s", proc.pid, " ".join(cmd))
    return f"local-{proc.pid}"


def _after_local_job(proc) -> None:
    code = proc.wait()
    with _local_lock:
        if _local["proc"] is not proc:
            return
        logger.info("Local worker (pid %d) ended with %s", proc.pid, code)
        _local["proc"] = None
        if _local["again"]:
            logger.info("Rows were added during the run: starting the worker again")
            _start_local_job(_local["sheet_id"])


def _trigger_local(sheet_id: str | None, registered: bool, wait: int = 0) -> dict:
    with _local_lock:
        proc = _local["proc"]
        if proc is not None and proc.poll() is None:
            _local["again"] = True
            return {"started": False, "registered": registered, "rerun": True,
                    "reason": "worker already running; it runs again when done"}
        if wait > 0:
            # Each edit moves the start back: it starts `wait` s after the last one.
            _local.update(start_at=time.time() + wait, sheet_id=sheet_id)
            if not _local["waiting"]:
                _local["waiting"] = True
                threading.Thread(target=_start_local_when_quiet, daemon=True).start()
            return {"started": False, "registered": registered, "waiting": True,
                    "reason": f"starts {wait}s after the last edit"}
        return {"started": True, "registered": registered,
                "execution": _start_local_job(sheet_id)}


# -- who may call /trigger -----------------------------------------------------
# The Scheduler sweep and a per-sheet script send TRIGGER_SECRET. The add-on
# sends the user's Google access token (ScriptApp.getOAuthToken), so it holds no
# secret: Google tells us which OAuth client the token is for and whose it is.
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


async def _token_info(token: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(TOKENINFO_URL, params={"access_token": token})
    return resp.json() if resp.status_code == 200 else None


def _addon_client(client_id: str, s) -> bool:
    """One of the listed clients, or any client of a listed project: Google OAuth
    client ids are "<project number>-<id>.apps.googleusercontent.com"."""
    if client_id in s.addon_oauth_client_ids:
        return True
    number, dash, rest = client_id.partition("-")
    return (bool(dash) and number.isdigit() and rest.endswith(".apps.googleusercontent.com")
            and number in s.addon_oauth_project_numbers)


def _addon_refusal(info: dict | None) -> str | None:
    """Why this token may not start a run, or None if it may."""
    s = get_settings()
    if info is None:
        return "Google did not accept the token"
    client_id = str(info.get("azp") or info.get("aud") or "").lower()
    if not s.addon_oauth_client_ids and not s.addon_oauth_project_numbers:
        return "add-on sign-in not set up (ADDON_OAUTH_CLIENT_IDS / ADDON_OAUTH_PROJECT_NUMBERS)"
    if not _addon_client(client_id, s):
        # Logged so the right id can be set after the first test call.
        logger.warning("/trigger: token for OAuth client %s, not the add-on's", client_id)
        # The id goes in the answer too (it's not a secret): the add-on shows it, so a
        # changed Apps Script project / Cloud project link is seen at once (2026-11-18).
        return f"token is not from the SourcingAI add-on (OAuth client {client_id or 'none'})"
    email = str(info.get("email") or "").lower()
    if not email or str(info.get("email_verified")).lower() != "true":
        return "Google did not confirm the email address"
    domains = s.allowed_email_domains
    if "*" in domains and not os.environ.get("K_SERVICE"):
        return None   # local tests only, as on the sign-in page (web_auth.email_allowed)
    # Only accounts of the listing's Workspace can authorize an Internal app;
    # the domain is checked too, in case the consent screen is ever made External.
    if email.rpartition("@")[2] not in domains:
        return f"{email} is not a Skylink Google account"
    return None


async def _iap_email(request: Request) -> str | None:
    """The email IAP signed for this request; None when no IAP is set up
    (IAP_AUDIENCE empty: local runs). Behind IAP, a request without its valid
    signed header is refused, whatever else it carries."""
    import asyncio

    from service.iap import IapRefused, verified_email

    audience = get_settings().iap_audience
    if not audience:
        return None
    try:
        return await asyncio.to_thread(verified_email, request.headers, audience)
    except IapRefused as exc:
        logger.warning("%s refused: %s", request.url.path, exc)
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


async def _caller(request: Request, x_trigger_secret: str) -> str:
    """"secret" or the add-on user's email; anything else is refused.
    Behind IAP the request must also carry IAP's signed header, and an add-on
    call must be signed in at IAP as the same person as its token."""
    iap_email = await _iap_email(request)
    secret = get_settings().trigger_secret

    def refuse(reason: str) -> HTTPException:
        # Logged: the add-on shows only what it is told (a sidebar 401, 2026-11-18,
        # left no trace of why on either side).
        logger.warning("%s refused: %s", request.url.path, reason)
        return HTTPException(status_code=401, detail=reason)

    if x_trigger_secret:
        if secret and hmac.compare_digest(x_trigger_secret, secret):
            return "secret"
        raise refuse("bad trigger secret")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:].strip():
        info = await _token_info(auth[7:].strip())
        refusal = _addon_refusal(info)
        if refusal:
            raise refuse(refusal)
        email = str(info["email"]).lower()
        if iap_email and iap_email != email:
            logger.warning("%s: IAP signed in %s, add-on token is %s", request.url.path,
                           iap_email, email)
            raise refuse("IAP and add-on sign-in differ")
        return email
    raise refuse("no add-on token and no trigger secret")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _relay_store():
    if get_settings().sheet_access != "relay":
        raise HTTPException(status_code=400, detail="SHEET_ACCESS is not relay")
    if get_settings().store_backend == "file":
        # The API and the worker are separate processes; a file store can't be shared.
        raise HTTPException(status_code=400, detail="the sheet relay needs STORE_BACKEND=firestore")
    return store()


def _save_snapshot(sheet_id: str, snapshot) -> None:
    ok = isinstance(snapshot, dict) and snapshot and all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(r, list) for r in v)
        for k, v in snapshot.items())
    if not ok:
        raise HTTPException(status_code=400, detail="snapshot: {tab: [[cell, ...], ...]}")
    try:
        _relay_store().put_sheet_snapshot(sheet_id, snapshot)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    logger.info("Sheet %s: copy received (%s)", sheet_id,
                ", ".join(f"{k} {len(v)} rows" for k, v in snapshot.items()))


async def _sheet_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not isinstance(body.get("sheet_id"), str) or not body["sheet_id"]:
        raise HTTPException(status_code=400, detail="sheet_id missing")
    return body


# Only one add-on run writes a sheet's waiting results at a time: the sidebar, the
# edit trigger and the hourly timer may all pull at once, and two writers would
# append the same RFQ lines twice. Apps Script's document lock can't cover the
# timer (it has no open document), so the lease is kept here. Held while results
# are being written; free again once a pull finds nothing, or after the TTL if
# that add-on run died mid-write (its results then come again).
PULL_LEASE_SECONDS = 120


def _pull_lease_name(sheet_id: str) -> str:
    return f"sheet_pull:{sheet_id}"


@app.post("/sheet/pull")
async def sheet_pull(request: Request, x_trigger_secret: str = Header(default="")) -> dict:
    """The add-on collects the writes waiting for its sheet (oldest first), writes
    them into the sheet, then confirms them with /sheet/ack. `busy`: a search is
    running or about to start, so more may come; the add-on keeps checking.
    `progress`: the worker's step and heartbeat (sourcing/progress.py).
    `lease`: send it back with the next pull and the ack; `writing`: another
    add-on run is writing this sheet's results right now (no ops given)."""
    from sourcing.progress import describe

    await _caller(request, x_trigger_secret)
    body = await _sheet_body(request)
    sheet_id = body["sheet_id"]
    st = _relay_store()
    with _local_lock:
        proc = _local["proc"]
        local_busy = _local["waiting"] or (proc is not None and proc.poll() is None)
    busy = bool(local_busy or st.is_locked(WORKER_LOCK) or st.is_locked(TRIGGER_LOCK))
    progress = describe(st.get_sheet_progress(sheet_id), time.time())
    if busy and (progress is None or progress["state"] in ("done", "failed")):
        # Started but not reading the sheet yet (waiting for a quiet sheet, job
        # starting); an earlier run's "done" is not this one's.
        # Nothing of the earlier run: 2026-11-18 the sidebar said "Starting for 13 min,
        # 51 of 51 row(s) finished", the previous run's time and rows.
        progress = {"state": "starting",
                    "steps": ["Starting (waits until the sheet is quiet for a moment)"],
                    "rows_done": 0, "rows_total": 0, "counts": {}, "elapsed_seconds": None,
                    "alive_seconds_ago": None, "finished_seconds_ago": None}
    reply = {"ops": [], "busy": busy, "progress": progress, "writing": False}

    lease = body.get("lease") if isinstance(body.get("lease"), str) else ""
    lease = lease or uuid.uuid4().hex
    name = _pull_lease_name(sheet_id)
    if not st.acquire_lock(name, lease, PULL_LEASE_SECONDS):
        return {**reply, "writing": True}
    ops = st.sheet_ops(sheet_id)[:200]      # one pull stays well inside Apps Script's limits
    if not ops:
        st.release_lock(name, lease)
        return reply
    return {**reply, "ops": [{"id": o["id"], **o["op"]} for o in ops], "lease": lease}


@app.post("/sheet/ack")
async def sheet_ack(request: Request, x_trigger_secret: str = Header(default="")) -> dict:
    await _caller(request, x_trigger_secret)
    body = await _sheet_body(request)
    ids = [i for i in body.get("ids") or [] if isinstance(i, str)]
    if ids:
        _relay_store().ack_sheet_ops(body["sheet_id"], ids)
    return {"acked": len(ids)}


@app.post("/trigger")
async def trigger(request: Request, x_trigger_secret: str = Header(default="")) -> dict:
    caller = await _caller(request, x_trigger_secret)

    # The Scheduler sweep sends no body; any other caller may send {sheet_id, label}.
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    sheet_id = body.get("sheet_id")
    # The add-on calls on every edit ("edited": true) and leaves the wait to us:
    # add-ons may not run a timer more than once an hour. "edited": false is its
    # "Search now" (no wait); the older per-sheet script sends neither.
    edited = body.get("edited")
    quiet = get_settings().trigger_quiet_seconds
    # The add-on sends a copy of the sheet: the worker may not open it (sheet_relay).
    if sheet_id and body.get("snapshot") is not None:
        _save_snapshot(sheet_id, body["snapshot"])

    if get_settings().trigger_run_local:
        # Not registered: the local worker searches only this sheet (--sheet), and
        # this process's copy of the file store would overwrite the worker's saves.
        return _trigger_local(sheet_id, False, quiet if edited is True else 0)

    st = store()
    registered = False
    if sheet_id:
        st.register_sheet(sheet_id, {
            "label": body.get("label"),
            "requests_tab": body.get("requests_tab"),
            "results_tab": body.get("results_tab"),
            # The worker waits until every sheet's last edit is `quiet` s old
            # (job_main.wait_until_quiet); 0 = don't wait for this sheet.
            "last_edit_at": None if edited is None else (time.time() if edited else 0),
            "last_called_by": None if caller == "secret" else caller,
        })
        registered = True
        logger.info("Registered sheet %s (%s)", sheet_id, caller)

    if st.is_locked(WORKER_LOCK):
        # The running worker read its rows (and sheet list) when it started: it
        # reads the sheets once more before it ends (job_main, RERUN_FLAG).
        st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, RERUN_SECONDS)
        return {"started": False, "registered": registered, "rerun": True,
                "reason": "worker already running"}
    holder = uuid.uuid4().hex
    if not st.acquire_lock(TRIGGER_LOCK, holder, TRIGGER_DEBOUNCE_SECONDS):
        # A job started seconds ago may already be past reading the sheet.
        st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, RERUN_SECONDS)
        return {"started": False, "registered": registered, "rerun": True,
                "reason": "debounced (a run was just started)"}

    try:
        execution = _start_job()
    except Exception as exc:
        # Seen 2026-11-12: a local API without TRIGGER_RUN_LOCAL asked Cloud Run for the
        # job, which isn't deployed (403 run.jobs.run): a clear answer, not a 500.
        st.release_lock(TRIGGER_LOCK, holder)
        logger.error("Could not start the worker job %s: %s", get_settings().job_name,
                     str(exc).splitlines()[0][:300])
        raise HTTPException(status_code=503, detail=(
            f"Could not start the Cloud Run job '{get_settings().job_name}' "
            f"({type(exc).__name__}). Not deployed yet? For a local test set "
            "TRIGGER_RUN_LOCAL=1")) from exc
    logger.info("Started worker execution %s", execution)
    return {"started": True, "registered": registered, "execution": execution}


# -- 2FA code handoff (sourcing/auth/hitl.py) --------------------------------
# The link's token is the only key: whoever has the Chat message can send the
# code. A Google sign-in check (the viewer is the requester) comes with step 5.

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>SourcingAI login code</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}}
input{{font-size:1.4rem;padding:.4rem;width:12rem;letter-spacing:.15rem}}
button{{font-size:1.1rem;padding:.5rem 1rem}}</style></head>
<body><h2>SourcingAI</h2>{body}</body></html>"""


def _page(body: str, *, refresh: bool = False, status: int = 200) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return HTMLResponse(_PAGE.format(refresh=meta, body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/hitl/code")
def hitl_code_form(t: str = "") -> HTMLResponse:
    from sourcing.auth.hitl import WAITING, hitl_id, status_text

    doc = store().get_hitl(hitl_id(t)) if t else None
    if not doc:
        return _page("<p>This link is not valid.</p>", status=404)
    if doc.get("state") != WAITING:
        busy = doc.get("state") in ("code_given", "code_used")
        return _page(f"<p>{html.escape(status_text(doc))}</p>", refresh=busy)
    vendor = html.escape(doc.get("vendor_name") or "The portal")
    method = html.escape(doc.get("method") or "")
    return _page(
        f"<p><b>{vendor}</b> is asking for a login code (sent by {method}).</p>"
        f'<form method="post" action="/hitl/code"><input type="hidden" name="t" '
        f'value="{html.escape(t)}"><p><input name="code" autocomplete="one-time-code" '
        f'inputmode="text" autofocus required> <button type="submit">Send</button></p></form>')


@app.post("/hitl/code")
async def hitl_code_submit(request: Request) -> HTMLResponse:
    from sourcing.auth.hitl import accept_code

    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    token = (form.get("t") or [""])[0]
    ok, message = accept_code(store(), token, (form.get("code") or [""])[0], via="form")
    return _page(f"<p>{html.escape(message)}</p>", refresh=ok, status=200 if ok else 400)


CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS = ("https://www.googleapis.com/service_accounts/v1/metadata/x509/"
              "chat@system.gserviceaccount.com")


def _check_chat_caller(authorization: str) -> None:
    """Chat signs each event for the app's audience (CHAT_APP_AUDIENCE). Without
    one set, events are taken only off Cloud Run (local tests with fake events)."""
    audience = get_settings().chat_app_audience
    if not audience:
        if os.environ.get("K_SERVICE"):
            raise HTTPException(status_code=403, detail="Chat app not set up")
        return
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = id_token.verify_token(token, google_requests.Request(), audience=audience,
                                       certs_url=CHAT_CERTS)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="bad Chat token") from exc
    if claims.get("iss") != CHAT_ISSUER:
        raise HTTPException(status_code=401, detail="not from Chat")


@app.post("/chat/events")
async def chat_events(request: Request, authorization: str = Header(default="")) -> dict:
    from sourcing.auth.hitl import accept_code

    _check_chat_caller(authorization)
    event = await request.json()
    action = event.get("action") or {}
    if event.get("type") == "CARD_CLICKED" and action.get("actionMethodName") == "submit_code":
        params = {p.get("key"): p.get("value") for p in action.get("parameters") or []}
        inputs = ((event.get("common") or {}).get("formInputs") or {}).get("code") or {}
        values = (inputs.get("stringInputs") or {}).get("value") or [""]
        _, message = accept_code(store(), params.get("t", ""), values[0], via="chat card")
        return {"text": message}
    return {"text": "SourcingAI posts login code requests here."}


def _not_built(step: str) -> None:
    raise HTTPException(status_code=501, detail=f"Not built yet (plan {step})")


@app.get("/hitl/view")
def hitl_view() -> None:
    _not_built("Step 5")


@app.get("/hitl/done")
def hitl_done() -> None:
    _not_built("Step 5")


_add_pages()
