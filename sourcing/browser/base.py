"""What every browser backend hands back, so the login and search code never
needs to know which one is running (BROWSER_BACKEND=browserbase|local)."""
from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, Page, Playwright


@dataclass
class BrowserHandle:
    session_id: str
    page: Page
    browser: Browser
    playwright: Playwright


class ContextNotFound(Exception):
    """The saved Context doesn't exist for this backend (deleted, made by the
    other backend, or the API key / project was switched to another account).
    The caller starts a fresh Context and logs in again."""


class LiveViewUnavailable(Exception):
    """This backend can't show the browser to a person (needed for the 2FA
    handoff, plan step 5)."""
