"""Browserbase sessions with saved Contexts, keep-alive and Live View.

Playwright connects over CDP; the browser runs at Browserbase, not in this
container (that is BROWSER_BACKEND=local, see local.py). One Context per (vendor, user) account holds that
account's cookies; it is saved when the session ends (persist=True).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from playwright.async_api import async_playwright

# Re-exported: older imports use sourcing.browser.browserbase for these.
from .base import BrowserHandle, ContextNotFound  # noqa: F401
from ..config import captcha_solving_allowed
from .local import LOCAL_CONTEXT_PREFIX

logger = logging.getLogger("sourcing.browser")


def _is_context_not_found(exc: Exception) -> bool:
    text = str(exc).lower()
    return "context" in text and ("not found" in text or "does not exist" in text)


class BrowserbaseClient:
    def __init__(self, api_key: str, project_id: str, *, record_sessions: bool = False,
                 session_timeout_seconds: int = 3600) -> None:
        if not api_key or not project_id:
            raise ValueError("BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID are not set")
        from browserbase import Browserbase

        self._bb = Browserbase(api_key=api_key)
        self._project_id = project_id
        self._record = record_sessions
        self._session_timeout = session_timeout_seconds
        if record_sessions:
            logger.warning("Browserbase session recording is ON: recordings show the vendor "
                           "pages, login screens included. Use for local debugging only.")

    async def create_context(self) -> str:
        ctx = await asyncio.to_thread(self._bb.contexts.create, project_id=self._project_id)
        return ctx.id

    async def open(self, context_id: str, *, keep_alive: bool = False,
                   timeout_seconds: Optional[int] = None,
                   metadata: Optional[dict] = None, stealth: bool = False) -> BrowserHandle:
        """stealth: Browserbase proxy (vendor setting browser_stealth)."""
        # A label made by the local backend: it was never a Browserbase Context.
        if context_id.startswith(LOCAL_CONTEXT_PREFIX):
            raise ContextNotFound(f"{context_id} was made by BROWSER_BACKEND=local")
        # Browserbase ends the session (and the browser with it) at this limit,
        # even mid-run, so it must outlast the longest run. Billing is by the
        # minutes actually used: the session is released as soon as the run ends.
        try:
            session = await self._create_session(context_id, keep_alive, timeout_seconds,
                                                  metadata, stealth)
        except Exception as exc:
            if _is_context_not_found(exc):
                raise ContextNotFound(f"Browserbase Context {context_id} not found") from exc
            raise
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
        except Exception:
            await pw.stop()
            await self._release(session.id)
            raise
        logger.info("Opened Browserbase session %s (context=%s, keep_alive=%s, recording=%s, "
                    "stealth=%s)", session.id, context_id, keep_alive,
                    "on" if self._record else "off",
                    "proxy" if stealth else "basic")
        # REMOVE-BEFORE-DEPLOY (asked 2026-10-23, for watching test runs): the
        # session's page in the Browserbase dashboard (the replay, when recording
        # is on) and a live view of the browser while it runs. The live view link
        # lets whoever has it see and use the logged-in session: local logs only.
        logger.info("Session page: https://www.browserbase.com/sessions/%s%s", session.id,
                    "" if self._record else " (recording off: no replay)")
        try:
            logger.info("Session live view (while the run lasts): %s",
                        await self.live_view_url(session.id))
        except Exception as exc:
            logger.info("Session live view link not available: %r", exc)
        return BrowserHandle(session.id, page, browser, pw)

    async def _create_session(self, context_id: str, keep_alive: bool,
                              timeout_seconds: Optional[int], metadata: Optional[dict],
                              stealth: bool = False):
        settings = {
            "context": {"id": context_id, "persist": True},
            # Off by default: login sessions must not be recorded (plan 6.5).
            # BROWSERBASE_RECORD_SESSIONS=1 turns it on for local debugging.
            "record_session": self._record,
            # Client rule: Browserbase never solves a CAPTCHA for us (stealth or not).
            # Paused for development only (2026-11-05, captcha_solving_allowed).
            # Browserbase's free plan has no solver (pricing page, 2026-11-05):
            # the setting does nothing below the Developer plan.
            "solve_captchas": captcha_solving_allowed(),
        }
        extra = {}
        if stealth:
            # Vendor setting browser_stealth (AAR, testing 2026-11-03): a Browserbase
            # proxy only. Advanced stealth was refused on our plan 2026-11-03 (403
            # "Verified mode is only available on the Enterprise plan"), so it's left off.
            extra["proxies"] = True
        return await asyncio.to_thread(
            self._bb.sessions.create,
            project_id=self._project_id,
            keep_alive=keep_alive,
            api_timeout=timeout_seconds or self._session_timeout,
            browser_settings=settings,
            user_metadata=metadata or {},
            **extra,
        )

    async def live_view_url(self, session_id: str) -> str:
        live = await asyncio.to_thread(self._bb.sessions.debug, session_id)
        return live.debugger_fullscreen_url

    async def _release(self, session_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._bb.sessions.update, session_id,
                project_id=self._project_id, status="REQUEST_RELEASE",
            )
        except Exception as exc:  # already ended is fine
            logger.debug("Release of %s failed: %r", session_id, exc)

    async def close(self, handle: BrowserHandle, *, release: bool = True) -> None:
        """Disconnect Playwright and (by default) end the session so the Context is saved.

        `release=False` keeps a keep-alive session running, e.g. while a
        human completes 2FA through Live View.
        """
        try:
            await handle.browser.close()
        except Exception:
            pass
        try:
            await handle.playwright.stop()
        except Exception:
            pass
        if release:
            await self._release(handle.session_id)
