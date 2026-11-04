"""A vendor set up from its login secret alone (no record, no adapter code), the
first-run flag, and not-sold wording learned from the page. Mock portal from the
generic adapter tests; skipped if no local Chromium."""
from __future__ import annotations

import json

import pytest

from sourcing.models import RESULTS_HEADER
from sourcing.credentials.base import SHARED, MemoryCredentialProvider, VendorCredential, secret_key
from sourcing.pipeline import NEW_VENDOR_FLAG, Deps, Pipeline, default_adapter_factory
from sourcing.store import MemoryStore
from sourcing.vendors.registry import record_from_secret, site_domain

from .fakes import FakeNotifier, FakeSheet, FakeSink
from .test_generic_adapter import HOST, page_for  # noqa: F401
from .test_generic_search import CatalogPortal, PageBrowser, ReadingLlm, row, signed_in

FLAG = RESULTS_HEADER.index("Flag")
SECRET = {"username": "jane", "password": "pw", "url": f"{HOST}/account/login"}


def test_site_domain_and_record_from_secret():
    assert site_domain("https://shop.wencor.com/login") == "wencor.com"
    assert site_domain("https://portal.vendor.co.uk/a") == "vendor.co.uk"
    cred = VendorCredential("wencor", SHARED, "sourcing-vendor-wencor", "u", "p",
                            url="https://shop.wencor.com/login", allowed_hosts=("login.okta.com",))
    record, problem = record_from_secret("Wencor ", cred)
    assert problem is None
    assert record["allowed_hosts"] == ["wencor.com", "login.okta.com"]
    assert record["adapter"] == "generic" and record["first_run_done"] is False
    assert record["display_name"] == "Wencor"
    no_url = VendorCredential("x", SHARED, "sourcing-vendor-x", "u", "p")
    assert "has no 'url'" in record_from_secret("X", no_url)[1]
    http = VendorCredential("x", SHARED, "sourcing-vendor-x", "u", "p", url="http://x.com/login")
    assert "https" in record_from_secret("X", http)[1]


@pytest.mark.asyncio
async def test_secret_allowed_hosts_are_read():
    creds = MemoryCredentialProvider({"sourcing-vendor-acme": {
        **SECRET, "allowed_hosts": "login.okta.com, sso.acme.com"}})
    cred = await creds.resolve("acme", "")
    assert cred.allowed_hosts == ("login.okta.com", "sso.acme.com")


def _deps(page, sheet, store, secrets, llm=None):
    return Deps(sheets=sheet, store=store, creds=MemoryCredentialProvider(secrets),
                browser=PageBrowser(page), sink=FakeSink(sheet), notifier=FakeNotifier(),
                adapter_factory=default_adapter_factory(llm or ReadingLlm()))


@pytest.mark.asyncio
async def test_vendor_from_its_secret_alone_runs_and_first_results_are_flagged(page_for):
    portal = CatalogPortal()
    page = await page_for(portal)
    store = MemoryStore()                                   # no vendor record at all
    sheet = FakeSheet([{"vendor": "Example Parts", "part": "35010-1", "by": ""}])
    deps = _deps(page, sheet, store, {secret_key("example_parts", SHARED): SECRET})
    summary = await Pipeline(deps, "run-1").run()

    assert summary["counts"] == {"DONE": 1}
    record = store.get_vendor("example_parts")
    assert record["adapter"] == "generic" and record["allowed_hosts"] == ["example.com"]
    assert record["auto_created_from"] == "sourcing-vendor-example-parts"
    assert record["first_run_done"] is True and record["first_run_id"] == "run-1"
    assert NEW_VENDOR_FLAG in sheet.results[0][FLAG]
    text = deps.notifier.messages[-1][1]
    assert "*New vendors*" in text and "set up from secret" in text and "first run worked" in text

    # the next run: no flag, nothing set up again
    sheet2 = FakeSheet([{"vendor": "Example Parts", "part": "35010-2", "by": ""}])
    deps2 = _deps(page, sheet2, store, {secret_key("example_parts", SHARED): SECRET})
    await Pipeline(deps2, "run-2").run()
    assert NEW_VENDOR_FLAG not in (sheet2.results[0][FLAG] or "")
    assert "*New vendors*" not in deps2.notifier.messages[-1][1]


@pytest.mark.asyncio
async def test_unknown_vendor_without_secret_or_url_stops_with_the_reason(page_for):
    page = await page_for(CatalogPortal())
    store = MemoryStore()
    sheet = FakeSheet([{"vendor": "Nobody Inc", "part": "1", "by": ""},
                       {"vendor": "No Url", "part": "2", "by": ""}])
    deps = _deps(page, sheet, store, {"sourcing-vendor-no-url": {"username": "u", "password": "p"}})
    await Pipeline(deps, "run-1").run()
    assert sheet.status_of("1") == "NO_CREDENTIALS"
    assert sheet.status_of("2") == "NEEDS_ATTENTION"
    sent = " ".join(text for _, text in deps.notifier.messages)
    assert "no login secret sourcing-vendor-nobody-inc" in sent and "has no 'url'" in sent
    assert store.get_vendor("nobody_inc") is None and store.get_vendor("no_url") is None


@pytest.mark.asyncio
async def test_not_sold_wording_is_learned_from_a_checked_quote(page_for):
    llm = ReadingLlm(not_sold="NS-1: this part is not sold by Example Parts.")
    page, a = await signed_in(page_for, CatalogPortal(), llm)
    first = await a.search(page, row("NS-1"))
    assert not first.found and "not sold by Example Parts" in first.not_sold
    assert json.loads(a.learned["not_sold_phrases"]) == ["this part is not sold by Example Parts"]
    assert llm.calls.count("not_sold") == 1

    second = await a.search(page, row("NS-2"))              # matched by rule now
    assert "not sold by Example Parts" in second.not_sold
    assert llm.calls.count("not_sold") == 1


@pytest.mark.asyncio
async def test_not_sold_quote_that_is_not_on_the_page_is_ignored(page_for):
    llm = ReadingLlm(not_sold="We stopped selling this part in 2019.")
    page, a = await signed_in(page_for, CatalogPortal(), llm)
    outcome = await a.search(page, row("99999"))            # a plain "No results" page
    assert not outcome.found and not outcome.not_sold
    assert "not_sold_phrases" not in a.learned
