"""2FA handoff: a person finishes a login the worker can't (plan step 5).

When a portal asks for something only a person has (a code sent by email or
SMS, a push approval), the browser stays open and the requester gets a Google
Chat message:

  Live View (used whenever the browser has one: BROWSER_BACKEND=browserbase):
     the message links to the live browser; the person opens it and logs in
     there themselves. The worker watches the page and carries on once it is
     signed in. Works for any 2FA, not only codes. Chosen to try first
     (2026-10-22); the link controls the browser, so for real use it goes behind
     /hitl/view with a Google sign-in check (not built yet).
  Code form (fallback, e.g. the local browser): the message links to a small
     form on the API (GET/POST /hitl/code), or a form inside Chat when a Chat
     app is set up (CHAT_APP_SPACE, /chat/events; the Chat app comes later). The
     code lands in the store (`hitl/<id>`) and the worker types it in
     (adapter.submit_mfa_code). The token is random, used once and valid for
     the wait; the store keeps only a hash of it and drops the code once used.

Nobody in time: the rows go NEEDS_ATTENTION as before. A CAPTCHA or bot check
seen while waiting stops the login (client rule), even with a person there.

Logout handoff (team, 2026-11-02): a session left open blocks the team's own
sign-in elsewhere (Aviall). When the program's own logout can't be confirmed
twice, the third attempt is a person's: the requesters get the Live View link
and log out there; the program checks in a second tab until the session is
signed out (logout_with_handoff). Without Live View (local browser without
--live-view) it stays at the warning in the log.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from typing import Awaitable, Callable, Optional

from ..browser.base import LiveViewUnavailable
from ..credentials.base import VendorCredential
from ..vendors.base import LoginBlocked, MfaRequired, VendorAdapter

logger = logging.getLogger("sourcing.hitl")

CODE_WAIT_SECONDS = 600
# A mistyped code gets one more request; then the rows go NEEDS_ATTENTION.
CODE_REQUESTS = 2
POLL_SECONDS = 2
# How often the page is checked while a person logs in through Live View.
LIVE_POLL_SECONDS = 3
# Longest one page check may take before it is given up and tried again.
LIVE_CHECK_TIMEOUT = 15
# The program's own logout attempts before a person is asked to log out.
LOGOUT_TRIES = 2
# How often the session is checked while a person logs out (each check opens
# the site in a second tab, so not every few seconds).
LOGOUT_POLL_SECONDS = 15
LOGOUT_CHECK_TIMEOUT = 45
# Login codes seen: 6 digits (most), 6-8 letters/digits, Notion's "ABCD-EFGH"-like.
_CODE = re.compile(r"^[A-Za-z0-9-]{4,12}$")

# Request states: waiting -> code_given -> code_used -> signed_in | code_rejected;
# waiting -> expired.
WAITING, CODE_GIVEN, CODE_USED = "waiting", "code_given", "code_used"
SIGNED_IN, CODE_REJECTED, EXPIRED = "signed_in", "code_rejected", "expired"


def _page_url(page) -> str:
    try:
        return page.url.split("?")[0]
    except Exception:
        return "?"


def hitl_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def clean_code(raw: str) -> Optional[str]:
    code = re.sub(r"\s+", "", raw or "")
    return code if _CODE.match(code) else None


def code_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/hitl/code?t={token}"


def accept_code(store, token: str, raw_code: str, *, via: str) -> tuple[bool, str]:
    """API side: save a person's code for a waiting request. (accepted, message)."""
    doc = store.get_hitl(hitl_id(token)) if token else None
    if not doc:
        return False, "This link is not valid."
    if doc.get("state") != WAITING:
        return False, status_text(doc)
    if time.time() > float(doc.get("expires_at") or 0):
        return False, "This request has expired. A new one comes with the next run."
    code = clean_code(raw_code)
    if not code:
        return False, "That doesn't look like a login code (4 to 12 letters or digits)."
    store.update_hitl(hitl_id(token), {"state": CODE_GIVEN, "code": code,
                                       "code_at": time.time(), "via": via})
    logger.info("2FA code received for %s via %s", doc.get("account"), via)
    return True, f"Thanks. Typing the code into {doc.get('vendor_name')} now."


def status_text(doc: dict) -> str:
    """What the form page says about a request after the code was sent."""
    vendor = doc.get("vendor_name") or "the portal"
    return {
        WAITING: f"{vendor} is waiting for the code.",
        CODE_GIVEN: f"Code received; typing it into {vendor}...",
        CODE_USED: f"Code typed into {vendor}; checking the sign-in...",
        SIGNED_IN: f"Done: {vendor} accepted the code.",
        CODE_REJECTED: f"{vendor} did not accept the code. If it asks again, a new message "
                       "with a new link follows.",
        EXPIRED: "This request has expired. A new one comes with the next run.",
    }.get(doc.get("state"), "Unknown request.")


def code_card(token: str, *, vendor_name: str, method: str, form_url: str,
              minutes: int) -> dict:
    """The Chat-app message: a card with a text box and a Send button (needs a
    Chat app: a webhook can't receive the submit), plus the form link as a fallback."""
    return {"cardsV2": [{"cardId": f"hitl-{hitl_id(token)[:8]}", "card": {
        "header": {"title": f"{vendor_name} needs a login code",
                   "subtitle": f"Sent by {method}; valid {minutes} min"},
        "sections": [{"widgets": [
            {"textInput": {"name": "code", "label": "Login code", "type": "SINGLE_LINE"}},
            {"buttonList": {"buttons": [
                {"text": "Send code", "onClick": {"action": {
                    "function": "submit_code", "parameters": [{"key": "t", "value": token}]}}},
                {"text": "Open form", "onClick": {"openLink": {"url": form_url}}},
            ]}},
        ]}],
    }}]}


class PersonHandoff:
    """Asks a person to finish a login and waits (see the module docstring).

    base_url: where the API runs (PUBLIC_BASE_URL), for the form link.
    chat_app: optional sender with `async send_card(message: dict)` (Chat app)."""

    def __init__(self, store, notifier, base_url: str, *, chat_app=None,
                 wait_seconds: int = CODE_WAIT_SECONDS, poll_seconds: float = POLL_SECONDS) -> None:
        self.store = store
        self.notifier = notifier
        self.base_url = base_url
        self.chat_app = chat_app
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds

    async def ask(self, *, account: str, vendor_name: str, method: str,
                  people: tuple = (), attempt: int = 1) -> Optional[tuple[str, str]]:
        """Send the request and wait: (code, request id), or None when no code came."""
        token = secrets.token_urlsafe(24)
        hid = hitl_id(token)
        now = time.time()
        self.store.update_hitl(hid, {
            "state": WAITING, "account": account, "vendor_name": vendor_name,
            "method": method, "people": list(people), "attempt": attempt,
            "created_at": now, "expires_at": now + self.wait_seconds,
        })
        url = code_url(self.base_url, token)
        minutes = max(1, self.wait_seconds // 60)
        again = " The last code was not accepted; please send the new one." if attempt > 1 else ""
        text = (f"🔐 {vendor_name} is asking for a login code (sent by {method}).{again} "
                f"Enter it here within {minutes} min: {url}")
        for person in people or ("",):
            await self.notifier.send(text, to=person or None)
        if self.chat_app is not None:
            try:
                await self.chat_app.send_card(code_card(
                    token, vendor_name=vendor_name, method=method, form_url=url,
                    minutes=minutes))
            except Exception as exc:       # the link in the message still works
                logger.warning("Chat app card not sent: %r", exc)
        self.store.audit("hitl_code_requested", account_id=account, method=method,
                         attempt=attempt)
        logger.info("[%s] Waiting up to %d min for a person to send the %s code (%s)",
                    account, minutes, method, url.split("?")[0])

        deadline = now + self.wait_seconds
        while time.time() < deadline:
            doc = self.store.get_hitl(hid) or {}
            if doc.get("state") == CODE_GIVEN and doc.get("code"):
                self.store.update_hitl(hid, {"state": CODE_USED, "code": None})
                self.store.audit("hitl_code_received", account_id=account, via=doc.get("via"))
                return doc["code"], hid
            await asyncio.sleep(self.poll_seconds)
        self.store.update_hitl(hid, {"state": EXPIRED})
        self.store.audit("hitl_code_timeout", account_id=account, method=method)
        logger.warning("[%s] No code within %d min", account, minutes)
        return None

    def finish(self, hid: str, accepted: bool) -> None:
        self.store.update_hitl(hid, {"state": SIGNED_IN if accepted else CODE_REJECTED,
                                     "finished_at": time.time()})

    async def watch_live_view(self, adapter: VendorAdapter, page, url: str, *, account: str,
                              method: str, people: tuple = ()) -> bool:
        """Send the Live View link and wait until the person has logged in on the
        open page. False when the wait ran out."""
        hid = secrets.token_hex(16)
        now = time.time()
        minutes = max(1, self.wait_seconds // 60)
        self.store.update_hitl(hid, {
            "state": WAITING, "mode": "live_view", "account": account,
            "vendor_name": adapter.display_name, "method": method, "people": list(people),
            "created_at": now, "expires_at": now + self.wait_seconds,
        })
        text = (f"🔐 {adapter.display_name} needs you to finish the login (it asks for a "
                f"{method} code or approval). Open the live browser and log in there: {url} "
                f"The run waits {minutes} min. The link controls that browser: don't share it.")
        for person in people or ("",):
            await self.notifier.send(text, to=person or None)
        self.store.audit("hitl_live_view_sent", account_id=account, method=method)
        logger.info("[%s] Waiting up to %d min for a person to log in through Live View",
                    account, minutes)

        deadline = now + self.wait_seconds
        last_note = now
        while time.time() < deadline:
            await asyncio.sleep(LIVE_POLL_SECONDS)
            # Bounded: a page call that never returns (seen with Browserbase,
            # 2026-10-22) must not outlast the wait.
            try:
                done = await asyncio.wait_for(adapter.person_finished_login(page),
                                              LIVE_CHECK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[%s] Live View check took over %ds; trying again", account,
                               LIVE_CHECK_TIMEOUT)
                done = False
            if not done and time.time() - last_note >= 30:
                last_note = time.time()
                logger.info("[%s] Still waiting for the person (page at %s, %d s left)",
                            account, _page_url(page), int(deadline - time.time()))
            if done:
                self.store.update_hitl(hid, {"state": SIGNED_IN, "finished_at": time.time()})
                self.store.audit("hitl_live_view_done", account_id=account)
                logger.info("[%s] The person finished the login (now at %s)", account,
                            page.url.split("?")[0])
                for person in people or ("",):
                    await self.notifier.send(f"Thanks: {adapter.display_name} is signed in; "
                                             "the run carries on.", to=person or None)
                return True
        self.store.update_hitl(hid, {"state": EXPIRED})
        self.store.audit("hitl_live_view_timeout", account_id=account, method=method)
        logger.warning("[%s] Nobody finished the login within %d min", account, minutes)
        return False


    async def watch_logout(self, adapter: VendorAdapter, page, url: str, *, account: str,
                           people: tuple = ()) -> bool:
        """Send the Live View link and wait until the session is signed out.
        False when the wait ran out or a bot check showed up."""
        hid = secrets.token_hex(16)
        now = time.time()
        minutes = max(1, self.wait_seconds // 60)
        self.store.update_hitl(hid, {
            "state": WAITING, "mode": "logout", "account": account,
            "vendor_name": adapter.display_name, "people": list(people),
            "created_at": now, "expires_at": now + self.wait_seconds,
        })
        name = adapter.display_name
        text = (f"🔓 The search on {name} is finished, but the program couldn't confirm it "
                f"logged out. While it stays signed in, nobody can log in to {name} from "
                f"another device. Please open the live browser and log out there: {url} "
                f"The browser stays open {minutes} min. The link controls that browser: "
                "don't share it.")
        for person in people or ("",):
            await self.notifier.send(text, to=person or None)
        self.store.audit("hitl_logout_sent", account_id=account)
        logger.info("[%s] Waiting up to %d min for a person to log out through Live View",
                    account, minutes)

        deadline = now + self.wait_seconds
        while time.time() < deadline:
            await asyncio.sleep(LOGOUT_POLL_SECONDS)
            try:
                done = await asyncio.wait_for(adapter.person_finished_logout(page),
                                              LOGOUT_CHECK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[%s] Logout check took over %ds; trying again", account,
                               LOGOUT_CHECK_TIMEOUT)
                done = False
            except LoginBlocked as exc:
                # Client rule: touch nothing more; a person looks at it.
                self.store.update_hitl(hid, {"state": EXPIRED})
                self.store.audit("hitl_logout_blocked", account_id=account, reason=str(exc)[:200])
                logger.warning("[%s] Bot check while waiting for the logout: %s", account, exc)
                return False
            if done:
                self.store.update_hitl(hid, {"state": "signed_out", "finished_at": time.time()})
                self.store.audit("hitl_logout_done", account_id=account)
                logger.info("[%s] The person logged out of %s", account, name)
                for person in people or ("",):
                    await self.notifier.send(f"Thanks: {name} is logged out; others can sign "
                                             "in again.", to=person or None)
                return True
        self.store.update_hitl(hid, {"state": EXPIRED})
        self.store.audit("hitl_logout_timeout", account_id=account)
        logger.warning("[%s] Nobody logged out of %s within %d min", account, name, minutes)
        for person in people or ("",):
            await self.notifier.send(
                f"⚠️ {name} may still be signed in (no logout within {minutes} min). If "
                f"someone can't log in to {name}, log out there by hand or wait for the "
                "portal to end the session.", to=person or None)
        return False


async def logout_with_handoff(adapter: VendorAdapter, page, handoff: Optional[PersonHandoff],
                              *, account: str, people: tuple = (),
                              live_view_url: Optional[Callable[[], Awaitable[str]]] = None,
                              tries: int = LOGOUT_TRIES) -> bool:
    """adapter.logout up to `tries` times; still not confirmed, a person logs
    out through Live View (the third attempt). True when signed out."""
    for attempt in range(1, tries + 1):
        try:
            if await adapter.logout(page):
                return True
            logger.warning("[%s] Logout attempt %d of %d not confirmed", account, attempt, tries)
        except Exception as exc:
            logger.warning("[%s] Logout attempt %d of %d failed: %r", account, attempt, tries,
                           exc)
    if handoff is None or live_view_url is None or not adapter.accepts_person_logout:
        return False
    try:
        url = await live_view_url()
    except LiveViewUnavailable as exc:
        logger.warning("[%s] No Live View to ask a person to log out: %s", account, exc)
        return False
    except Exception as exc:
        logger.warning("[%s] Live View link not available: %r", account, exc)
        return False
    if not url:
        return False
    return await handoff.watch_logout(adapter, page, url, account=account, people=people)


# While a session that may be signed in waits for a person to log out: how often
# the sign-out is checked, and every how many checks the program tries the
# logout itself again when nobody can reach the browser (headless, no live view).
HOLD_POLL_SECONDS = 15
HOLD_RETRY_EVERY = 4

async def hold_until_logged_out(account: str, adapter: VendorAdapter, handle,
                                 browser) -> bool:
    """The logout isn't confirmed: the browser is never closed on a session that
    may be signed in (team rule, 2026-11-04: an AAR login at 11:01 was left open
    when the local browser closed). Waits, with no time limit, until the portal
    says signed out: a person logs out in the shown window or the live view, or,
    when nobody can reach the browser, the program tries the logout again.
    Onboarding always; the worker only on a browser that `waits_for_logout`
    (local: a Cloud Run job can't wait forever, Browserbase keeps the 10-minute
    Live View handoff)."""
    page = handle.page
    link = None
    try:
        link = await browser.live_view_url(handle.session_id)
    except Exception:
        pass
    window = bool(getattr(browser, "shows_window", False))
    reachable = window or bool(link)
    where = ("in the open browser window" if window else
             f"through the live view: {link}" if link else
             "nowhere (headless, no live view): the program tries the logout again")
    logger.warning("[%s] LOGOUT NOT CONFIRMED: the browser stays open until the "
                   "session is signed out. Log out %s. (Ctrl+C closes it signed in.)",
                   account, where)
    checks = 0
    while True:
        await asyncio.sleep(HOLD_POLL_SECONDS)
        checks += 1
        try:
            if not reachable and checks % HOLD_RETRY_EVERY == 0:
                if await adapter.logout(page):
                    logger.info("[%s] Logout confirmed on a later try", account)
                    return True
            elif await adapter.person_finished_logout(page):
                logger.info("[%s] Signed out; closing the browser", account)
                return True
        except LoginBlocked as exc:
            logger.warning("[%s] Bot check while waiting for the logout: %s; "
                           "still waiting", account, exc)
        except Exception as exc:
            logger.info("[%s] Sign-out check failed: %r; still waiting",
                        account, exc)
        if checks % 8 == 0:
            logger.warning("[%s] Still signed in, still waiting (log out %s)",
                           account, where)



class CodeNotGiven(MfaRequired):
    """The portal wanted a code and none came back from a person in time."""

    def __init__(self, exc: MfaRequired, asked: bool) -> None:
        super().__init__(exc.method, str(exc), exc.input_selector)
        self.asked = asked


async def login_with_handoff(adapter: VendorAdapter, page, cred: VendorCredential,
                             handoff: Optional[PersonHandoff], *, account: str,
                             people: tuple = (),
                             live_view_url: Optional[Callable[[], Awaitable[str]]] = None
                             ) -> None:
    """adapter.login, and when the portal asks for 2FA a person must answer, the
    handoff above: Live View when `live_view_url` gives a link, else the code
    form. Raises like adapter.login; a 2FA step nobody finished raises
    CodeNotGiven (asked=False when no handoff was possible)."""
    try:
        await adapter.login(page, cred)
        return
    except MfaRequired as exc:
        pending = exc
    if handoff is not None and live_view_url is not None and adapter.accepts_person_login:
        try:
            url = await live_view_url()
        except LiveViewUnavailable:
            url = None                     # local browser: the code form below
        if url:
            if await handoff.watch_live_view(adapter, page, url, account=account,
                                             method=pending.method, people=people):
                return
            raise CodeNotGiven(pending, asked=True)
    sent_by = pending.method          # "email" / "sms", also in the second request
    for attempt in range(1, CODE_REQUESTS + 1):
        if (handoff is None or not handoff.base_url or not adapter.accepts_mfa_codes
                or not pending.input_selector):
            raise CodeNotGiven(pending, asked=attempt > 1)
        got = await handoff.ask(account=account, vendor_name=adapter.display_name,
                                method=sent_by, people=people, attempt=attempt)
        if got is None:
            raise CodeNotGiven(pending, asked=True)
        code, hid = got
        try:
            await adapter.submit_mfa_code(page, cred, code, pending.input_selector)
        except MfaRequired as again:
            handoff.finish(hid, accepted=False)
            pending = again
            continue
        except BaseException:
            handoff.finish(hid, accepted=False)
            raise
        handoff.finish(hid, accepted=True)
        return
    raise CodeNotGiven(pending, asked=True)
