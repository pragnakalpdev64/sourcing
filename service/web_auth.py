"""Google sign-in for the API's pages (login update page first; the 2FA pages
can use it later).

OpenID Connect authorization-code flow with an OAuth client the client makes in
their project (consent screen "Internal"):

  GET  /auth/login?next=/credentials  -> Google's sign-in page
  GET  /auth/callback                 -> ID token checked -> session cookie
  GET  /auth/grant?next=/credentials/new  admins: Google Cloud access (see below)
  POST /auth/logout

Sign-in uses only the person's identity (scopes openid + email). The session is
a cookie signed with SESSION_SECRET: no server state, so it works with any
number of Cloud Run instances.

Adding a login creates a secret, which our service account may not do. The
admin gives their own Google Cloud access for it (/auth/grant, scope
cloud-platform, Secret Manager has no narrower one), so Google's IAM decides
who may create secrets. Their token is kept encrypted in a cookie sent only to
/credentials/new, for GRANT_SECONDS, tied to the session; never logged or stored.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from sourcing.config import get_settings

logger = logging.getLogger("sourcing.web_auth")

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

SESSION_COOKIE = "sai_session"
STATE_COOKIE = "sai_oauth"
SESSION_SECONDS = 8 * 3600
STATE_SECONDS = 600          # time to finish Google's sign-in page
GRANT_COOKIE = "sai_gcp"
GRANT_PATH = "/credentials/new"
GRANT_SECONDS = 600          # enough to fill in the add-login form
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


# -- signed values --------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, data: str) -> str:
    return _b64(hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest())


def sign(data: dict, secret: str, max_age: int) -> str:
    body = _b64(json.dumps({**data, "exp": int(time.time()) + max_age}).encode())
    return f"{body}.{_mac(secret, body)}"


def unsign(token: str, secret: str) -> Optional[dict]:
    body, _, mac = (token or "").partition(".")
    if not body or not secret or not hmac.compare_digest(mac, _mac(secret, body)):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or float(data.get("exp") or 0) < time.time():
        return None
    return data


# -- session --------------------------------------------------------------------

def _secret() -> str:
    secret = get_settings().session_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Sign-in is not set up (SESSION_SECRET)")
    return secret


def current_user(request: Request) -> Optional[dict]:
    """{"email", "name", "sid"} of the signed-in person, or None."""
    data = unsign(request.cookies.get(SESSION_COOKIE, ""), get_settings().session_secret)
    return data if data and data.get("email") else None


def csrf_token(user: dict) -> str:
    return _mac(_secret(), f"csrf:{user['sid']}")


def check_csrf(user: dict, token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, csrf_token(user))


def _base_url(request: Request) -> str:
    # Cloud Run ends TLS in front of the app, so the request itself looks like
    # http; PUBLIC_BASE_URL is the address people (and Google) use.
    return (get_settings().public_base_url or str(request.base_url)).rstrip("/")


def _secure(request: Request) -> bool:
    return _base_url(request).startswith("https://")


def _set_cookie(resp: Response, request: Request, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=_secure(request),
                    samesite="lax", path="/")


def _safe_next(next_path: str) -> str:
    # Only a path on this site: "//evil.com" or "https://..." would send the
    # person elsewhere after signing in.
    if not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path:
        return "/credentials"
    return next_path


def login_redirect(request: Request) -> HTMLResponse:
    """Signed out: a sign-in page with a Google button that comes back here."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method != "GET":
        path = "/credentials"     # a form sent after the session ended can't be resent
    link = html.escape(f"/auth/login?{urlencode({'next': path})}")
    domains = _domains_text().strip()
    # Said before Google's screen, so nobody tries a personal Gmail first.
    notice = (
        '<div class="auth-notice">'
        f'<strong>Skylink accounts only.</strong> Use your {html.escape(domains)} Google '
        'Workspace account; personal Gmail and other accounts are refused.</div>'
    ) if domains else ""
    body = (
        '<div class="auth-card">'
        '  <div class="auth-icon">'
        '    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '      <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>'
        '      <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>'
        '    </svg>'
        '  </div>'
        '  <h3 class="auth-title">Authentication Required</h3>'
        f'  <p class="auth-desc">Sign in with your {_domains_text()}Google account to change a vendor login.</p>'
        f'  {notice}'
        f'  <div class="auth-action">{_google_button(link, "Sign in with Google")}</div>'
        '  <p class="auth-footer muted">Protected by Google Cloud IAM & Secret Manager.</p>'
        '</div>'
    )
    return page(body, status=401)


def _domains_text() -> str:
    # "@skylink.com " for the sign-in texts; empty when any account is allowed (local).
    domains = [d for d in get_settings().allowed_email_domains if d != "*"]
    return f'{" or ".join("@" + d for d in domains)} ' if domains else ""


def _google_button(link: str, label: str) -> str:
    return (
        f'<a class="google" href="{link}">'
        '  <svg class="google-svg" viewBox="0 0 24 24" width="20" height="20">'
        '    <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.66-5.17 3.66-9.17z"/>'
        '    <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.25v3.15C3.26 21.36 7.34 24 12 24z"/>'
        '    <path fill="#FBBC05" d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.25C.45 8.18 0 9.99 0 12s.45 3.82 1.25 5.42l4.03-3.15z"/>'
        '    <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.34 0 3.26 2.64 1.25 6.58l4.03 3.15c.95-2.83 3.6-4.98 6.72-4.98z"/>'
        '  </svg>'
        f'  <span>{html.escape(label)}</span>'
        '</a>'
    )


def signin_problem(title: str, text: str, *, status: int, button: str = "Try again",
                   note: str = "", link: str = "/auth/login") -> HTMLResponse:
    """A sign-in that didn't work: what went wrong and a way to start again. /auth/login
    always shows Google's account picker, so the button also changes the account."""
    note_html = f'<p class="auth-desc">{note}</p>' if note else ""
    body = (
        '<div class="auth-card">'
        '  <div class="auth-icon denied">'
        '    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '      <circle cx="12" cy="12" r="10"></circle><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line>'
        '    </svg>'
        '  </div>'
        f'  <h3 class="auth-title">{html.escape(title)}</h3>'
        f'  <p class="auth-desc">{html.escape(text)}</p>'
        f'  {note_html}'
        f'  <div class="auth-action">{_google_button(link, button)}</div>'
        '  <p class="auth-footer muted">If you should have access, ask the SourcingAI admin.</p>'
        '</div>'
    )
    return page(body, status=status)


def _start_session(request: Request, email: str, name: str, next_path: str) -> RedirectResponse:
    resp = RedirectResponse(_safe_next(next_path), status_code=303)
    token = sign({"email": email, "name": name, "sid": secrets.token_urlsafe(16)},
                 _secret(), SESSION_SECONDS)
    _set_cookie(resp, request, SESSION_COOKIE, token, SESSION_SECONDS)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


def page(body: str, *, user: Optional[dict] = None, status: int = 200) -> HTMLResponse:
    """A styled SaaS page with header, user session badge, and modern layout."""
    bar = ""
    if user:
        email = html.escape(user["email"])
        initial = email[0].upper() if email else "U"
        csrf = csrf_token(user)
        bar = (
            f'<div class="user-header">'
            f'  <div class="user-pill">'
            f'    <div class="user-avatar">{initial}</div>'
            f'    <div class="user-details">'
            f'      <span class="user-label">Signed in as</span>'
            f'      <span class="user-email">{email}</span>'
            f'    </div>'
            f'  </div>'
            f'  <form class="bar" method="post" action="/auth/logout">'
            f'    <input type="hidden" name="csrf" value="{csrf}">'
            f'    <button type="submit" class="btn-signout">'
            f'      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>'
            f'        <polyline points="16 17 21 12 16 7"></polyline>'
            f'        <line x1="21" y1="12" x2="9" y2="12"></line>'
            f'      </svg>'
            f'      Sign out'
            f'    </button>'
            f'  </form>'
            f'</div>'
        )
    return HTMLResponse(_PAGE.format(bar=bar, body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# Inline CSS and one inline script (show/hide password, "passwords match" hint);
# no outside fonts or files, so the page needs nothing but this service. The CSP
# in api.py allows only this.
_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SourcingAI vendor logins</title>
  <style>
    :root {{
      --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --bg-page: #f8fafc;
      --card-bg: #ffffff;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --border-color: #e2e8f0;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --primary-light: #eff6ff;
      --danger-bg: #fef2f2;
      --danger-text: #991b1b;
      --danger-border: #fecaca;
      --success-bg: #f0fdf4;
      --success-text: #166534;
      --success-border: #bbf7d0;
      --warning-bg: #fffbeb;
      --warning-text: #92400e;
      --warning-border: #fde68a;
      --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
      --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.07), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
      --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      font-family: var(--font-sans);
      background: var(--bg-page);
      color: var(--text-main);
      line-height: 1.5;
      min-height: 100vh;
      padding: 1.5rem 1rem 3rem;
    }}

    .app-container {{
      max-width: 58rem;
      margin: 0 auto;
    }}

    .navbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 2rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--border-color);
      flex-wrap: wrap;
      gap: 1rem;
    }}

    .brand {{
      display: inline-flex;
      align-items: center;
      gap: 0.75rem;
      text-decoration: none;
      color: var(--text-main);
      height: 40px;
    }}

    .brand-icon {{
      width: 40px;
      height: 40px;
      border-radius: var(--radius-sm);
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      color: #fff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 2px 4px rgba(37, 99, 235, 0.25);
      flex-shrink: 0;
    }}

    .brand-text h1 {{
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text-main);
      line-height: 1;
      display: flex;
      align-items: center;
    }}

    .brand-badge {{
      display: inline-block;
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      background: #e0e7ff;
      color: #3730a3;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      margin-left: 0.4rem;
    }}

    .user-header {{
      display: inline-flex;
      align-items: center;
      gap: 0.75rem;
      height: 40px;
    }}

    .user-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.65rem;
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: 9999px;
      padding: 0 0.85rem 0 0.35rem;
      height: 40px;
      box-sizing: border-box;
      box-shadow: var(--shadow-sm);
      white-space: nowrap;
    }}

    .user-avatar {{
      width: 30px;
      height: 30px;
      border-radius: 50%;
      background: #3b82f6;
      color: #fff;
      font-weight: 600;
      font-size: 0.85rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      line-height: 1;
    }}

    .user-details {{
      display: flex;
      flex-direction: column;
      justify-content: center;
    }}

    .user-label {{
      font-size: 0.68rem;
      color: var(--text-muted);
      line-height: 1.1;
      margin-bottom: 2px;
    }}

    .user-email {{
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text-main);
      line-height: 1.2;
    }}

    .bar {{
      margin: 0;
      display: inline-flex;
      align-items: center;
      height: 40px;
    }}

    .btn-signout,
    button.btn-signout,
    .bar button[type=submit] {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.45rem;
      background: var(--primary);
      border: 1px solid var(--primary);
      color: #ffffff;
      font-size: 0.8125rem;
      font-weight: 600;
      padding: 0 1rem;
      height: 40px;
      box-sizing: border-box;
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: all 0.15s ease;
      margin: 0 !important;
      margin-top: 0 !important;
      white-space: nowrap;
      box-shadow: 0 1px 2px 0 rgba(37, 99, 235, 0.25);
      line-height: 1;
    }}

    .btn-signout:hover,
    button.btn-signout:hover,
    .bar button[type=submit]:hover {{
      background: var(--primary-hover);
      border-color: var(--primary-hover);
      color: #ffffff;
    }}

    /* Card styling */
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-md);
      padding: 2rem;
      margin-bottom: 1.5rem;
    }}

    .card-header {{
      margin-bottom: 1.5rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border-color);
    }}

    .card-title, h2, h3 {{
      font-size: 1.35rem;
      font-weight: 700;
      color: var(--text-main);
      letter-spacing: -0.02em;
      margin-bottom: 0.35rem;
    }}

    .card-subtitle {{
      font-size: 0.875rem;
      color: var(--text-muted);
    }}

    /* Form inputs */
    label {{
      display: block;
      font-size: 0.875rem;
      font-weight: 600;
      color: #334155;
      margin-top: 1.25rem;
      margin-bottom: 0.4rem;
    }}

    .input-desc {{
      font-weight: 400;
      color: var(--text-muted);
      font-size: 0.8125rem;
      margin-left: 0.35rem;
    }}

    input[type=text], input[type=password], select, textarea {{
      font-family: inherit;
      font-size: 0.95rem;
      padding: 0.65rem 0.85rem;
      width: 100%;
      max-width: 28rem;
      background: #fff;
      border: 1px solid #cbd5e1;
      border-radius: var(--radius-sm);
      color: var(--text-main);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}

    input[type=text]:focus, input[type=password]:focus, select:focus, textarea:focus {{
      border-color: var(--primary);
      outline: none;
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
    }}

    .pw-wrapper {{
      position: relative;
      max-width: 28rem;
    }}

    .pw-wrapper input {{
      padding-right: 2.75rem;
    }}

    .btn-toggle-pw {{
      position: absolute;
      right: 0.65rem;
      top: 50%;
      transform: translateY(-50%);
      background: transparent;
      border: none;
      color: #94a3b8;
      cursor: pointer;
      padding: 0.3rem;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 4px;
    }}

    .btn-toggle-pw:hover {{
      color: #334155;
    }}

    .eye-icon-slash {{
      display: none;
    }}

    .btn-toggle-pw[aria-pressed="true"] .eye-icon-open {{
      display: none;
    }}

    .btn-toggle-pw[aria-pressed="true"] .eye-icon-slash {{
      display: block;
    }}

    .pw-match {{
      font-size: 0.8rem;
      margin-top: 0.4rem;
      font-weight: 500;
      min-height: 1.2rem;
    }}
    .pw-match-ok {{ color: #16a34a; }}
    .pw-match-err {{ color: #dc2626; }}

    /* Buttons */
    .btn, button[type=submit]:not(.btn-signout) {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.45rem;
      font-family: inherit;
      font-size: 0.9375rem;
      font-weight: 600;
      padding: 0 1.4rem;
      height: 42px;
      box-sizing: border-box;
      border-radius: var(--radius-sm);
      cursor: pointer;
      text-decoration: none;
      transition: all 0.15s ease;
      border: 1px solid transparent;
      margin: 0;
      white-space: nowrap;
      line-height: 1;
    }}

    .btn-primary, button[type=submit]:not(.btn-signout) {{
      background: var(--primary);
      border-color: var(--primary);
      color: #ffffff;
      box-shadow: 0 1px 2px 0 rgba(37, 99, 235, 0.3);
    }}

    .btn-primary:hover, button[type=submit]:not(.btn-signout):hover {{
      background: var(--primary-hover);
      border-color: var(--primary-hover);
      box-shadow: 0 2px 4px 0 rgba(37, 99, 235, 0.4);
    }}

    .btn-secondary {{
      background: #ffffff;
      border-color: #cbd5e1;
      color: #334155;
    }}

    .btn-secondary:hover {{
      background: #f8fafc;
      border-color: #94a3b8;
    }}

    .btn-sm {{
      font-size: 0.8125rem;
      font-weight: 500;
      padding: 0 0.75rem;
      height: 32px;
      margin: 0;
      line-height: 1;
      white-space: nowrap;
    }}

    .btn-group {{
      display: flex;
      align-items: center;
      gap: 0.85rem;
      margin-top: 1.75rem;
    }}

    .btn-group .btn,
    .btn-group button,
    .btn-group a,
    .btn-group button[type=submit] {{
      margin: 0 !important;
      margin-top: 0 !important;
    }}

    /* Table */
    .table-card {{
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      border: 1px solid var(--border-color);
      border-radius: var(--radius-md);
      margin: 1.25rem 0;
      width: 100%;
    }}

    .table-card table {{
      min-width: 520px;
    }}

    table {{
      border-collapse: collapse;
      width: 100%;
      font-size: 0.9125rem;
    }}

    th {{
      background: #f8fafc;
      color: #475569;
      font-weight: 600;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.75rem 0.85rem;
      border-bottom: 1px solid var(--border-color);
      text-align: left;
    }}

    td {{
      padding: 0.75rem 0.85rem;
      border-bottom: 1px solid #f1f5f9;
      color: #334155;
      vertical-align: middle;
    }}

    tr:last-child td {{
      border-bottom: none;
    }}

    tr:hover td {{
      background: #fafafa;
    }}

    .col-status {{
      white-space: nowrap;
    }}

    .col-action {{
      text-align: right;
      white-space: nowrap;
      width: 1%;
    }}

    .col-changed {{
      white-space: nowrap;
    }}

    .timestamp {{
      font-weight: 500;
      color: var(--text-main);
      font-size: 0.8125rem;
      line-height: 1.35;
      white-space: nowrap;
    }}

    .author {{
      font-size: 0.75rem;
      color: var(--text-muted);
      line-height: 1.35;
      white-space: nowrap;
    }}

    /* Badges */
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.2rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.8125rem;
      font-weight: 500;
      line-height: 1.25;
      white-space: nowrap;
      cursor: default;
    }}

    .badge[title] {{
      cursor: help;
    }}

    .badge-dot {{
      width: 0.45rem;
      height: 0.45rem;
      border-radius: 50%;
      flex-shrink: 0;
    }}

    .badge-success {{
      background: var(--success-bg);
      color: var(--success-text);
      border: 1px solid var(--success-border);
    }}
    .badge-success .badge-dot {{ background: #16a34a; }}

    .badge-danger {{
      background: var(--danger-bg);
      color: var(--danger-text);
      border: 1px solid var(--danger-border);
    }}
    .badge-danger .badge-dot {{ background: #dc2626; }}

    .badge-warning {{
      background: var(--warning-bg);
      color: var(--warning-text);
      border: 1px solid var(--warning-border);
    }}
    .badge-warning .badge-dot {{ background: #d97706; }}

    .badge-neutral {{
      background: #f1f5f9;
      color: #475569;
      border: 1px solid #e2e8f0;
    }}
    .badge-neutral .badge-dot {{ background: #94a3b8; }}

    /* Alerts */
    .err, .ok, .alert {{
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
      padding: 0.95rem 1.15rem;
      border-radius: var(--radius-sm);
      margin-bottom: 1.25rem;
      font-size: 0.9rem;
      line-height: 1.45;
    }}

    .err, .alert-danger {{
      background: var(--danger-bg);
      border: 1px solid var(--danger-border);
      color: var(--danger-text);
    }}

    .ok, .alert-success {{
      background: var(--success-bg);
      border: 1px solid var(--success-border);
      color: var(--success-text);
    }}

    .alert-icon {{
      flex-shrink: 0;
      margin-top: 2px;
    }}

    .muted {{
      color: var(--text-muted);
      font-size: 0.85rem;
    }}

    .action-footer {{
      margin-top: 1.25rem;
    }}

    /* Breadcrumbs */
    .breadcrumb {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.875rem;
      color: var(--text-muted);
      margin-bottom: 1.25rem;
    }}

    .breadcrumb a {{
      color: var(--primary);
      text-decoration: none;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}

    .breadcrumb a:hover {{
      text-decoration: underline;
    }}

    /* Sign in view */
    .auth-card {{
      max-width: 26rem;
      margin: 3rem auto;
      text-align: center;
      background: var(--card-bg);
      border: 1px solid var(--border-color);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-lg);
      padding: 2.5rem 2rem;
    }}

    .auth-icon {{
      width: 56px;
      height: 56px;
      margin: 0 auto 1.25rem;
      background: #eff6ff;
      color: var(--primary);
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    .auth-icon.denied {{
      background: var(--danger-bg);
      color: var(--danger-text);
    }}

    .auth-notice {{
      margin: -0.75rem 0 1.5rem;
      padding: 0.7rem 0.9rem;
      background: var(--warning-bg);
      border: 1px solid var(--warning-border);
      border-radius: var(--radius-sm);
      color: var(--warning-text);
      font-size: 0.85rem;
      text-align: left;
    }}

    .auth-desc + .auth-desc {{
      margin-top: -1rem;
    }}

    .auth-title {{
      font-size: 1.35rem;
      font-weight: 700;
      margin-bottom: 0.5rem;
      color: var(--text-main);
    }}

    .auth-desc {{
      font-size: 0.9rem;
      color: var(--text-muted);
      margin-bottom: 1.75rem;
    }}

    a.google {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.75rem;
      width: 100%;
      padding: 0.75rem 1.25rem;
      background: #ffffff;
      border: 1px solid #cbd5e1;
      border-radius: var(--radius-sm);
      text-decoration: none;
      color: #1e293b;
      font-size: 0.95rem;
      font-weight: 600;
      box-shadow: var(--shadow-sm);
      transition: all 0.15s ease;
    }}

    a.google:hover {{
      background: #f8fafc;
      border-color: #94a3b8;
      box-shadow: var(--shadow-md);
    }}

    .google-svg {{
      flex-shrink: 0;
    }}

    .auth-footer {{
      margin-top: 1.75rem;
      font-size: 0.75rem;
      line-height: 1.4;
    }}

    /* Responsive styles */
    @media (max-width: 640px) {{
      body {{
        padding: 1rem 0.75rem 2rem;
      }}

      .navbar {{
        flex-direction: column;
        align-items: stretch;
        gap: 0.85rem;
        margin-bottom: 1.25rem;
        padding-bottom: 1rem;
      }}

      .user-header {{
        width: 100%;
        justify-content: space-between;
      }}

      .card {{
        padding: 1.25rem 1rem;
        border-radius: var(--radius-md);
      }}

      .card-title, h2, h3 {{
        font-size: 1.2rem;
      }}

      .table-card {{
        margin: 1rem 0;
        border-radius: var(--radius-sm);
      }}

      th, td {{
        padding: 0.65rem 0.75rem;
      }}
    }}
  </style>
  <script>
    document.addEventListener('DOMContentLoaded', function () {{
      var toggleButtons = document.querySelectorAll('.btn-toggle-pw');
      toggleButtons.forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          var targetId = btn.getAttribute('data-target');
          if (!targetId) return;
          var input = document.getElementById(targetId);
          if (!input) return;

          var isPressed = btn.getAttribute('aria-pressed') === 'true';
          if (isPressed) {{
            input.type = 'password';
            btn.setAttribute('aria-pressed', 'false');
            btn.setAttribute('aria-label', 'Show password');
          }} else {{
            input.type = 'text';
            btn.setAttribute('aria-pressed', 'true');
            btn.setAttribute('aria-label', 'Hide password');
          }}
        }});
      }});

      var p1 = document.getElementById('pwd1');
      var p2 = document.getElementById('pwd2');
      var indicator = document.getElementById('pw-match-indicator');

      function checkMatch() {{
        if (!p1 || !p2 || !indicator) return;
        if (!p2.value) {{
          indicator.textContent = '';
          indicator.className = 'pw-match';
          return;
        }}
        if (p1.value === p2.value) {{
          indicator.textContent = '✓ Passwords match';
          indicator.className = 'pw-match pw-match-ok';
        }} else {{
          indicator.textContent = '✗ Passwords do not match';
          indicator.className = 'pw-match pw-match-err';
        }}
      }}

      if (p1 && p2 && indicator) {{
        p1.addEventListener('input', checkMatch);
        p2.addEventListener('input', checkMatch);
      }}
    }});
  </script>
</head>
<body>
  <div class="app-container">
    <header class="navbar">
      <a class="brand" href="/credentials">
        <div class="brand-icon">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
            <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
          </svg>
        </div>
        <div class="brand-text">
          <h1>SourcingAI<span class="brand-badge">Logins</span></h1>
        </div>
      </a>
      {bar}
    </header>
    <main>
      {body}
    </main>
  </div>
</body>
</html>"""


# -- Google -----------------------------------------------------------------------

def _dev_email() -> str:
    # Local testing without an OAuth client; never on Cloud Run.
    email = get_settings().dev_signin_email
    return email if email and not os.environ.get("K_SERVICE") else ""


async def exchange_code(code: str, redirect_uri: str, with_token: bool = False):
    """Google's verified ID token claims for an authorization code; with_token:
    (claims, {"access_token", "scope"})."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code, "client_id": s.google_oauth_client_id,
            "client_secret": s.google_oauth_client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        })
    if resp.status_code != 200:
        raise ValueError(f"token exchange failed ({resp.status_code})")
    reply = resp.json()
    raw = reply.get("id_token") or ""

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims = await asyncio.to_thread(id_token.verify_oauth2_token, raw,
                                     google_requests.Request(), s.google_oauth_client_id)
    if with_token:
        return claims, {"access_token": reply.get("access_token") or "",
                        "scope": reply.get("scope") or ""}
    return claims


def email_allowed(claims: dict) -> Optional[str]:
    """Why these claims may not sign in, or None if they may."""
    domains = get_settings().allowed_email_domains
    email = str(claims.get("email") or "").strip().lower()
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return "not signed in by Google"
    if not email or claims.get("email_verified") is not True:
        return "Google did not confirm the email address"
    if not domains:
        return "no email domain is allowed yet (ALLOWED_EMAIL_DOMAINS)"
    if "*" in domains:
        # Any verified Google account, Gmail too: for local tests, where the OAuth
        # client's test-user list decides who gets in. Never on Cloud Run.
        if os.environ.get("K_SERVICE"):
            return "ALLOWED_EMAIL_DOMAINS=* is for local testing only"
        return None
    domain = email.rpartition("@")[2]
    # `hd` is set only for Workspace accounts; a personal Gmail with a forwarded
    # company address has none.
    if domain not in domains or (claims.get("hd") or "").lower() != domain:
        return f"{email} is not a Skylink Google account"
    return None


@router.get("/auth/login")
def auth_login(request: Request, next: str = "/credentials") -> Response:
    dev = _dev_email()
    if dev:
        logger.warning("DEV_SIGNIN_EMAIL: signed in as %s without Google", dev)
        return _start_session(request, dev, dev, next)
    s = get_settings()
    if not s.google_oauth_client_id or not s.google_oauth_client_secret:
        return page("<p class=err>Google sign-in is not set up yet (OAuth client).</p>",
                    status=503)
    return _to_google(request, next, {"scope": "openid email profile",
                                      "prompt": "select_account"})


def _to_google(request: Request, next_path: str, params: dict, grant: str = "") -> Response:
    s = get_settings()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    params = {"client_id": s.google_oauth_client_id, "response_type": "code",
              "redirect_uri": f"{_base_url(request)}/auth/callback",
              "state": state, "nonce": nonce, **params}
    if len(s.allowed_email_domains) == 1 and s.allowed_email_domains[0] != "*":
        params["hd"] = s.allowed_email_domains[0]   # only a hint; checked again on return
    resp = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=303)
    _set_cookie(resp, request, STATE_COOKIE,
                sign({"state": state, "nonce": nonce, "next": _safe_next(next_path),
                      "grant": grant}, _secret(), STATE_SECONDS), STATE_SECONDS)
    return resp


# -- admin's Google Cloud access (add a login) -------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    key = hashlib.sha256(f"gcp-grant:{_secret()}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def gcp_credentials(request: Request, user: dict):
    """The signed-in person's own Google Cloud access, or None (ask with /auth/grant)."""
    if _dev_email():
        # Local testing: the developer's own gcloud login, as the person.
        try:
            import google.auth

            return google.auth.default(scopes=[GCP_SCOPE])[0]
        except Exception as exc:
            logger.warning("No local Google credentials for adding a login: %r", exc)
            return None
    raw = request.cookies.get(GRANT_COOKIE, "")
    if not raw:
        return None
    try:
        data = json.loads(_fernet().decrypt(raw.encode(), ttl=GRANT_SECONDS))
    except Exception:
        return None
    if data.get("sid") != user["sid"] or data.get("email") != user["email"]:
        return None
    from google.oauth2.credentials import Credentials

    return Credentials(data["token"])


def forget_grant(resp: Response) -> None:
    resp.delete_cookie(GRANT_COOKIE, path=GRANT_PATH)


@router.get("/auth/grant")
def auth_grant(request: Request, next: str = GRANT_PATH) -> Response:
    user = current_user(request)
    if not user:
        return login_redirect(request)
    s = get_settings()
    if not s.google_oauth_client_id or not s.google_oauth_client_secret:
        return page("<p class=err>Google sign-in is not set up yet (OAuth client).</p>",
                    user=user, status=503)
    # No refresh token (access_type online): the access ends within the hour.
    return _to_google(request, next, {"scope": f"openid email {GCP_SCOPE}",
                                      "login_hint": user["email"], "access_type": "online"},
                      grant=user["sid"])


def _finish_grant(request: Request, saved: dict, claims: dict, token: dict) -> Response:
    user = current_user(request)
    email = str(claims.get("email") or "").strip().lower()
    problem = email_allowed(claims)
    if not problem and (not user or user["sid"] != saved["grant"] or email != user["email"]):
        problem = "the Google account is not the one signed in here"
    if not problem and claims.get("nonce") != saved.get("nonce"):
        problem = "sign-in reply does not match this browser"
    if not problem and (GCP_SCOPE not in token["scope"].split() or not token["access_token"]):
        problem = "Google Cloud access was not given"
    if problem:
        logger.warning("Cloud access refused: %s", problem)
        resp = signin_problem("Access not given", f"Can't add logins: {problem}.", status=403,
                              link=f"/auth/grant?{urlencode({'next': saved.get('next', '')})}")
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp
    logger.info("Cloud access given for adding a login: %s", email)
    resp = RedirectResponse(_safe_next(saved.get("next", "")), status_code=303)
    value = _fernet().encrypt(json.dumps({"sid": user["sid"], "email": email,
                                          "token": token["access_token"]}).encode()).decode()
    resp.set_cookie(GRANT_COOKIE, value, max_age=GRANT_SECONDS, httponly=True,
                    secure=_secure(request), samesite="lax", path=GRANT_PATH)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "",
                        error: str = "") -> Response:
    saved = unsign(request.cookies.get(STATE_COOKIE, ""), _secret())
    if error or not code or not saved or not hmac.compare_digest(state, saved.get("state", "")):
        return signin_problem("Sign-in did not finish",
                              "Google sign-in was cancelled or took too long.", status=400)
    try:
        reply = await exchange_code(code, f"{_base_url(request)}/auth/callback",
                                    with_token=bool(saved.get("grant")))
    except Exception as exc:
        logger.warning("Google sign-in failed: %r", exc)
        return signin_problem("Google sign-in failed",
                              "Google did not confirm the sign-in.", status=400)
    if saved.get("grant"):
        return _finish_grant(request, saved, *reply)
    claims = reply
    problem = email_allowed(claims)
    if not problem and claims.get("nonce") != saved.get("nonce"):
        problem = "sign-in reply does not match this browser"
    if problem:
        logger.warning("Sign-in refused: %s", problem)
        domains = _domains_text().strip()
        resp = signin_problem(
            "Access denied", f"Can't sign in: {problem}.", status=403,
            button="Sign in with a different account",
            note=f"Use your {html.escape(domains)} Google Workspace account." if domains else "")
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp
    email = claims["email"].strip().lower()
    logger.info("Signed in: %s", email)
    return _start_session(request, email, str(claims.get("name") or email), saved.get("next", ""))


@router.post("/auth/logout")
async def auth_logout(request: Request) -> Response:
    from urllib.parse import parse_qs

    user = current_user(request)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    if user and not check_csrf(user, (form.get("csrf") or [""])[0]):
        raise HTTPException(status_code=400, detail="bad form token")
    resp = page("<p>Signed out.</p>")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    forget_grant(resp)
    return resp
