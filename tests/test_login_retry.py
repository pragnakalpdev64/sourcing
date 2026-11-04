"""Login ladder: a second attempt only after a failure the portal didn't judge
(asked 2026-10-21: Jamaica Bearings locks the account after one failed login)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sourcing.auth.manager import AuthManager, CannotLogin, LoginDeferred
from sourcing.credentials.base import VendorCredential
from sourcing.limits import effective_limits
from sourcing.models import AccountState
from sourcing.store import MemoryStore
from sourcing.vendors.base import CaptchaFailed, InvalidCredentials, LoginBlocked, LoginFailed


class _Adapter:
    """Each login() raises the next prepared outcome; None = signed in."""

    def __init__(self, *outcomes, limits=None):
        self.outcomes = list(outcomes)
        self.logins = 0
        self.signed_in = False
        self.limits = effective_limits({"limits": limits or {}})[0]

    async def login(self, page, cred):
        self.logins += 1
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome
        self.signed_in = True

    async def is_authenticated(self, page):
        self.probes = getattr(self, "probes", 0) + 1
        return self.signed_in


CRED = VendorCredential("jamaica_bearings", "_shared", "sourcing-vendor-jamaica-bearings",
                        "user", "pw")
AID = "jamaica_bearings___shared"


async def _login(adapter, store=None):
    store = store or MemoryStore()
    try:
        await AuthManager(store, browser=None, max_login_attempts=2).login(
            SimpleNamespace(page=None, session_id="s-1"), adapter, CRED)
        return None, store
    except CannotLogin as exc:
        return exc, store


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    LoginFailed("Timed out at https://portal/login"),
    LoginFailed("Server error after the login submit ('service unavailable')"),
])
async def test_a_failure_the_portal_did_not_judge_gets_one_more_attempt(first):
    adapter = _Adapter(first, None)
    error, store = await _login(adapter)
    assert error is None and adapter.logins == 2
    assert store.get_account(AID)["state"] == AccountState.ACTIVE.value


@pytest.mark.asyncio
async def test_a_captcha_is_tried_again_and_not_counted_as_a_failed_login():
    # For now (2026-11-03) a CAPTCHA is retried, not a stop (AAR: "reCAPTCHA
    # verification failed.").
    adapter = _Adapter(CaptchaFailed("Portal shows a CAPTCHA ('captcha')"), None)
    error, store = await _login(adapter)
    assert error is None and adapter.logins == 2
    assert not store.get_account(AID).get("login_failures")


@pytest.mark.asyncio
async def test_a_captcha_on_every_attempt_leaves_the_account_for_the_next_run():
    adapter = _Adapter(CaptchaFailed("Portal shows a CAPTCHA ('captcha')"),
                       CaptchaFailed("Portal shows a CAPTCHA ('captcha')"))
    error, store = await _login(adapter)
    assert adapter.logins == 2 and error.state == AccountState.NEEDS_LOGIN
    doc = store.get_account(AID)
    assert doc["state"] == AccountState.NEEDS_LOGIN.value and not doc.get("login_failures")
    # The next run logs in again.
    adapter = _Adapter(None)
    error, store = await _login(adapter, store)
    assert error is None and adapter.logins == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first, state", [
    (InvalidCredentials("Portal rejected the username or password"),
     AccountState.NEEDS_CREDENTIAL_UPDATE),
    (LoginBlocked("Portal reports a lockout or block ('too many requests')"),
     AccountState.SUSPENDED),
    # Still on the form with no message: maybe a rejection in words we don't know.
    (LoginFailed("Still on the login page after submit", retryable=False),
     AccountState.NEEDS_ATTENTION),
])
async def test_a_rejection_or_security_flag_stops_at_the_first_attempt(first, state):
    adapter = _Adapter(first, None)
    error, store = await _login(adapter)
    assert adapter.logins == 1
    assert error is not None and error.state == state
    assert store.get_account(AID)["state"] == state.value


@pytest.mark.asyncio
async def test_two_retryable_failures_stop_after_two_attempts():
    adapter = _Adapter(LoginFailed("Timed out"), LoginFailed("Timed out"))
    error, _ = await _login(adapter)
    assert adapter.logins == 2 and error.state == AccountState.SUSPENDED


# --- per-vendor login limits (sourcing/limits.py) ---------------------------

@pytest.mark.asyncio
async def test_a_vendor_that_locks_after_one_failure_gets_no_second_attempt():
    # Jamaica Bearings: a timeout after the submit may have counted as a failed login.
    adapter = _Adapter(LoginFailed("Timed out after submit"), None,
                       limits={"vendor_lockout_after": 1, "login_safety_margin": 0})
    error, store = await _login(adapter)
    assert adapter.logins == 1 and error.state == AccountState.NEEDS_ATTENTION
    assert "locks after 1" in error.reason
    assert len(store.get_account(AID)["login_failures"]) == 1


@pytest.mark.asyncio
async def test_a_failure_before_the_credentials_were_sent_is_not_counted():
    adapter = _Adapter(LoginFailed("No login form found", credentials_sent=False), None,
                       limits={"vendor_lockout_after": 1, "login_safety_margin": 0})
    error, store = await _login(adapter)
    assert error is None and adapter.logins == 2
    assert store.get_account(AID)["login_failures"] == []        # cleared by the success


@pytest.mark.asyncio
async def test_the_cooldown_after_a_failed_login_holds_the_next_run():
    import time

    store = MemoryStore()
    store.update_account(AID, {"login_failures": [{"at": time.time() - 600, "reason": "x"}],
                               "last_failure_at": time.time() - 600})
    adapter = _Adapter(None)                                   # would succeed
    with pytest.raises(LoginDeferred, match="next attempt allowed in"):
        await AuthManager(store, browser=None).login(SimpleNamespace(page=None), adapter, CRED)
    assert adapter.logins == 0                                 # nothing sent to the portal


@pytest.mark.asyncio
async def test_the_daily_login_limit_holds_further_logins():
    import time

    from sourcing.limits import day_key

    adapter = _Adapter(None, limits={"max_logins_per_day": 2})
    store = MemoryStore()
    store.update_account(AID, {"logins_day": day_key(time.time(), adapter.limits),
                               "logins_today": 2})
    with pytest.raises(LoginDeferred, match="daily login limit"):
        await AuthManager(store, browser=None).login(SimpleNamespace(page=None), adapter, CRED)
    assert adapter.logins == 0


@pytest.mark.asyncio
async def test_the_failure_budget_counts_failures_from_earlier_runs():
    import time

    store = MemoryStore()
    # 2 failures today; default lockout 3 with a margin of 1 -> no third try.
    store.update_account(AID, {"login_failures": [{"at": time.time() - 7200, "reason": "x"},
                                                  {"at": time.time() - 3700, "reason": "y"}],
                               "last_failure_at": time.time() - 3700})
    adapter = _Adapter(None)
    error, _ = await _login(adapter, store)
    assert adapter.logins == 0 and error.state == AccountState.NEEDS_ATTENTION


# -- log out after a login that may have gone through (DASI 2026-10-28) -------------

from sourcing.models import LlmUsage  # noqa: E402
from sourcing.onboarding import onboard  # noqa: E402
from sourcing.vendors.base import may_be_signed_in  # noqa: E402

UNCLEAR = "Couldn't tell whether the sign-in worked at https://portal/my-orders"


class _Browser:
    def __init__(self):
        self.closed = 0

    async def create_context(self):
        return "ctx-1"

    async def open(self, context_id, metadata=None):
        return SimpleNamespace(page=None, session_id="s-1")

    async def close(self, handle, **kwargs):
        self.closed += 1

    def live_view_url(self, session_id):
        return None


class _LoggingOutAdapter(_Adapter):
    def __init__(self, *outcomes, probe_ok=True):
        super().__init__(*outcomes)
        self.probe_ok = probe_ok
        self.logouts = 0
        self.config, self.learned, self.found_by = {}, {}, {}
        self.usage, self.discovery_calls = LlmUsage(), 0

    async def is_authenticated(self, page):
        return self.signed_in and self.probe_ok

    async def logout(self, page):
        self.logouts += 1
        return True

    def confirmed_learned(self):
        return {}


def test_which_failed_logins_may_have_signed_in():
    assert may_be_signed_in(LoginFailed(UNCLEAR, retryable=False))
    assert may_be_signed_in(RuntimeError("page crashed after submit"))
    assert not may_be_signed_in(LoginFailed("no password box", credentials_sent=False))
    assert not may_be_signed_in(LoginBlocked("CAPTCHA"))
    assert not may_be_signed_in(InvalidCredentials("wrong password"))


async def _open(adapter):
    browser, store = _Browser(), MemoryStore()
    manager = AuthManager(store, browser, max_login_attempts=2)
    with pytest.raises(CannotLogin):
        await manager.open_authenticated(adapter, CRED)
    return browser, store


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter, logouts", [
    (_LoggingOutAdapter(LoginFailed(UNCLEAR, retryable=False)), 1),
    (_LoggingOutAdapter(None, None, probe_ok=False), 1),         # signed in, probe disagrees
    (_LoggingOutAdapter(LoginBlocked("CAPTCHA on the login page")), 0),   # touch nothing
    (_LoggingOutAdapter(LoginFailed("timeout"), LoginBlocked("bot check")), 0),
    (_LoggingOutAdapter(InvalidCredentials("wrong password")), 0),
    (_LoggingOutAdapter(LoginFailed("no password box", retryable=False,
                                    credentials_sent=False)), 0),
])
async def test_worker_logs_out_after_a_login_that_may_have_gone_through(adapter, logouts):
    browser, store = await _open(adapter)
    assert adapter.logouts == logouts and browser.closed == 1
    events = [e["event"] for e in store.audit_log]
    assert ("logout_after_failed_login" in events) == bool(logouts)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, logouts", [
    (LoginFailed(UNCLEAR, retryable=False), 1),
    (LoginBlocked("CAPTCHA on the login page"), 0),
])
async def test_onboarding_logs_out_after_an_unclear_login(outcome, logouts):
    adapter, browser = _LoggingOutAdapter(outcome), _Browser()
    report = await onboard("dasi", adapter, CRED, browser, [])
    assert report.stopped and adapter.logouts == logouts and browser.closed == 1
    assert report.logged_out is (True if logouts else None)


@pytest.mark.asyncio
async def test_a_sign_in_the_login_already_saw_is_not_probed_again():
    adapter = _Adapter(None)
    adapter.signed_in_proof = "a:has-text('Log out') at https://portal/home"
    error, store = await _login(adapter)
    assert error is None and getattr(adapter, "probes", 0) == 0
    assert store.get_account(AID)["state"] == AccountState.ACTIVE.value

    unproven = _Adapter(None)                     # no proof: the probe still runs
    assert (await _login(unproven))[0] is None and unproven.probes == 1


class _ClaimingAdapter(_Adapter):
    """The login reads its last page as signed in; the probe can't prove it."""

    def __init__(self, claimed=True):
        super().__init__(None, None)
        self.claimed = claimed

    async def login(self, page, cred):
        await super().login(page, cred)
        self.login_claimed = self.claimed

    async def is_authenticated(self, page):
        self.probes = getattr(self, "probes", 0) + 1
        return False


@pytest.mark.asyncio
async def test_a_login_that_says_signed_in_is_searched_not_tried_again():
    # Monroe 2026-11-04: signed in (LLM read the orders page), the probe said no,
    # the second login over the live session failed and suspended the account.
    adapter = _ClaimingAdapter()
    error, store = await _login(adapter)
    assert error is None and adapter.logins == 1
    assert store.get_account(AID)["state"] == AccountState.ACTIVE.value
    assert store.audit_log[-1]["event"] == "login_unconfirmed"


@pytest.mark.asyncio
async def test_a_login_without_a_signed_in_reading_still_needs_the_probe():
    adapter = _ClaimingAdapter(claimed=False)
    error, _ = await _login(adapter)
    assert error is not None and adapter.logins == 2
