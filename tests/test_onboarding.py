"""Onboarding run (sourcing/onboarding.py, scripts/onboard_vendor.py) against the
mock catalog from the generic adapter tests. Skipped if no local Chromium."""
from __future__ import annotations

import pytest

from scripts.onboard_vendor import build_parser, main
from sourcing.onboarding import onboard, report_text

from .test_generic_adapter import adapter, cred, index_of, page_for  # noqa: F401
from .test_generic_search import UNKNOWN_SEARCH, CatalogPortal, PageBrowser, ReadingLlm


class ClosingBrowser(PageBrowser):
    def __init__(self, page):
        super().__init__(page)
        self.closed = False

    async def close(self, handle, *, release=True):
        self.closed = True


@pytest.mark.asyncio
async def test_onboarding_logs_in_searches_logs_out_and_reports(page_for):
    portal = CatalogPortal()
    browser = ClosingBrowser(await page_for(portal))
    report = await onboard("example", adapter(ReadingLlm(),
                                              not_sold_phrases=["not sold by Example Parts"]),
                           cred(), browser, ["35010-1", "99999", "NS-1"])
    assert report.ok and not report.stopped
    assert [p.status for p in report.parts] == ["found", "no results", "not sold"]
    assert report.parts[0].offers[0]["price"] == "42.50"
    assert report.signed_in_check and report.logged_out and browser.closed
    assert not portal.signed_in
    # how each element was found, and what a --save would store
    assert report.found_by["username_input"] == "generic list"
    assert report.found_by["search_input"] == "generic list"
    assert report.learned["part_url_template"].endswith("/search?q={part}")
    assert report.llm_discovery_calls == 0
    text = report_text(report)
    assert "OK" in text.splitlines()[0] and "part NS-1: not sold" in text


@pytest.mark.asyncio
async def test_unknown_search_box_is_reported_as_found_by_the_llm(page_for):
    llm = ReadingLlm(element=index_of(id="x1"))
    report = await onboard("example", adapter(llm), cred(), ClosingBrowser(
        await page_for(CatalogPortal(search=UNKNOWN_SEARCH))), ["35010-1"])
    assert report.ok
    assert report.found_by["search_input"] == "LLM"
    assert report.llm_discovery_calls == 1


@pytest.mark.asyncio
async def test_captcha_stops_the_run_without_searching(page_for):
    # A CAPTCHA is a failed login for now (2026-11-03), no longer "blocked".
    browser = ClosingBrowser(await page_for(CatalogPortal(captcha=True)))
    report = await onboard("example", adapter(ReadingLlm()), cred(), browser, ["35010-1"])
    assert not report.ok and report.stopped.startswith("the login could not")
    assert report.parts == [] and browser.closed


@pytest.mark.asyncio
async def test_sms_code_and_wrong_password_stop_with_their_reason(page_for):
    sms = await onboard("example", adapter(ReadingLlm()), cred(),
                        ClosingBrowser(await page_for(CatalogPortal(mfa="sms"))), ["35010-1"])
    assert sms.stopped.startswith("2FA")
    bad = await onboard("example", adapter(ReadingLlm()), cred(password="nope"),
                        ClosingBrowser(await page_for(CatalogPortal())), ["35010-1"])
    assert bad.stopped.startswith("the portal rejected")


@pytest.mark.asyncio
async def test_part_count_is_limited():
    with pytest.raises(ValueError):
        await onboard("example", adapter(ReadingLlm()), cred(), None, ["a", "b", "c", "d"])
    with pytest.raises(SystemExit):
        main(["example", "--part", "a", "--part", "b", "--part", "c", "--part", "d"])
    with pytest.raises(SystemExit):              # no parts: only with --login-only
        main(["example"])
    assert build_parser().parse_args(["x", "--part", "a"]).fresh is False


def test_report_lists_what_to_check():
    from sourcing.onboarding import OnboardingReport, PartResult
    r = OnboardingReport("acme", "GenericAdapter", signed_in=True, signed_in_check=True)
    r.parts = [PartResult("P1", "found", 1.0, offers=[{"part_number": "P1", "price": None,
               "qty_available": None, "condition": None, "lead_time": None}]),
               PartResult("P2", "found", 1.0, offers=[{"part_number": "P2", "price": "5",
               "qty_available": "3", "condition": None, "lead_time": None}])]
    r.found_by = {"search_input": "LLM", "username_input": "generic list"}
    notes = r.to_check
    assert any(n.startswith("P1: found, but every line is empty") for n in notes)
    assert any(n.startswith("P2: in-stock lines have no condition") for n in notes)
    assert any("found by the LLM" in n and "search_input" in n for n in notes)
    assert any("no direct results address" in n for n in notes)
    assert "TO CHECK:" in report_text(r) and r.as_dict()["to_check"] == notes


class _Handle:
    page = object()
    session_id = "s1"


class _HoldBrowser:
    def __init__(self, window=False, link=None):
        self.shows_window = window
        self.link = link

    async def live_view_url(self, session_id):
        if not self.link:
            raise RuntimeError("no live view")
        return self.link


class _HoldAdapter:
    """Signed out only after `after` sign-out checks (a person logging out)."""
    def __init__(self, after):
        self.after = after
        self.checks = 0
        self.logouts = 0

    async def person_finished_logout(self, page):
        self.checks += 1
        return self.checks >= self.after

    async def logout(self, page):
        self.logouts += 1
        return self.logouts >= 2


@pytest.mark.asyncio
async def test_unconfirmed_logout_waits_for_a_person_in_the_window(monkeypatch):
    # Team rule (2026-11-04): never close a session that may be signed in.
    import sourcing.auth.hitl as hitl
    monkeypatch.setattr(hitl, "HOLD_POLL_SECONDS", 0)
    ad = _HoldAdapter(after=5)
    assert await hitl.hold_until_logged_out("aar", ad, _Handle(),
                                                   _HoldBrowser(window=True)) is True
    assert ad.checks == 5 and ad.logouts == 0          # the person's page is left alone


@pytest.mark.asyncio
async def test_unreachable_browser_retries_the_logout_until_confirmed(monkeypatch):
    import sourcing.auth.hitl as hitl
    monkeypatch.setattr(hitl, "HOLD_POLL_SECONDS", 0)
    ad = _HoldAdapter(after=10**9)
    assert await hitl.hold_until_logged_out("aar", ad, _Handle(), _HoldBrowser()) is True
    assert ad.logouts == 2


@pytest.mark.asyncio
async def test_onboarding_never_closes_before_the_hold_returns(page_for, monkeypatch):
    import sourcing.onboarding as onboarding
    order = []

    async def hold(*a, **kw):
        order.append("hold")
        return True
    monkeypatch.setattr(onboarding, "hold_until_logged_out", hold)
    ad = adapter(ReadingLlm())

    async def no_logout(page):
        return False
    monkeypatch.setattr(ad, "logout", no_logout)
    monkeypatch.setattr(type(ad), "accepts_person_logout", False)
    browser = ClosingBrowser(await page_for(CatalogPortal()))
    real_close = browser.close

    async def close(handle, **kw):
        order.append("close")
        await real_close(handle, **kw)
    browser.close = close
    report = await onboard("example", ad, cred(), browser, [])
    assert order == ["hold", "close"] and report.logged_out is True
