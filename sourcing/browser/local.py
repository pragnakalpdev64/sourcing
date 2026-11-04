"""Chromium started by Playwright inside this container (or on this machine).

The client asked to compare this with Browserbase on Cloud Run (2026-10-15).
Same methods as BrowserbaseClient, with these differences:
- Nothing is kept between runs: every session starts with no cookies. Runs log
  out at the end anyway (`logout_after_run`), so each run logs in either way.
- Live View only on this machine, when turned on (LOCAL_BROWSER_LIVE_VIEW=1):
  Chromium's own DevTools page for the open tab, on 127.0.0.1, for testing the
  2FA handoff without Browserbase (added 2026-10-22, Browserbase minutes ran
  out). Off: a 2FA step falls back to the code form.
- No recordings.
- The browser is left as Playwright starts it (no changed user agent or
  fingerprint): a bot check must stop the run, not be avoided (client rule).
"""
from __future__ import annotations

import logging
import socket
import uuid
from typing import Optional

import httpx

from playwright.async_api import async_playwright

from .base import BrowserHandle, LiveViewUnavailable

logger = logging.getLogger("sourcing.browser")

# Context ids made here start with this, so BrowserbaseClient can tell one
# apart from its own when the backend is switched (see its open()).
LOCAL_CONTEXT_PREFIX = "local-"

# Containers often have a small /dev/shm; Chromium then crashes on big pages.
# Playwright already passes --no-sandbox when needed, so it runs as non-root.
CHROMIUM_ARGS = ["--disable-dev-shm-usage"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalBrowserClient:
    def __init__(self, *, headless: bool = True, live_view: bool = False) -> None:
        self._headless = headless
        self._live_view = live_view
        self._debug_ports: dict[str, int] = {}     # session id -> DevTools port

    async def create_context(self) -> str:
        # Only a label stored on the account; there is nothing to create.
        return f"{LOCAL_CONTEXT_PREFIX}{uuid.uuid4().hex}"

    async def open(self, context_id: str, *, keep_alive: bool = False,
                   timeout_seconds: Optional[int] = None,
                   metadata: Optional[dict] = None, stealth: bool = False) -> BrowserHandle:
        if stealth:
            # Proxies / advanced stealth are Browserbase features.
            logger.info("Vendor asks for browser stealth: only on Browserbase; ignored locally")
        args = list(CHROMIUM_ARGS)
        port = None
        if self._live_view:
            # Bound to 127.0.0.1 by Chromium: the link only works on this machine.
            port = _free_port()
            args += [f"--remote-debugging-port={port}",
                     f"--remote-allow-origins=http://127.0.0.1:{port}"]
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=self._headless, args=args)
            context = await browser.new_context()
            page = await context.new_page()
        except Exception:
            await pw.stop()
            raise
        session_id = f"local-{uuid.uuid4().hex[:12]}"
        if port:
            self._debug_ports[session_id] = port
        logger.info("Opened local Chromium %s (session %s, headless=%s, live view=%s; no "
                    "saved cookies, context label %s)", browser.version, session_id,
                    self._headless, f"127.0.0.1:{port}" if port else "off", context_id)
        return BrowserHandle(session_id, page, browser, pw)

    # The worker waits (no time limit) for a confirmed logout before closing a
    # local browser (auth/hitl.hold_until_logged_out, team rule 2026-11-04).
    waits_for_logout = True

    @property
    def shows_window(self) -> bool:
        """A person can use the browser window (onboarding waits there for a logout)."""
        return not self._headless

    async def live_view_url(self, session_id: str) -> str:
        """DevTools for the session's open tab; its screencast shows the page and
        takes clicks and typing."""
        port = self._debug_ports.get(session_id)
        if not port:
            raise LiveViewUnavailable("Local browser started without live view "
                                      "(LOCAL_BROWSER_LIVE_VIEW=1 turns it on)")
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                targets = (await client.get(f"{base}/json/list")).json()
        except Exception as exc:
            raise LiveViewUnavailable(f"Local DevTools not reachable: {exc!r}") from exc
        pages = [t for t in targets if t.get("type") == "page"
                 and not str(t.get("url", "")).startswith("devtools://")]
        if not pages or not pages[0].get("id"):
            raise LiveViewUnavailable("No open tab to show")
        # The DevTools page Chromium serves itself, not the hosted one its
        # /json/list points to (appspot.com): nothing is loaded from outside.
        return (f"{base}/devtools/inspector.html?ws=127.0.0.1:{port}"
                f"/devtools/page/{pages[0]['id']}")

    async def close(self, handle: BrowserHandle, *, release: bool = True) -> None:
        # `release=False` (keep the browser for a person to finish 2FA) means
        # nothing here: a local browser can't outlive this process.
        self._debug_ports.pop(handle.session_id, None)
        try:
            await handle.browser.close()
        except Exception:
            pass
        try:
            await handle.playwright.stop()
        except Exception:
            pass
