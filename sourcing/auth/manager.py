"""Login ladder and account state machine (plan section 6.1).

  0. Reuse the saved Browserbase Context for (vendor, user) -> auth probe
  1. Automatic login: username/password + TOTP from the vault
  2. Human handoff to the requester over Google Chat (hitl.py), when the run has
     one: a Live View link to finish the login in (Browserbase), else a code form
  3. Stop safely: wrong password -> NEEDS_CREDENTIAL_UPDATE,
                  lockout/block/too many failures -> SUSPENDED

At most `max_login_attempts` automatic logins per account per run, counting
re-logins after a mid-run expiry. A second attempt is made only after a failure
the portal didn't judge (timeout, server error, nothing sent yet: LoginFailed
with retryable=True). Wrong password, lockout / bot check and an unexplained
rejection stop at the first attempt (asked 2026-10-21: a portal may lock the
account after one failed login).

Before every attempt the vendor's login limits are checked (sourcing/limits.py):
failed logins the vendor saw in its lockout window, a cooldown after a failure,
and logins per day. Attempts and failures are saved on the account record, so
the limits hold across runs.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..browser.base import BrowserHandle, ContextNotFound
from ..credentials.base import VendorCredential
from ..limits import (
    LOGIN_OK_FIELDS, login_attempt_fields, login_block, login_failure_fields,
)
from ..models import BLOCKED_ACCOUNT_STATES, AccountState, account_id
from ..store import Store
from .hitl import (PersonHandoff, CodeNotGiven, hold_until_logged_out, login_with_handoff,
                   logout_with_handoff)
from ..vendors.base import (
    CaptchaFailed, InvalidCredentials, LoginBlocked, LoginFailed, MfaRequired, VendorAdapter,
    may_be_signed_in,
)

logger = logging.getLogger("sourcing.auth")


def _ago(ts: float) -> str:
    minutes = int((time.time() - float(ts)) // 60)
    return f"{minutes // 60}h {minutes % 60}m ago" if minutes >= 60 else f"{minutes}m ago"


class CannotLogin(Exception):
    """The account can't be used this run. `state` is the account's new state."""

    def __init__(self, state: AccountState, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


class LoginDeferred(Exception):
    """A login limit says wait (cooldown, daily cap): the rows wait for a later
    run, the account state is unchanged."""


class NeedsHuman(Exception):
    """A 2FA step only a person can complete, and no code came from one.
    asked: whether a person was asked for the code (and didn't send one in time)."""

    def __init__(self, method: str, asked: bool = False) -> None:
        super().__init__(f"2FA needs a person ({method})")
        self.method = method
        self.asked = asked


class AuthManager:
    def __init__(self, store: Store, browser, max_login_attempts: int = 2,
                 handoff: Optional[PersonHandoff] = None) -> None:
        self.store = store
        self.browser = browser
        self.max_login_attempts = max_login_attempts
        self.handoff = handoff
        self._attempts: dict[str, int] = {}
        # Browser sessions where a login attempt failed but the portal may have
        # signed us in anyway (may_be_signed_in): logged out before closing.
        self._maybe_signed_in: set[str] = set()

    async def open_authenticated(self, adapter: VendorAdapter, cred: VendorCredential,
                                 people: tuple = ()) -> BrowserHandle:
        """people: who to ask for a 2FA code (the rows' requesters)."""
        aid = account_id(cred.vendor_id, cred.owner)
        doc = self.store.get_account(aid) or {}
        state = doc.get("state")
        if state and AccountState(state) in BLOCKED_ACCOUNT_STATES:
            raise CannotLogin(AccountState(state), f"Account {aid} is {state}")

        context_id = doc.get("context_id")
        if context_id:
            logger.info("[%s] Reusing saved browser Context %s (state=%s)", aid, context_id,
                        state or "unknown")
        else:
            context_id = await self._new_context(aid, cred, doc, "first login for this account")

        try:
            handle = await self.browser.open(context_id, metadata={"account": aid},
                                              **getattr(adapter, "browser_options", {}))
        except ContextNotFound:
            # Saved Context is gone: deleted, or the Browserbase key/project now
            # points at another account. Start fresh: new Context, new login.
            logger.warning("[%s] Saved Context %s doesn't exist in this Browserbase project; "
                           "starting a fresh one", aid, context_id)
            context_id = await self._new_context(aid, cred, doc, "saved one not found")
            handle = await self.browser.open(context_id, metadata={"account": aid},
                                              **getattr(adapter, "browser_options", {}))
        try:
            # A Context can only hold a sign-in if a login has succeeded in it. Until
            # then, log in directly: an auth probe "passing" on a Context that never
            # logged in means the probe is wrong, and trusting it would search
            # logged out (every part would come back as a false NOT_FOUND).
            if not doc.get("last_login_at"):
                logger.info("[%s] Context %s has no active sign-in (%s); logging in", aid,
                            context_id,
                            f"signed out at the end of the run {_ago(doc['last_logout_at'])}"
                            if doc.get("last_logout_at") else "never signed in")
                await self.login(handle, adapter, cred, people)
                return handle
            if await adapter.is_authenticated(handle.page):  # step 0
                logger.info("[%s] Saved Context still signed in (last login %s); skipping login",
                            aid, _ago(doc["last_login_at"]))
                self.store.update_account(aid, {"state": AccountState.ACTIVE.value,
                                                "last_validated_at": time.time()})
                return handle
            logger.info("[%s] Saved Context is no longer signed in (last login %s); logging in",
                        aid, _ago(doc["last_login_at"]))
            await self.login(handle, adapter, cred, people)  # step 1
            return handle
        except BaseException:
            await self.logout_if_maybe_signed_in(adapter, handle, aid, people)
            await self.browser.close(handle)
            raise

    def maybe_signed_in(self, handle: BrowserHandle) -> bool:
        return handle.session_id in self._maybe_signed_in

    async def logout_if_maybe_signed_in(self, adapter: VendorAdapter, handle: BrowserHandle,
                                        aid: str, people: tuple = ()) -> None:
        """A failed login that may still have opened a vendor session: sign out
        (two tries, then a person through Live View, as after a run)."""
        if handle.session_id not in self._maybe_signed_in:
            return
        self._maybe_signed_in.discard(handle.session_id)
        try:
            ok = await logout_with_handoff(
                adapter, handle.page, self.handoff, account=aid, people=people,
                live_view_url=lambda: self.browser.live_view_url(handle.session_id))
        except Exception as exc:
            logger.warning("[%s] Logout after the failed login failed: %r", aid, exc)
            ok = False
        if not ok and getattr(self.browser, "waits_for_logout", False):
            # Never close a local browser signed in (team rule, 2026-11-04).
            ok = await hold_until_logged_out(aid, adapter, handle, self.browser)
        self.store.audit("logout_after_failed_login", account_id=aid, ok=ok)
        logger.info("[%s] Logout after the failed login: %s", aid,
                    "signed out" if ok else "not confirmed (the portal may keep the session "
                    "until it expires)")

    async def _new_context(self, aid: str, cred: VendorCredential, doc: dict, why: str) -> str:
        """Create a Context for the account and save it. A new Context holds no
        sign-in, so the login markers are cleared too (`doc` is updated in place)."""
        context_id = await self.browser.create_context()
        logger.info("[%s] Created browser Context %s (%s)", aid, context_id, why)
        fields = {
            "vendor_id": cred.vendor_id, "user_email": cred.owner,
            "secret_key": cred.secret_key, "context_id": context_id,
            "state": AccountState.NEEDS_LOGIN.value, "consecutive_login_failures": 0,
            "last_login_at": None, "last_logout_at": None,
        }
        self.store.update_account(aid, fields)
        doc.update(fields)
        return context_id

    async def login(self, handle: BrowserHandle, adapter: VendorAdapter,
                    cred: VendorCredential, people: tuple = ()) -> None:
        aid = account_id(cred.vendor_id, cred.owner)
        limits = adapter.limits
        max_attempts = limits["max_login_attempts_per_run"] or self.max_login_attempts
        last_reason = "login did not run"
        captcha = False
        while self._attempts.get(aid, 0) < max_attempts:
            doc = self.store.get_account(aid) or {}
            reason, needs_person = login_block(doc, limits, time.time(),
                                               retry_in_run=self._attempts.get(aid, 0) > 0)
            if reason:
                self.store.audit("login_held", account_id=aid, reason=reason)
                if needs_person:
                    self._stop(aid, AccountState.NEEDS_ATTENTION, reason)
                logger.info("[%s] Login not attempted: %s", aid, reason)
                raise LoginDeferred(reason)
            self._attempts[aid] = self._attempts.get(aid, 0) + 1
            attempt = self._attempts[aid]
            logger.info("[%s] Automatic login attempt %d/%d", aid, attempt, max_attempts)
            self.store.update_account(aid, login_attempt_fields(doc, limits, time.time()))
            try:
                await self._attempt(adapter, handle, cred, aid, people)
            except InvalidCredentials as exc:
                self._failed(aid, limits, str(exc))
                self._stop(aid, AccountState.NEEDS_CREDENTIAL_UPDATE, str(exc))
            except LoginBlocked as exc:
                self._failed(aid, limits, str(exc))
                self._stop(aid, AccountState.SUSPENDED, str(exc))
            except MfaRequired as exc:
                asked = isinstance(exc, CodeNotGiven) and exc.asked
                self.store.audit("login_needs_human", account_id=aid, method=exc.method,
                                 asked=asked)
                raise NeedsHuman(exc.method, asked) from exc
            except LoginFailed as exc:
                last_reason = str(exc)
                captcha = isinstance(exc, CaptchaFailed)
                logger.warning("[%s] Login attempt %d failed: %s", aid, attempt, last_reason)
                if exc.credentials_sent:
                    self._failed(aid, limits, last_reason)
                if not exc.retryable:
                    self._stop(aid, AccountState.NEEDS_ATTENTION, last_reason)
                continue

            proof = getattr(adapter, "signed_in_proof", None)
            if proof or await adapter.is_authenticated(handle.page):
                if proof:
                    logger.info("[%s] Sign-in seen by the login (%s); no separate probe",
                                aid, proof)
                self._maybe_signed_in.discard(handle.session_id)
                now = time.time()
                self.store.update_account(aid, {
                    "state": AccountState.ACTIVE.value, "last_login_at": now,
                    "last_validated_at": now, "consecutive_login_failures": 0,
                    **LOGIN_OK_FIELDS,
                })
                self.store.audit("login_ok", account_id=aid, method="auto")
                logger.info("[%s] Login OK", aid)
                return
            if getattr(adapter, "login_claimed", False):
                # The login read its last page as signed in; only the probe can't
                # prove it. Logging in again over a live session is what suspended
                # Monroe (2026-11-04), so search instead: a search that meets a sign-in
                # wall raises SessionExpired (one re-login), offers confirm it, and an
                # empty result is still never NOT_FOUND without the probe's proof.
                logger.warning("[%s] Login attempt %d: the login says signed in but the auth "
                               "probe can't confirm it; searching once to find out",
                               aid, attempt)
                self._maybe_signed_in.add(handle.session_id)
                self.store.update_account(aid, {
                    "state": AccountState.ACTIVE.value, "last_login_at": time.time(),
                    "consecutive_login_failures": 0, **LOGIN_OK_FIELDS,
                })
                self.store.audit("login_unconfirmed", account_id=aid, method="auto")
                return
            last_reason = "auth probe failed right after login"
            captcha = False
            self._maybe_signed_in.add(handle.session_id)
            logger.warning("[%s] Login attempt %d: %s", aid, attempt, last_reason)

        if captcha:
            # For now a CAPTCHA isn't a stop (2026-11-03): the account stays
            # NEEDS_LOGIN, so the next run logs in again.
            self._stop(aid, AccountState.NEEDS_LOGIN,
                       f"CAPTCHA on all {max_attempts} automatic logins: {last_reason}; "
                       "the next run tries again")
        doc = self.store.get_account(aid) or {}
        self.store.update_account(aid, {
            "consecutive_login_failures": int(doc.get("consecutive_login_failures") or 0) + 1,
        })
        self._stop(aid, AccountState.SUSPENDED,
                   f"Automatic login failed {max_attempts} times: {last_reason}")

    async def _attempt(self, adapter: VendorAdapter, handle: BrowserHandle,
                       cred: VendorCredential, aid: str, people: tuple) -> None:
        try:
            await login_with_handoff(
                adapter, handle.page, cred, self.handoff, account=aid, people=people,
                live_view_url=lambda: self.browser.live_view_url(handle.session_id))
        except BaseException as exc:
            if may_be_signed_in(exc):
                self._maybe_signed_in.add(handle.session_id)
            else:
                # A bot check now means: touch nothing, even after an earlier attempt.
                self._maybe_signed_in.discard(handle.session_id)
            raise

    def _failed(self, aid: str, limits: dict, reason: str) -> None:
        """A failed login the vendor saw: counts towards its lockout window."""
        doc = self.store.get_account(aid) or {}
        self.store.update_account(aid, login_failure_fields(doc, limits, time.time(), reason))

    def _stop(self, aid: str, state: AccountState, reason: str) -> None:
        logger.warning("[%s] Stopping login: account -> %s (%s)", aid, state.value, reason)
        self.store.update_account(aid, {"state": state.value, "last_error": reason[:300]})
        self.store.audit("login_stopped", account_id=aid, state=state.value, reason=reason[:300])
        raise CannotLogin(state, reason)
