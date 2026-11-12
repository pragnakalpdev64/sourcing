"""Runtime settings.

On Cloud Run, infra secrets come from GCP Secret Manager mounted as env vars
(`--set-secrets` in cloudbuild.yaml), so this module only ever reads env.
Locally, a `.env` file next to this repo is loaded if present.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional at runtime
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def captcha_solving_allowed() -> bool:
    """REMOVE-AFTER-CAPTCHA-TRIAL. The client's "never solve a CAPTCHA" rule is
    paused for a few days of development (allowed 2026-11-05). DEV_ALLOW_CAPTCHA_SOLVING=1
    lets the browser service solve CAPTCHAs and makes adapters wait for it before
    handing the challenge to a person. Off by default; never set it on Cloud Run."""
    return _env_bool("DEV_ALLOW_CAPTCHA_SOLVING")


def _env_list(name: str) -> tuple:
    # ";" too: gcloud's --set-env-vars splits on commas (cloudbuild.yaml).
    return tuple(v.lower() for v in re.split(r"[,;\s]+", _env(name)) if v)


@dataclass(frozen=True)
class Settings:
    # GCP
    gcp_project: str
    region: str
    job_name: str

    # Google Sheet
    sheet_id: str
    requests_tab: str
    results_tab: str

    # State store: "firestore" on Cloud Run; "file" (local JSON file, survives
    # between runs) or "memory" (lost when the run ends) for local runs / tests
    store_backend: str
    store_file: str

    # Vendor credentials (GCP Secret Manager, in gcp_project), e.g.
    # "sourcing-vendor-proponent=sourcing-vendor-test" reads Proponent's shared login
    # from the test secret.
    vendor_secret_overrides: str

    # Browser: "browserbase" (remote browser, saved Contexts, Live View) or
    # "local" (Chromium inside this container / on this machine). Both are run
    # on Cloud Run to compare them (client request 2026-10-15).
    browser_backend: str
    # Local browser only: false opens a visible window (local debugging).
    local_browser_headless: bool
    # Local browser only: a DevTools live view on 127.0.0.1 for the 2FA handoff
    # (testing it without Browserbase; the link works on this machine only).
    local_browser_live_view: bool

    # Browserbase
    browserbase_api_key: str
    browserbase_project_id: str
    # Session recordings show everything on screen, login pages included
    # (plan 6.5): off by default, turn on for local debugging only.
    browserbase_record_sessions: bool
    # Browserbase closes the browser at this limit even mid-run (its default is
    # 15 min); keep it at least as long as the job lock.
    browserbase_session_timeout: int

    # LLM: pick a provider per LLM_PROVIDER, model per LLM_MODEL. Only the chosen
    # provider's key needs to be set. No defaults: a missing value stops the run
    # instead of silently using a model/effort nobody chose.
    llm_provider: str
    llm_model: str
    llm_effort: str
    anthropic_api_key: str
    openai_api_key: str
    gemini_api_key: str

    @property
    def llm_api_key(self) -> str:
        return {"anthropic": self.anthropic_api_key, "openai": self.openai_api_key,
                "gemini": self.gemini_api_key}.get(self.llm_provider, "")

    # Notifications / service
    chat_webhook_url: str
    trigger_secret: str
    # Local testing of the sheet's Apps Script (through ngrok): /trigger starts
    # `python -m service.job_main` on this machine instead of the Cloud Run job,
    # with TRIGGER_LOCAL_ARGS added (e.g. "--dummy"). Never set on Cloud Run.
    trigger_run_local: bool
    trigger_local_args: str
    # Where the API runs (Cloud Run URL, or http://localhost:8080 locally). Set =
    # a 2FA code the worker can't make is asked from the requester over Chat
    # with a link to the API's code form (sourcing/auth/hitl.py).
    public_base_url: str
    # Optional Chat app (client sets it up): the space it posts the code card to,
    # and the audience Chat signs its events for (the app's GCP project number).
    chat_app_space: str
    chat_app_audience: str
    hitl_code_wait_seconds: int

    # Login update page (service/credentials_ui.py): Google sign-in with an OAuth
    # client the client makes in their project; redirect URI
    # <PUBLIC_BASE_URL>/auth/callback.
    google_oauth_client_id: str
    google_oauth_client_secret: str
    session_secret: str              # signs the session cookie and form tokens
    allowed_email_domains: tuple     # who may sign in, e.g. ("skylink.com",)
    credential_admins: tuple         # may change every login, shared ones included
    # Local testing only (refused on Cloud Run): sign in as this email without Google.
    dev_signin_email: str

    # Behaviour
    dummy_mode: bool
    max_login_attempts: int
    lease_seconds: int
    job_lock_seconds: int
    max_page_chars: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gcp_project=_env("GCP_PROJECT") or _env("GOOGLE_CLOUD_PROJECT"),
            region=_env("GCP_REGION", "us-central1"),
            job_name=_env("WORKER_JOB_NAME", "sourcing-worker"),
            sheet_id=_env("SHEET_ID"),
            requests_tab=_env("REQUESTS_TAB", "Requests"),
            results_tab=_env("RESULTS_TAB", "Results"),
            store_backend=_env("STORE_BACKEND", "firestore").lower(),
            store_file=_env("STORE_FILE") or str(Path(__file__).resolve().parent.parent
                                                 / ".local_store.json"),
            vendor_secret_overrides=_env("VENDOR_SECRET_OVERRIDES"),
            browser_backend=_env("BROWSER_BACKEND", "browserbase").lower(),
            local_browser_headless=_env_bool("LOCAL_BROWSER_HEADLESS", True),
            local_browser_live_view=_env_bool("LOCAL_BROWSER_LIVE_VIEW"),
            browserbase_api_key=_env("BROWSERBASE_API_KEY"),
            browserbase_project_id=_env("BROWSERBASE_PROJECT_ID"),
            browserbase_record_sessions=_env_bool("BROWSERBASE_RECORD_SESSIONS"),
            browserbase_session_timeout=_env_int("BROWSERBASE_SESSION_TIMEOUT", 3600),
            llm_provider=_env("LLM_PROVIDER").lower(),
            llm_model=_env("LLM_MODEL"),
            llm_effort=_env("LLM_EFFORT").lower(),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_api_key=_env("OPENAI_API_KEY"),
            gemini_api_key=_env("GEMINI_API_KEY"),
            chat_webhook_url=_env("CHAT_WEBHOOK_URL"),
            trigger_secret=_env("TRIGGER_SECRET"),
            trigger_run_local=_env_bool("TRIGGER_RUN_LOCAL"),
            trigger_local_args=_env("TRIGGER_LOCAL_ARGS"),
            public_base_url=_env("PUBLIC_BASE_URL"),
            chat_app_space=_env("CHAT_APP_SPACE"),
            chat_app_audience=_env("CHAT_APP_AUDIENCE"),
            hitl_code_wait_seconds=_env_int("HITL_CODE_WAIT_SECONDS", 600),
            google_oauth_client_id=_env("GOOGLE_OAUTH_CLIENT_ID"),
            google_oauth_client_secret=_env("GOOGLE_OAUTH_CLIENT_SECRET"),
            session_secret=_env("SESSION_SECRET"),
            allowed_email_domains=_env_list("ALLOWED_EMAIL_DOMAINS"),
            credential_admins=_env_list("CREDENTIAL_ADMINS"),
            dev_signin_email=_env("DEV_SIGNIN_EMAIL").lower(),
            dummy_mode=_env_bool("DUMMY_MODE"),
            max_login_attempts=_env_int("MAX_LOGIN_ATTEMPTS", 2),
            lease_seconds=_env_int("ACCOUNT_LEASE_SECONDS", 1800),
            job_lock_seconds=_env_int("JOB_LOCK_SECONDS", 3600),
            max_page_chars=_env_int("MAX_PAGE_CHARS", 60000),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def quiet_http_logs() -> None:
    """The HTTP clients log every request URL at INFO, and the Chat webhook URL
    holds its key and token (seen in an onboarding log 2026-10-22): warnings only."""
    import logging

    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
