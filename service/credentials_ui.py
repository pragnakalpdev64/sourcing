"""Login update page (plan step 6, docs/CREDENTIAL_UPDATE_UI_PLAN.md).

  GET  /credentials                       vendors the person may change a login for
  GET  /credentials?vendor=X              that vendor's logins they may change
  GET  /credentials/update?vendor=&owner= the form
  POST /credentials/update                new version of the login's secret
  GET/POST /credentials/new               admins: add a login (creates its secret)
  GET/POST /credentials/editors?vendor=&owner=_shared   admins: who may change a shared login
  GET/POST /credentials/find              list a login whose secret was made elsewhere (gcloud)

Google sign-in (web_auth.py) first. Who may change which login:
sourcing/credentials/access.py. Changing a password only adds a version to an
existing secret, with our service account. Adding a login creates the secret
with the admin's own Google access (web_auth.gcp_credentials), so only people
Google lets create secrets in the project can do it, even if this page let
someone through by mistake. The password is never shown, logged or stored
outside Secret Manager.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response

from sourcing.config import get_settings
from sourcing.credentials import gsm
from sourcing.credentials.access import (Login, can_edit, editable_logins, is_admin,
                                         login_from_record, vendor_name)
from sourcing.credentials.base import CredentialError, CredentialNotFound, secret_key
from sourcing.models import SHARED, AccountState, account_id, slugify
from sourcing.vendors import ADAPTERS, canonical_vendor_id

from .web_auth import (check_csrf, csrf_token, current_user, gcp_credentials, login_redirect,
                       page)

logger = logging.getLogger("sourcing.credentials_ui")

router = APIRouter()

MAX_FIELD = 500

_STATE_TEXT = {
    AccountState.ACTIVE.value: "working",
    AccountState.NEEDS_LOGIN.value: "working",
    AccountState.NEEDS_CREDENTIAL_UPDATE.value: "password rejected: please update",
    AccountState.AWAITING_HUMAN.value: "waiting for a 2FA code",
    AccountState.NEEDS_ATTENTION.value: "needs a developer",
    AccountState.SUSPENDED.value: "stopped after failed logins",
}

_STATE_TOOLTIP = {
    AccountState.ACTIVE.value: "Login is working normally.",
    AccountState.NEEDS_LOGIN.value: "Login is active and will authenticate on next run.",
    AccountState.NEEDS_CREDENTIAL_UPDATE.value: "The worker failed to log in with this password. Please update it.",
    AccountState.AWAITING_HUMAN.value: "Waiting for a two-factor authentication (2FA) verification code.",
    AccountState.NEEDS_ATTENTION.value: "Encountered an error that needs developer attention.",
    AccountState.SUSPENDED.value: "Account suspended after repeated failed logins.",
}

# Badge colour per state (CSS classes in web_auth._PAGE); others are grey.
_STATE_BADGE = {
    AccountState.ACTIVE.value: "badge-success",
    AccountState.NEEDS_LOGIN.value: "badge-success",
    AccountState.NEEDS_CREDENTIAL_UPDATE.value: "badge-danger",
    AccountState.SUSPENDED.value: "badge-danger",
    AccountState.AWAITING_HUMAN.value: "badge-warning",
    AccountState.NEEDS_ATTENTION.value: "badge-danger",
}

_creds = None
_secret_client = None   # tests put a fake Secret Manager client here
EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[a-z0-9.-]+\.[a-z]{2,}$")


def store():
    from . import api   # the API's store, so tests can swap one in

    return api.store()


def creds():
    global _creds
    if _creds is None:
        from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides

        s = get_settings()
        _creds = SecretManagerProvider(s.gcp_project, parse_overrides(s.vendor_secret_overrides))
    return _creds


def _e(value) -> str:
    return html.escape(str(value or ""))


def _who(login: Login, email: str) -> str:
    if login.is_shared:
        return "Shared team login"
    return "Your own login" if login.owner == email else f"{login.owner}'s login"


def _when(ts: Optional[float]) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _find(user: dict, vendor_id: str, owner: str) -> Optional[Login]:
    owner = (owner or "").strip().lower()
    for login in editable_logins(store(), user["email"], get_settings().credential_admins):
        if login.vendor_id == vendor_id and login.owner == owner:
            return login
    return None


def _status_badge(state: Optional[str]) -> str:
    text = _STATE_TEXT.get(state, state or "not used yet")
    tooltip = _STATE_TOOLTIP.get(state, "This account has not been used yet by the sourcing worker.")
    cls = _STATE_BADGE.get(state, "badge-neutral")
    return f'<span class="badge {cls}" title="{_e(tooltip)}"><span class="badge-dot"></span>{_e(text)}</span>'


@router.get("/credentials")
def credentials_home(request: Request, vendor: str = "") -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    st = store()
    admin = is_admin(user["email"], get_settings().credential_admins)
    add_link = ('<div class="btn-group">'
                + ('<a href="/credentials/new" class="btn btn-secondary">Add a login</a>'
                   if admin else "")
                + '<a href="/credentials/find" class="btn btn-secondary">Login not listed? '
                  'Find it</a></div>')
    logins = editable_logins(st, user["email"], get_settings().credential_admins)
    if not logins:
        return page(
            '<div class="card"><p class="muted">There are no vendor logins you can change. Ask the SourcingAI admin '
            f'if you need access to one.</p>{add_link}</div>', user=user)

    if not vendor:
        vendors = sorted({l.vendor_id for l in logins}, key=lambda v: vendor_name(st, v).lower())
        options = "".join(f'<option value="{_e(v)}">{_e(vendor_name(st, v))}</option>'
                          for v in vendors)
        content = (
            '<div class="card">'
            '  <div class="card-header">'
            '    <h2 class="card-title">Select Vendor Portal</h2>'
            '    <p class="card-subtitle">Choose a vendor to view and manage its login credentials.</p>'
            '  </div>'
            '  <form method="get" action="/credentials">'
            '    <label for="vendor-select">Vendor</label>'
            f'    <select id="vendor-select" name="vendor" required>{options}</select>'
            '    <div class="btn-group">'
            '      <button type="submit" class="btn btn-primary">Next</button>'
            '    </div>'
            '  </form>'
            f'  {add_link}'
            '</div>'
        )
        return page(content, user=user)

    rows = []
    for login in (l for l in logins if l.vendor_id == vendor):
        link = "/credentials/update?" + urlencode({"vendor": login.vendor_id,
                                                   "owner": login.owner})
        if login.password_changed_at:
            changed_html = (
                f'<div class="timestamp">{_e(_when(login.password_changed_at))}</div>'
                f'<div class="author">by {_e(login.password_changed_by)}</div>'
            )
        else:
            changed_html = '<span class="muted">not through this page yet</span>'

        rows.append(
            f'<tr>'
            f'  <td><strong>{_e(_who(login, user["email"]))}</strong></td>'
            f'  <td class="col-status">{_status_badge(login.state)}</td>'
            f'  <td class="col-changed">{changed_html}</td>'
            f'  <td class="col-action"><a href="{_e(link)}" class="btn btn-primary btn-sm">Change password</a>'
            f'{_editors_button(login) if admin else ""}</td>'
            f'</tr>'
        )
    if not rows:
        return page(
            '<div class="card"><p class="err">You can\'t change any login for this vendor. '
            '<a href="/credentials" class="breadcrumb">Back</a></p></div>', user=user, status=404)
    v_name = vendor_name(st, vendor)
    content = (
        '<div class="breadcrumb"><a href="/credentials">← Other vendor</a></div>'
        '<div class="card">'
        '  <div class="card-header">'
        f'    <h2 class="card-title">{_e(v_name)}</h2>'
        f'    <p class="card-subtitle">Manage login credentials for {_e(v_name)}.</p>'
        '  </div>'
        '  <div class="table-card">'
        '    <table>'
        '      <thead>'
        '        <tr><th>Login</th><th class="col-status">Status</th><th>Password last changed</th><th class="col-action">Action</th></tr>'
        '      </thead>'
        f'      <tbody>{"".join(rows)}</tbody>'
        '    </table>'
        '  </div>'
        '</div>'
    )
    return page(content, user=user)


def _form(user: dict, login: Login, error: str = "", status: int = 200) -> Response:
    st = store()
    v_name = vendor_name(st, login.vendor_id)
    who_desc = _who(login, user["email"])
    back_url = f"/credentials?{urlencode({'vendor': login.vendor_id})}"
    err = (
        f'<div class="alert err">'
        f'  <svg class="alert-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f'    <circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>'
        f'  </svg>'
        f'  <div>{_e(error)}</div>'
        f'</div>'
    ) if error else ""
    content = (
        f'<div class="breadcrumb"><a href="{back_url}">← Back to {_e(v_name)} logins</a></div>'
        f'<div class="card">'
        f'  <div class="card-header">'
        f'    <h3 class="card-title">{_e(v_name)}: {_e(who_desc)}</h3>'
        f'    <p class="card-subtitle">Enter the updated credentials below. The password will be saved directly into Secret Manager and is never logged.</p>'
        f'  </div>'
        f'  {err}'
        f'  <form method="post" action="/credentials/update" autocomplete="off">'
        f'    <input type="hidden" name="csrf" value="{csrf_token(user)}">'
        f'    <input type="hidden" name="vendor" value="{_e(login.vendor_id)}">'
        f'    <input type="hidden" name="owner" value="{_e(login.owner)}">'
        f'    <label for="pwd1">New password <span class="input-desc">(required)</span></label>'
        f'    <div class="pw-wrapper">'
        f'      <input type="password" id="pwd1" name="password" required autocomplete="new-password" placeholder="Enter new password">'
        f'      <button type="button" class="btn-toggle-pw" data-target="pwd1" title="Show/hide password" aria-label="Show password">'
        f'        <svg class="eye-icon-open" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>'
        f'        <svg class="eye-icon-slash" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>'
        f'      </button>'
        f'    </div>'
        f'    <label for="pwd2">New password again <span class="input-desc">(confirm)</span></label>'
        f'    <div class="pw-wrapper">'
        f'      <input type="password" id="pwd2" name="password2" required autocomplete="new-password" placeholder="Re-type new password">'
        f'      <button type="button" class="btn-toggle-pw" data-target="pwd2" title="Show/hide password" aria-label="Show password">'
        f'        <svg class="eye-icon-open" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>'
        f'        <svg class="eye-icon-slash" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>'
        f'      </button>'
        f'    </div>'
        f'    <div id="pw-match-indicator" class="pw-match"></div>'
        f'    <label for="username">Username <span class="muted">(only if it changed; blank keeps it)</span></label>'
        f'    <input type="text" id="username" name="username" autocomplete="off" placeholder="Leave empty to keep existing">'
        f'    <label for="totp">2FA key <span class="muted">(only if the vendor gave a new one; blank keeps it)</span></label>'
        f'    <input type="text" id="totp" name="totp_secret" autocomplete="off" placeholder="e.g. JBSWY3DPEHPK3PXP">'
        f'    <div class="btn-group">'
        f'      <button type="submit" class="btn btn-primary">Save</button>'
        f'      <a href="{back_url}" class="btn btn-secondary">Back</a>'
        f'    </div>'
        f'  </form>'
        f'</div>'
    )
    return page(content, user=user, status=status)


@router.get("/credentials/update")
def credentials_form(request: Request, vendor: str = "", owner: str = "") -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    login = _find(user, vendor, owner)
    if not login:
        return page('<div class="card"><p class="err">You can\'t change this login. '
                    '<a href="/credentials">Back</a></p></div>', user=user, status=403)
    return _form(user, login)


def _check_fields(password: str, password2: str, username: str, totp: str) -> Optional[str]:
    if not password:
        return "Type the new password."
    if password != password2:
        return "The two passwords are not the same."
    if password != password.strip():
        # The worker trims the password it reads, so it would never be typed as given.
        return "Remove the spaces at the start or end of the password."
    if any(len(v) > MAX_FIELD for v in (password, username, totp)):
        return "A value is too long."
    if totp:
        import pyotp

        try:
            pyotp.TOTP("".join(totp.split())).now()
        except Exception:
            return "The 2FA key is not valid (it is a code of letters A-Z and digits 2-7)."
    return None


@router.post("/credentials/update")
async def credentials_save(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace"),
                                         keep_blank_values=True).items()}
    if not check_csrf(user, form.get("csrf", "")):
        return page('<div class="card"><p class="err">This form has expired. <a href="/credentials">Start '
                    "again</a>.</p></div>", user=user, status=400)
    login = _find(user, form.get("vendor", ""), form.get("owner", ""))
    # Checked again here, not only on the form: the POST can be sent without it.
    if not login or not can_edit(user["email"], login, get_settings().credential_admins):
        return page('<div class="card"><p class="err">You can\'t change this login.</p></div>', user=user, status=403)

    password, username = form.get("password", ""), form.get("username", "").strip()
    totp = form.get("totp_secret", "").strip()
    problem = _check_fields(password, form.get("password2", ""), username, totp)
    if problem:
        return _form(user, login, problem, status=400)

    now = time.time()
    fields = {"password": password, "username": username,
              "totp_secret": "".join(totp.split()).upper(),
              "updated_at": int(now), "updated_by": user["email"]}
    try:
        await creds().update(login.vendor_id, login.owner, fields, must_exist=True)
    except CredentialNotFound:
        return _form(user, login, "This login has no secret in Secret Manager yet. Ask the "
                     "SourcingAI admin to set it up.", status=409)
    except CredentialError as exc:
        logger.warning("Login update failed for %s: %s", login.account_id, exc)
        return _form(user, login, "Saving failed. Please tell the SourcingAI admin.", status=500)

    changed = sorted(k for k in ("password", "username", "totp_secret") if fields[k])
    st = store()
    account = {"vendor_id": login.vendor_id, "user_email": login.owner,
               "secret_key": login.secret_key,
               "password_changed_at": now, "password_changed_by": user["email"]}
    rejected = login.state == AccountState.NEEDS_CREDENTIAL_UPDATE.value
    if rejected:
        account.update({"state": AccountState.NEEDS_LOGIN.value, "last_error": None})
    st.update_account(login.account_id, account)
    st.audit("credential_updated", account_id=login.account_id, by=user["email"],
             fields=changed)
    logger.info("Login %s updated by %s (%s)", login.account_id, user["email"],
                ", ".join(changed))
    waiting = (" Parts that were skipped because of the old password (Status NO_CREDENTIALS) "
               "won't run by themselves: set their Status back to NEW." if rejected else "")
    v_name = vendor_name(st, login.vendor_id)
    back_url = f"/credentials?{urlencode({'vendor': login.vendor_id})}"
    content = (
        f'<div class="card">'
        f'  <div class="alert ok">'
        f'    <svg class="alert-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f'      <polyline points="20 6 9 17 4 12"></polyline>'
        f'    </svg>'
        f'    <div>'
        f'      <strong>Saved successfully!</strong><br>'
        f'      Saved. The next search uses the new password.{waiting}'
        f'    </div>'
        f'  </div>'
        f'  <div class="btn-group">'
        f'    <a href="{back_url}" class="btn btn-primary">Back to {_e(v_name)} logins</a>'
        f'    <a href="/credentials" class="btn btn-secondary">Other vendor</a>'
        f'  </div>'
        f'</div>'
    )
    return page(content, user=user)


# -- admins: add a login, choose a shared login's editors ----------------------------

def _not_admin(user: dict) -> Response:
    return page('<div class="card"><p class="err">Only the SourcingAI admins can do this. '
                '<a href="/credentials">Back</a></p></div>', user=user, status=403)


def _email_problem(email: str) -> Optional[str]:
    if not EMAIL_RE.match(email):
        return f"{email} is not an email address."
    domains = get_settings().allowed_email_domains
    if domains and "*" not in domains and email.rpartition("@")[2] not in domains:
        return f"{email} is not a Skylink account ({', '.join(domains)}), so it can't sign in here."
    return None


def _parse_emails(text: str) -> tuple[list[str], Optional[str]]:
    emails = sorted({e.strip().lower() for e in re.split(r"[\s,;]+", text or "") if e.strip()})
    for email in emails:
        problem = _email_problem(email)
        if problem:
            return [], problem
    return emails, None


def _editors_button(login: Login) -> str:
    if not login.is_shared:
        return ""     # a personal login is only its owner's (and the admins')
    link = "/credentials/editors?" + urlencode({"vendor": login.vendor_id, "owner": login.owner})
    return f' <a href="{_e(link)}" class="btn btn-secondary btn-sm">Who can change it</a>'


def _known_vendors(st) -> list[str]:
    ids = set(st.list_vendors()) | {cls.vendor_id for cls in ADAPTERS.values()}
    return sorted(ids, key=lambda v: vendor_name(st, v).lower())


def _alert(error: str) -> str:
    return f'<div class="alert err"><div>{_e(error)}</div></div>' if error else ""


def _new_form(user: dict, form: Optional[dict] = None, error: str = "",
              status: int = 200) -> Response:
    form = form or {}
    st = store()
    options = "".join(f'<option value="{_e(v)}">{_e(vendor_name(st, v))}</option>'
                      for v in _known_vendors(st))
    personal = form.get("kind") == "personal"
    # Passwords are never put back into the form after an error.
    content = (
        '<div class="breadcrumb"><a href="/credentials">← Logins</a></div>'
        '<div class="card">'
        '  <div class="card-header">'
        '    <h2 class="card-title">Add a vendor login</h2>'
        '    <p class="card-subtitle">Creates the login\'s secret in Secret Manager with your own '
        'Google access. The password is never shown or logged.</p>'
        '  </div>'
        f'  {_alert(error)}'
        '  <form method="post" action="/credentials/new" autocomplete="off">'
        f'    <input type="hidden" name="csrf" value="{csrf_token(user)}">'
        '    <label for="vendor">Vendor <span class="input-desc">(pick one, or type a new '
        'vendor name)</span></label>'
        f'    <input type="text" id="vendor" name="vendor" list="vendors" required '
        f'value="{_e(form.get("vendor"))}"><datalist id="vendors">{options}</datalist>'
        '    <label for="kind">Whose login</label>'
        '    <select id="kind" name="kind">'
        f'      <option value="shared"{"" if personal else " selected"}>Shared team login</option>'
        f'      <option value="personal"{" selected" if personal else ""}>One team member\'s own login</option>'
        '    </select>'
        '    <label for="owner_email">Team member\'s email <span class="input-desc">(own '
        'login only)</span></label>'
        f'    <input type="text" id="owner_email" name="owner_email" value="{_e(form.get("owner_email"))}">'
        '    <label for="username">Username on the vendor\'s site</label>'
        f'    <input type="text" id="username" name="username" required value="{_e(form.get("username"))}">'
        '    <label for="pwd1">Password</label>'
        '    <input type="password" id="pwd1" name="password" required autocomplete="new-password">'
        '    <label for="pwd2">Password again</label>'
        '    <input type="password" id="pwd2" name="password2" required autocomplete="new-password">'
        '    <div id="pw-match-indicator" class="pw-match"></div>'
        '    <label for="totp">2FA key <span class="input-desc">(only if the vendor uses an '
        'authenticator app)</span></label>'
        '    <input type="text" id="totp" name="totp_secret" placeholder="e.g. JBSWY3DPEHPK3PXP">'
        '    <label for="url">Login page <span class="input-desc">(optional, https)</span></label>'
        f'    <input type="text" id="url" name="url" value="{_e(form.get("url"))}">'
        '    <label for="editors">Who else can change it <span class="input-desc">(shared login '
        'only: emails, one per line; admins always can)</span></label>'
        f'    <textarea id="editors" name="editors" rows="3">{_e(form.get("editors"))}</textarea>'
        '    <div class="btn-group">'
        '      <button type="submit" class="btn btn-primary">Create login</button>'
        '      <a href="/credentials" class="btn btn-secondary">Back</a>'
        '    </div>'
        '  </form>'
        '</div>'
    )
    return page(content, user=user, status=status)


def _grant_needed(user: dict, status: int = 200) -> Response:
    content = (
        '<div class="breadcrumb"><a href="/credentials">← Logins</a></div>'
        '<div class="card">'
        '  <div class="card-header">'
        '    <h2 class="card-title">Add a vendor login</h2>'
        '    <p class="card-subtitle">A new login needs a new secret in Secret Manager. SourcingAI '
        'may not create secrets itself, so Google asks you to let it act with your Google '
        'Cloud access for the next few minutes. Only people Google allows to create secrets '
        'in the project can finish this.</p>'
        '  </div>'
        '  <div class="btn-group"><a href="/auth/grant?next=%2Fcredentials%2Fnew" '
        'class="btn btn-primary">Continue with Google</a></div>'
        '</div>'
    )
    return page(content, user=user, status=status)


def _read_form(body: bytes) -> dict:
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"),
                                         keep_blank_values=True).items()}


@router.get("/credentials/new")
def credentials_new(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    if not is_admin(user["email"], get_settings().credential_admins):
        return _not_admin(user)
    if gcp_credentials(request, user) is None:
        return _grant_needed(user)
    return _new_form(user)


def _new_login_fields(form: dict) -> tuple[Optional[dict], Optional[str]]:
    vendor_id = canonical_vendor_id(slugify(form.get("vendor", "")))
    if not vendor_id or len(vendor_id) > 40:
        return None, "Type the vendor's name."
    owner = SHARED
    if form.get("kind") == "personal":
        owner = form.get("owner_email", "").strip().lower()
        problem = _email_problem(owner) if owner else "Type the team member's email."
        if problem:
            return None, problem
    username = form.get("username", "").strip()
    if not username:
        return None, "Type the username."
    totp = form.get("totp_secret", "").strip()
    problem = _check_fields(form.get("password", ""), form.get("password2", ""), username, totp)
    if problem:
        return None, problem
    url = form.get("url", "").strip()
    if url and (not url.startswith("https://") or len(url) > MAX_FIELD):
        return None, "The login page must be an https:// address."
    editors, problem = _parse_emails(form.get("editors", ""))
    if problem:
        return None, problem
    if editors and owner != SHARED:
        return None, "Only a shared login has other editors; a team member's own login is theirs."
    return {"vendor_id": vendor_id, "owner": owner, "username": username,
            "password": form["password"], "totp_secret": "".join(totp.split()).upper(),
            "url": url, "editors": editors}, None


@router.post("/credentials/new")
async def credentials_create(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = _read_form(await request.body())
    if not check_csrf(user, form.get("csrf", "")):
        return page('<div class="card"><p class="err">This form has expired. <a href="/credentials/new">'
                    "Start again</a>.</p></div>", user=user, status=400)
    if not is_admin(user["email"], get_settings().credential_admins):
        return _not_admin(user)
    google = gcp_credentials(request, user)
    if google is None:
        # The few minutes ran out while typing; nothing was saved.
        return _grant_needed(user, status=401)
    fields, problem = _new_login_fields(form)
    if problem:
        return _new_form(user, form, problem, status=400)
    project = get_settings().gcp_project
    if not project:
        return _new_form(user, form, "Secret Manager is not set up (GCP_PROJECT).", status=503)

    vendor_id, owner = fields["vendor_id"], fields["owner"]
    key, aid = secret_key(vendor_id, owner), account_id(vendor_id, owner)
    now = time.time()
    payload = {k: fields[k] for k in ("username", "password", "totp_secret", "url") if fields[k]}
    payload.update({"updated_at": int(now), "updated_by": user["email"]})
    try:
        state = await gsm.create_login_secret(project, key, payload, credentials=google,
                                              client=_secret_client)
    except CredentialError as exc:
        logger.warning("Adding login %s failed: %s", aid, exc)
        return _new_form(user, form, str(exc), status=403)

    st = store()
    current = login_from_record(aid, st.get_account(aid) or {})
    account = {"vendor_id": vendor_id, "user_email": owner, "secret_key": key,
               "editors": sorted(set(fields["editors"]) | set(current.editors if current else ()))}
    if state != "exists":
        account.update({"password_changed_at": now, "password_changed_by": user["email"]})
    st.update_account(aid, account)
    st.audit("credential_created" if state != "exists" else "login_registered",
             account_id=aid, by=user["email"], secret=key, editors=account["editors"])
    logger.info("Login %s added by %s (secret %s)", aid, user["email"], state)

    v_name = vendor_name(st, vendor_id)
    back_url = f"/credentials?{urlencode({'vendor': vendor_id})}"
    if state == "exists":
        text = (f"{_e(key)} already exists in Secret Manager, so it was left as it is. The "
                "login is now in the list: use Change password to replace its password.")
    else:
        text = f"Created {_e(key)}. The next search for {_e(v_name)} uses this login."
    content = (
        f'<div class="card"><div class="alert ok"><div><strong>Done.</strong><br>{text}'
        '</div></div><div class="btn-group">'
        f'<a href="{_e(back_url)}" class="btn btn-primary">{_e(v_name)} logins</a>'
        '<a href="/credentials/new" class="btn btn-secondary">Add another</a></div></div>'
    )
    return page(content, user=user)


def _editors_form(user: dict, login: Login, text: Optional[str] = None, error: str = "",
                  status: int = 200) -> Response:
    v_name = vendor_name(store(), login.vendor_id)
    back_url = f"/credentials?{urlencode({'vendor': login.vendor_id})}"
    text = "\n".join(login.editors) if text is None else text
    content = (
        f'<div class="breadcrumb"><a href="{_e(back_url)}">← Back to {_e(v_name)} logins</a></div>'
        '<div class="card">'
        '  <div class="card-header">'
        f'    <h3 class="card-title">{_e(v_name)}: who can change the shared login</h3>'
        '    <p class="card-subtitle">These people can change its password on this page. '
        'Admins always can. Nobody here can see the password.</p>'
        '  </div>'
        f'  {_alert(error)}'
        '  <form method="post" action="/credentials/editors">'
        f'    <input type="hidden" name="csrf" value="{csrf_token(user)}">'
        f'    <input type="hidden" name="vendor" value="{_e(login.vendor_id)}">'
        f'    <input type="hidden" name="owner" value="{_e(login.owner)}">'
        '    <label for="editors">Emails <span class="input-desc">(one per line; empty = admins only)</span></label>'
        f'    <textarea id="editors" name="editors" rows="6">{_e(text)}</textarea>'
        '    <div class="btn-group">'
        '      <button type="submit" class="btn btn-primary">Save</button>'
        f'      <a href="{_e(back_url)}" class="btn btn-secondary">Back</a>'
        '    </div>'
        '  </form>'
        '</div>'
    )
    return page(content, user=user, status=status)


def _shared_login_for_admin(user: dict, vendor_id: str, owner: str) -> Optional[Login]:
    if not is_admin(user["email"], get_settings().credential_admins):
        return None
    login = _find(user, vendor_id, owner)
    return login if login and login.is_shared else None


@router.get("/credentials/editors")
def credentials_editors(request: Request, vendor: str = "", owner: str = "") -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    login = _shared_login_for_admin(user, vendor, owner)
    if not login:
        return _not_admin(user)
    return _editors_form(user, login)


@router.post("/credentials/editors")
async def credentials_editors_save(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = _read_form(await request.body())
    if not check_csrf(user, form.get("csrf", "")):
        return page('<div class="card"><p class="err">This form has expired. <a href="/credentials">Start '
                    "again</a>.</p></div>", user=user, status=400)
    login = _shared_login_for_admin(user, form.get("vendor", ""), form.get("owner", ""))
    if not login:
        return _not_admin(user)
    editors, problem = _parse_emails(form.get("editors", ""))
    if problem:
        return _editors_form(user, login, form.get("editors", ""), problem, status=400)
    st = store()
    st.update_account(login.account_id, {"editors": editors})
    st.audit("login_editors_changed", account_id=login.account_id, by=user["email"],
             added=sorted(set(editors) - set(login.editors)),
             removed=sorted(set(login.editors) - set(editors)))
    logger.info("Editors of %s changed by %s", login.account_id, user["email"])
    return page(
        f'<div class="card"><div class="alert ok"><div><strong>Saved.</strong><br>'
        f'{_e(", ".join(editors) or "Only the admins")} can change this login now.</div></div>'
        f'<div class="btn-group"><a href="/credentials?{_e(urlencode({"vendor": login.vendor_id}))}" '
        'class="btn btn-primary">Back to logins</a></div></div>', user=user)


# -- find a login made outside this page ---------------------------------------------
# Our account can't list secrets, but it can read any sourcing-* secret by name, and
# the name follows from vendor + owner. So a login whose secret was made with gcloud
# is found by asking for it, then listed like one the worker has used.

def _find_form(user: dict, form: Optional[dict] = None, error: str = "", note: str = "",
               status: int = 200) -> Response:
    form = form or {}
    admin = is_admin(user["email"], get_settings().credential_admins)
    owner_field = (
        '    <label for="owner_email">Team member\'s email <span class="input-desc">(admins: '
        'to find someone else\'s own login; blank = shared and your own)</span></label>'
        f'    <input type="text" id="owner_email" name="owner_email" value="{_e(form.get("owner_email"))}">'
    ) if admin else ""
    ok = f'<div class="alert ok"><div>{note}</div></div>' if note else ""
    content = (
        '<div class="breadcrumb"><a href="/credentials">← Logins</a></div>'
        '<div class="card">'
        '  <div class="card-header">'
        '    <h2 class="card-title">Find a login</h2>'
        '    <p class="card-subtitle">For a login that was set up in Secret Manager directly and '
        'is not in the list yet. We look for the shared login and your own.</p>'
        '  </div>'
        f'  {_alert(error)}{ok}'
        '  <form method="post" action="/credentials/find" autocomplete="off">'
        f'    <input type="hidden" name="csrf" value="{csrf_token(user)}">'
        '    <label for="vendor">Vendor</label>'
        f'    <input type="text" id="vendor" name="vendor" required value="{_e(form.get("vendor"))}">'
        f'{owner_field}'
        '    <div class="btn-group">'
        '      <button type="submit" class="btn btn-primary">Find</button>'
        '      <a href="/credentials" class="btn btn-secondary">Back</a>'
        '    </div>'
        '  </form>'
        '</div>'
    )
    return page(content, user=user, status=status)


@router.get("/credentials/find")
def credentials_find_form(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    return _find_form(user)


async def _secret_exists(vendor_id: str, owner: str) -> bool:
    try:
        return await creds()._fetch(secret_key(vendor_id, owner)) is not None
    except Exception as exc:
        # Not JSON, or a secret with no version yet: nothing the worker could use.
        logger.warning("Looking up %s: %s", secret_key(vendor_id, owner), type(exc).__name__)
        return False


@router.post("/credentials/find")
async def credentials_find(request: Request) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    form = _read_form(await request.body())
    if not check_csrf(user, form.get("csrf", "")):
        return page('<div class="card"><p class="err">This form has expired. <a href="/credentials/find">'
                    "Start again</a>.</p></div>", user=user, status=400)
    vendor_id = canonical_vendor_id(slugify(form.get("vendor", "")))
    if not vendor_id or len(vendor_id) > 40:
        return _find_form(user, form, "Type the vendor's name.", status=400)
    admins = get_settings().credential_admins
    owners = [SHARED, user["email"]]
    other = form.get("owner_email", "").strip().lower()
    if other and is_admin(user["email"], admins):
        problem = _email_problem(other)
        if problem:
            return _find_form(user, form, problem, status=400)
        owners = [other]

    st = store()
    found = []
    for owner in owners:
        if not await _secret_exists(vendor_id, owner):
            continue
        aid = account_id(vendor_id, owner)
        if st.get_account(aid) is None:
            st.update_account(aid, {"vendor_id": vendor_id, "user_email": owner,
                                    "secret_key": secret_key(vendor_id, owner), "editors": []})
            st.audit("login_found", account_id=aid, by=user["email"])
        found.append(Login(vendor_id, owner))
    if not found:
        names = ", ".join(secret_key(vendor_id, o) for o in owners)
        return _find_form(user, form, f"No login found (looked for {names}). The name must be "
                          "exactly this; ask the SourcingAI admin.", status=404)
    lines = []
    for login in found:
        mine = can_edit(user["email"], login_from_record(
            login.account_id, st.get_account(login.account_id) or {}) or login, admins)
        lines.append(f"{_e(_who(login, user['email']))} ({_e(login.secret_key)}): "
                     + ("you can change it now." if mine else
                        "listed; ask an admin to let you change it."))
    v_link = f'<a href="/credentials?{_e(urlencode({"vendor": vendor_id}))}">{_e(vendor_name(st, vendor_id))} logins</a>'
    return _find_form(user, {}, note="Found:<br>" + "<br>".join(lines) + f"<br>{v_link}")
