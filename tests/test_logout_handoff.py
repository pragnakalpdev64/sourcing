"""Logout handoff (sourcing/auth/hitl.py, team 2026-11-02): when the program
can't confirm its own logout twice, the requesters get the Live View link and
log out there; the program checks in a second tab. Uses the Aviall mock portal
(Log in / Log out in the avatar menu). Skipped if no local Chromium."""
from __future__ import annotations

import asyncio

import pytest

import sourcing.auth.hitl as hitl
from sourcing.auth.hitl import PersonHandoff, logout_with_handoff
from sourcing.browser.base import LiveViewUnavailable
from sourcing.credentials.base import MemoryCredentialProvider, secret_key
from sourcing.store import MemoryStore
from sourcing.vendors.aviall import AviallAdapter

from .fakes import FakeAdapter, FakeBrowser, FakeNotifier
from .test_aviall_adapter import HOST, MockAviall, adapter, cred, page_for  # noqa: F401
from .test_pipeline import JANE, make, run

LIVE = "https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=abc"


async def live_url():
    return LIVE


class StuckLogout(AviallAdapter):
    """Its own logout never confirms (e.g. a pop-up over the avatar menu)."""
    tries = 0

    async def logout(self, page) -> bool:
        StuckLogout.tries += 1
        return False


class Person(FakeNotifier):
    """Logs out in the live browser shortly after the link arrives (logs_out=False: never)."""

    def __init__(self, portal: MockAviall, logs_out: bool = True) -> None:
        super().__init__()
        self.portal, self.logs_out = portal, logs_out

    async def send(self, text, *, to=None):
        await super().send(text, to=to)
        if self.logs_out and LIVE in text:
            asyncio.get_running_loop().call_later(
                0.1, lambda: setattr(self.portal, "signed_in", False))


async def signed_in_page(page_for):
    portal = MockAviall(mfa=False, menu_logout=True)
    page = await page_for(portal)
    await adapter().login(page, cred())
    assert portal.signed_in
    return portal, page


def stuck(**config) -> StuckLogout:
    StuckLogout.tries = 0
    base = adapter()
    return StuckLogout(extractor=base.extractor, vendor_config={**base.config, **config})


@pytest.mark.asyncio
async def test_third_attempt_is_a_person_logging_out_through_live_view(page_for, monkeypatch):
    monkeypatch.setattr(hitl, "LOGOUT_POLL_SECONDS", 0.2)
    portal, page = await signed_in_page(page_for)
    store, person = MemoryStore(), Person(portal)
    ok = await logout_with_handoff(stuck(), page, PersonHandoff(store, person, "", wait_seconds=10),
                                   account="aviall__", people=(JANE,), live_view_url=live_url)
    assert ok and StuckLogout.tries == 2 and not portal.signed_in
    (to, ask), (_, thanks) = person.messages
    assert to == JANE and LIVE in ask and "log out" in ask
    assert "logged out" in thanks
    doc, = store.hitl.values()
    assert doc["mode"] == "logout" and doc["state"] == "signed_out"


@pytest.mark.asyncio
async def test_nobody_logs_out_in_time(page_for, monkeypatch):
    monkeypatch.setattr(hitl, "LOGOUT_POLL_SECONDS", 0.1)
    portal, page = await signed_in_page(page_for)
    store, person = MemoryStore(), Person(portal, logs_out=False)
    ok = await logout_with_handoff(stuck(), page, PersonHandoff(store, person, "", wait_seconds=1),
                                   account="aviall__", people=(JANE,), live_view_url=live_url)
    assert not ok and portal.signed_in
    assert "may still be signed in" in person.messages[-1][1]
    doc, = store.hitl.values()
    assert doc["state"] == "expired"


@pytest.mark.asyncio
async def test_without_live_view_nobody_is_asked(page_for):
    async def no_live_view():
        raise LiveViewUnavailable("local browser")

    portal, page = await signed_in_page(page_for)
    person = Person(portal)
    ok = await logout_with_handoff(stuck(), page, PersonHandoff(MemoryStore(), person, ""),
                                   account="aviall__", live_view_url=no_live_view)
    assert not ok and person.messages == [] and StuckLogout.tries == 2


@pytest.mark.asyncio
async def test_a_confirmed_logout_asks_nobody(page_for):
    portal, page = await signed_in_page(page_for)
    person = Person(portal)
    ok = await logout_with_handoff(adapter(), page, PersonHandoff(MemoryStore(), person, ""),
                                   account="aviall__", live_view_url=live_url)
    assert ok and not portal.signed_in and person.messages == []


class StuckFakeAdapter(FakeAdapter):
    accepts_person_logout = True
    tries = 0

    async def logout(self, page) -> bool:
        StuckFakeAdapter.tries += 1
        return False

    async def person_finished_logout(self, page) -> bool:
        page.set_signed_in(False)
        return True


@pytest.mark.asyncio
async def test_the_run_asks_the_requester_to_log_out_and_records_it(monkeypatch):
    monkeypatch.setattr(hitl, "LOGOUT_POLL_SECONDS", 0.01)
    store = MemoryStore()
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    StuckFakeAdapter.tries = 0
    deps.adapter_factory = lambda vid, sel, cfg: StuckFakeAdapter(portal, selectors=sel,
                                                                  vendor_config=cfg)
    deps.code_handoff = PersonHandoff(store, deps.notifier, "", wait_seconds=5)
    await run(deps, "r1")
    assert StuckFakeAdapter.tries == 2
    texts = [t for _, t in deps.notifier.messages]
    assert any("https://live.example/" in t and "log out" in t for t in texts)
    account = store.accounts[f"proponent__{JANE}"]
    assert account["last_login_at"] is None and account["last_logout_at"]


@pytest.mark.asyncio
async def test_a_local_browser_is_not_closed_before_the_logout_is_confirmed(monkeypatch):
    # Team rule (2026-11-04): the worker on a local browser waits (no time limit)
    # instead of closing signed in; no Chat handoff here, so the hold decides.
    monkeypatch.setattr(hitl, "HOLD_POLL_SECONDS", 0)
    store = MemoryStore()
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    StuckFakeAdapter.tries = 0
    deps.adapter_factory = lambda vid, sel, cfg: StuckFakeAdapter(portal, selectors=sel,
                                                                  vendor_config=cfg)
    deps.code_handoff = None
    deps.browser.waits_for_logout = True
    order = []
    real_close = deps.browser.close

    async def close(handle, **kw):
        order.append("close")
        return await real_close(handle, **kw)
    deps.browser.close = close
    real_hold = hitl.hold_until_logged_out

    async def hold(*a, **kw):
        order.append("hold")
        return await real_hold(*a, **kw)
    import sourcing.pipeline as pipeline
    monkeypatch.setattr(pipeline, "hold_until_logged_out", hold)
    await run(deps, "r1")
    assert StuckFakeAdapter.tries == 2 and order == ["hold", "close"]
    account = store.accounts[f"proponent__{JANE}"]
    assert account["last_login_at"] is None and account["last_logout_at"]


@pytest.mark.asyncio
async def test_the_hold_ends_when_the_browser_is_gone(monkeypatch):
    # Textron 2026-11-12: the window closed, then "Connection closed" on every check
    # for as long as the run lived.
    from types import SimpleNamespace
    from sourcing.auth import hitl
    monkeypatch.setattr(hitl, "HOLD_POLL_SECONDS", 0)

    class Gone:
        display_name = "Textron Aviation"

        async def person_finished_logout(self, page):
            raise AssertionError("not checked on a closed browser")

    class Browser:
        shows_window = True

        async def live_view_url(self, session_id):
            raise RuntimeError("none")

    page = SimpleNamespace(is_closed=lambda: True)
    handle = SimpleNamespace(page=page, session_id="s1", browser=None)
    assert await hitl.hold_until_logged_out("textron__shared", Gone(), handle, Browser()) is False
