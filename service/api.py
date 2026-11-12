"""Cloud Run service `sourcing-api`.

  POST /trigger              the sheet's Apps Script (apps_script/) or the Cloud
                              Scheduler sweep -> register the caller's sheet (if
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
import uuid
from urllib.parse import parse_qs

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
        _store = make_store(s.store_backend, s.gcp_project, s.store_file)
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
_local = {"proc": None, "again": False, "sheet_id": None}
_local_lock = threading.Lock()


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


def _trigger_local(sheet_id: str | None, registered: bool) -> dict:
    with _local_lock:
        proc = _local["proc"]
        if proc is not None and proc.poll() is None:
            _local["again"] = True
            return {"started": False, "registered": registered, "rerun": True,
                    "reason": "worker already running; it runs again when done"}
        return {"started": True, "registered": registered,
                "execution": _start_local_job(sheet_id)}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/trigger")
async def trigger(request: Request, x_trigger_secret: str = Header(default="")) -> dict:
    secret = get_settings().trigger_secret
    if not secret or not hmac.compare_digest(x_trigger_secret, secret):
        raise HTTPException(status_code=401, detail="bad trigger secret")

    # The Scheduler sweep sends no body; any other caller may send {sheet_id, label}.
    try:
        body = await request.json()
    except Exception:
        body = {}
    sheet_id = (body or {}).get("sheet_id")

    if get_settings().trigger_run_local:
        # Not registered: the local worker searches only this sheet (--sheet), and
        # this process's copy of the file store would overwrite the worker's saves.
        return _trigger_local(sheet_id, False)

    st = store()
    registered = False
    if sheet_id:
        st.register_sheet(sheet_id, {
            "label": body.get("label"),
            "requests_tab": body.get("requests_tab"),
            "results_tab": body.get("results_tab"),
        })
        registered = True
        logger.info("Registered sheet %s", sheet_id)

    if st.is_locked(WORKER_LOCK):
        # The running worker read its rows (and sheet list) when it started: it
        # reads the sheets once more before it ends (job_main, RERUN_FLAG).
        st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, RERUN_SECONDS)
        return {"started": False, "registered": registered, "rerun": True,
                "reason": "worker already running"}
    if not st.acquire_lock(TRIGGER_LOCK, uuid.uuid4().hex, TRIGGER_DEBOUNCE_SECONDS):
        # A job started seconds ago may already be past reading the sheet.
        st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, RERUN_SECONDS)
        return {"started": False, "registered": registered, "rerun": True,
                "reason": "debounced (a run was just started)"}

    execution = _start_job()
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
