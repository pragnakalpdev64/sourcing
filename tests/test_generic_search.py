"""GenericAdapter search against a mock catalog (docs/GENERIC_VENDOR_PLAN.md
phase 4), and one whole pipeline run for a vendor that exists only as a record.
Skipped if no local Chromium."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from sourcing.credentials.base import SHARED, MemoryCredentialProvider, secret_key
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.pipeline import Deps, Pipeline, default_adapter_factory
from sourcing.store import MemoryStore
from sourcing.vendors import web
from sourcing.vendors.base import SessionExpired

from .fakes import FakeNotifier, FakeSheet, FakeSink
from .test_generic_adapter import (  # noqa: F401  (page_for is a fixture)
    DASHBOARD, HOST, FakeLlm, MockPortal, adapter, cred, index_of, page_for,
)

FORM_SEARCH = """<form action="/search" method="get">
<input name="q" placeholder="Part number"><button type="submit">Search</button></form>"""
# A box outside any form: Enter does nothing, only the button runs the search.
BUTTON_SEARCH = """<input id="pn" placeholder="Part number">
<button onclick="location.href='/search?q='+encodeURIComponent(document.getElementById('pn').value)">
Search</button>"""
# Results drawn in place by script: the address never holds the part number.
INPLACE_SEARCH = """<form onsubmit="document.getElementById('out').innerHTML =
 '<table><tr><td>'+this.q.value+'</td><td>$42.50</td><td>Qty 7</td></tr></table>'; return false;">
<input name="q" placeholder="Part number"></form><div id="out"></div>"""
# The box's script keeps its own value from keystrokes only, so a pasted value
# searches for nothing (DASI's Salesforce store, 2026-10-29).
KEYS_SEARCH = """<input id="pn" placeholder="Enter Part Number" onkeyup="
 if (event.key === 'Enter') { if (this.dataset.v) location.href = '/search?q=' + this.dataset.v; }
 else if (event.key.length === 1) this.dataset.v = (this.dataset.v || '') + event.key;">"""
# Like DASI: keystrokes only, Enter does nothing, the search button runs it; a
# "Multiple" (bulk) button next to it must not be clicked.
DASI_SEARCH = """<input id="pn" placeholder="Enter Part Number" onkeyup="
 if (event.key.length === 1) this.dataset.v = (this.dataset.v || '') + event.key;">
<button title="Search Advanced" onclick="location.href='/bulk'">Multiple</button>
<button onclick="var v = document.getElementById('pn').dataset.v;
 if (v) location.href = '/search?q=' + v;">Search</button>"""
# DASI as seen 2026-10-29: Enter only opens a recent-searches drop-down; the
# search runs from an icon button titled "Search" (next to "Search Advanced").
HISTORY_SEARCH = """<div class="custom-input-container"><input id="pn" placeholder="Enter Part Number"
 onkeydown="if (event.key === 'Enter') document.getElementById('h').style.display = 'block';">
<div id="h" class="search-history-dropdown" style="display:none">411A6880-9B October 29th</div></div>
<button title="Search" onclick="location.href = '/search?q=' + document.getElementById('pn').value">
<svg width="10" height="10"></svg></button>
<button title="Search Advanced" onclick="location.href = '/bulk'">Multiple</button>"""
# The first Enter empties the box; the second, with the part typed again, searches.
EMPTYING_SEARCH = """<input id="pn" placeholder="Part number" onkeydown="
 if (event.key !== 'Enter') return;
 if (!this.dataset.armed) { this.dataset.armed = 1; this.value = ''; }
 else location.href = '/search?q=' + this.value;">"""
# Only the Multiple box searches: its button opens a text area and a Submit.
MULTIPLE_ONLY = """<input id="pn" placeholder="Part number">
<button id="multi" onclick="document.getElementById('dlg').style.display = 'block'">Multiple</button>
<div id="dlg" style="display:none"><textarea id="parts"></textarea>
<button id="go" onclick="location.href = '/search?q=' + document.getElementById('parts').value">
Submit</button></div>"""
# Neither Enter nor the button does anything.
DEAD_SEARCH = """<input id="pn" placeholder="Part number"><button type="button">Search</button>"""
# No hint that it is a part search: only the LLM can tell.
UNKNOWN_SEARCH = """<form action="/search" method="get"><input id="x1" name="q"></form>
<input id="x2" name="other">"""


class CatalogPortal(MockPortal):
    def __init__(self, search=FORM_SEARCH, **kw):
        super().__init__(**kw)
        self.search_markup = search
        self.gets: list[str] = []

    def _dashboard(self) -> str:
        return DASHBOARD.format(logout='<a href="/account/logout">Log out</a>') + self.search_markup

    async def handle(self, route):
        url = urlparse(route.request.url)
        if route.request.method == "GET":
            self.gets.append(url.path + (f"?{url.query}" if url.query else ""))
        if url.path == "/search" and self.signed_in:
            pn = parse_qs(url.query).get("q", [""])[0]
            if pn.startswith("NS-"):
                body = f"<p>{pn}: this part is not sold by Example Parts.</p>"
            elif pn.startswith("35"):
                body = f"<table><tr><td>{pn}</td><td>$42.50</td><td>Qty 7</td></tr></table>"
            else:
                body = f"<p>No results found for {pn}</p>"
            html = DASHBOARD.format(logout='<a href="/account/logout">Log out</a>') + body
            await route.fulfill(status=200, content_type="text/html", body=html)
            return
        if url.path == "/gone":
            await route.fulfill(status=404, content_type="text/html", body="<p>Not found</p>")
            return
        await super().handle(route)


class ReadingLlm(FakeLlm):
    """FakeLlm that also reads results pages the way the real extractor answers."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.pages: list[str] = []

    async def extract_offers(self, *, page_text, page_url, part_number, **_):
        self.pages.append(page_url)
        if "Customer portal" in page_text and "Forgot password" in page_text:
            return False, "login_page", [], LlmUsage(calls=1)
        if part_number in page_text and "$42.50" in page_text:
            return True, "results", [Offer(part_number, "42.50", "USD", "7", "NE", None,
                                           page_url)], LlmUsage(calls=1)
        return False, "no_results", [], LlmUsage(calls=1)


def row(pn):
    return RequestRow(sheet_row=2, rfq_id="R1", vendor_raw="Example Parts", part_number=pn)


async def signed_in(page_for, portal, llm=None, **config):
    page = await page_for(portal)
    a = adapter(llm or ReadingLlm(), **config)
    await a.login(page, cred())
    return page, a


@pytest.mark.asyncio
async def test_search_box_by_rule_then_the_learned_address(page_for):
    portal = CatalogPortal()
    page, a = await signed_in(page_for, portal)
    first = await a.search(page, row("35010-1"))
    assert first.found and first.offers[0].price == "42.50"
    assert a.learned["search_input"] == "input[placeholder*='part' i]"
    assert a.learned["part_url_template"] == f"{HOST}/search?q={{part}}"

    portal.gets.clear()
    second = await a.search(page, row("35010-2"))
    assert second.found
    assert portal.gets == ["/search?q=35010-2"]          # straight to the results address


@pytest.mark.asyncio
async def test_address_from_the_vendor_record(page_for):
    portal = CatalogPortal()
    page, a = await signed_in(page_for, portal,
                              part_url_template=f"{HOST}/search?q={{part}}")
    portal.gets.clear()
    assert (await a.search(page, row("35010-1"))).found
    assert portal.gets == ["/search?q=35010-1"]


@pytest.mark.asyncio
async def test_bad_address_falls_back_to_the_search_box(page_for):
    portal = CatalogPortal()
    page, a = await signed_in(page_for, portal, part_url_template=f"{HOST}/gone?q={{part}}")
    outcome = await a.search(page, row("35010-1"))
    assert outcome.found and a.part_url_failed


@pytest.mark.asyncio
async def test_unknown_search_box_is_found_by_the_llm(page_for):
    llm = ReadingLlm(element=index_of(id="x1"))
    page, a = await signed_in(page_for, CatalogPortal(search=UNKNOWN_SEARCH), llm)
    assert (await a.search(page, row("35010-1"))).found
    assert llm.calls == ["element"] and a.learned["search_input"] == '[id="x1"]'


@pytest.mark.asyncio
async def test_llm_pick_that_is_not_a_text_box_is_refused(page_for):
    llm = ReadingLlm(element=index_of(text="Catalog"))          # a link
    page, a = await signed_in(page_for, CatalogPortal(search=UNKNOWN_SEARCH), llm)
    with pytest.raises(RuntimeError, match="No part search box"):
        await a.search(page, row("35010-1"))


@pytest.mark.asyncio
async def test_llm_pick_of_a_text_box_as_the_search_button_is_refused(page_for):
    # Enter does nothing here; the LLM (wrongly) names the text box as the button.
    lone_box = '<input id="pn" placeholder="Part number"><span>no button</span>'
    llm = ReadingLlm(element=index_of(id="pn"))
    page, a = await signed_in(page_for, CatalogPortal(search=lone_box), llm)
    with pytest.raises(RuntimeError, match="did not run"):
        await a.search(page, row("35010-1"))


@pytest.mark.asyncio
async def test_search_button_when_enter_does_nothing(page_for):
    page, a = await signed_in(page_for, CatalogPortal(search=BUTTON_SEARCH))
    assert (await a.search(page, row("35010-1"))).found
    assert a.learned["search_button"] == web.SEARCH_BUTTON_SELECTORS[0]


@pytest.mark.asyncio
async def test_box_that_only_hears_keystrokes_is_typed_key_by_key(page_for):
    portal = CatalogPortal(search=KEYS_SEARCH)
    page, a = await signed_in(page_for, portal)
    assert (await a.search(page, row("35010-1"))).found
    assert a.learned["part_url_template"] == f"{HOST}/search?q={{part}}"
    assert "search_button" not in a.learned


@pytest.mark.asyncio
async def test_button_next_to_a_keystroke_box_runs_the_search_and_is_reused(page_for):
    portal = CatalogPortal(search=DASI_SEARCH)
    page, a = await signed_in(page_for, portal)
    assert (await a.search(page, row("35010-1"))).found
    assert a.learned["search_button"] == web.SEARCH_BUTTON_SELECTORS[0]
    assert "/bulk" not in portal.gets

    # Next run without the learned address: the saved button is clicked at once.
    saved = {k: a.learned[k] for k in ("search_input", "search_button", "home_url")}
    b = adapter(ReadingLlm(), selectors=saved)
    portal.gets.clear()
    assert (await b.search(page, row("35010-2"))).found
    assert "/search?q=35010-2" in portal.gets and "/bulk" not in portal.gets


@pytest.mark.asyncio
async def test_search_that_does_not_run_is_an_error_not_a_not_found(page_for):
    llm = ReadingLlm()
    page, a = await signed_in(page_for, CatalogPortal(search=DEAD_SEARCH), llm)
    with pytest.raises(RuntimeError, match="did not run") as failed:
        await a.search(page, row("35010-1"))
    assert llm.pages == [] and "search_button" not in a.learned    # nothing was read
    assert "pressed Enter again" in str(failed.value) and "LLM gave up" in str(failed.value)

    # The run's next part fails at once instead of trying the same ways again.
    calls = list(llm.calls)
    with pytest.raises(RuntimeError, match="not tried"):
        await a.search(page, row("35010-2"))
    assert llm.calls == calls


@pytest.mark.asyncio
async def test_history_drop_down_is_not_a_search_and_the_icon_button_runs_it(page_for):
    portal = CatalogPortal(search=HISTORY_SEARCH)
    page, a = await signed_in(page_for, portal)
    outcome = await a.search(page, row("35010-1"))
    assert outcome.found and "/bulk" not in portal.gets
    assert a.learned["search_button"] == "button[title*='search' i]" + web._NOT_SIMPLE
    assert a.learned["part_url_template"] == f"{HOST}/search?q={{part}}"


@pytest.mark.asyncio
async def test_box_emptied_by_enter_is_filled_again(page_for):
    page, a = await signed_in(page_for, CatalogPortal(search=EMPTYING_SEARCH))
    assert (await a.search(page, row("35010-1"))).found
    assert "search_button" not in a.learned


@pytest.mark.asyncio
async def test_llm_finds_a_new_way_through_the_multiple_box(page_for):
    llm = ReadingLlm(steps=[("click", index_of(id="multi")), ("type", index_of(id="parts")),
                            ("click", index_of(id="go")), ("results", None)])
    portal = CatalogPortal(search=MULTIPLE_ONLY)
    page, a = await signed_in(page_for, portal, llm)
    outcome = await a.search(page, row("35010-1"))
    assert outcome.found and llm.calls.count("step") == 4
    assert a.learned["part_url_template"] == f"{HOST}/search?q={{part}}"


@pytest.mark.asyncio
async def test_llm_step_that_would_change_something_is_refused(page_for):
    llm = ReadingLlm(steps=[("click", index_of(text="Log out"))])
    portal = CatalogPortal(search=DEAD_SEARCH)
    page, a = await signed_in(page_for, portal, llm)
    with pytest.raises(RuntimeError, match="refused"):
        await a.search(page, row("35010-1"))
    assert portal.signed_in


@pytest.mark.asyncio
async def test_results_drawn_in_place(page_for):
    page, a = await signed_in(page_for, CatalogPortal(search=INPLACE_SEARCH))
    assert (await a.search(page, row("35010-1"))).found
    assert "part_url_template" not in a.learned          # nothing to learn from the address


@pytest.mark.asyncio
async def test_no_results_and_not_sold(page_for):
    page, a = await signed_in(page_for, CatalogPortal(),
                              not_sold_phrases=["not sold by Example Parts"])
    none = await a.search(page, row("99999"))
    assert not none.found and not none.not_sold
    not_sold = await a.search(page, row("NS-1"))
    assert not not_sold.found and "not sold by Example Parts" in not_sold.not_sold


@pytest.mark.asyncio
async def test_signed_out_during_search(page_for):
    portal = CatalogPortal()
    page, a = await signed_in(page_for, portal)
    await a.search(page, row("35010-1"))                  # learns the address
    portal.signed_in = False                              # portal session ended
    with pytest.raises(SessionExpired):
        await a.search(page, row("35010-2"))


# -- whole pipeline run for a record-only vendor -----------------------------------

class PageBrowser:
    """Hands the pipeline one already-open page (instead of Browserbase / Chromium)."""

    def __init__(self, page):
        self.page = page

    async def create_context(self):
        return "ctx-1"

    async def open(self, context_id, **_):
        from sourcing.browser.base import BrowserHandle
        return BrowserHandle("session-1", self.page, None, None)

    async def live_view_url(self, session_id):
        return "about:blank"

    async def close(self, handle, *, release=True):
        return None


@pytest.mark.asyncio
async def test_pipeline_runs_a_vendor_that_exists_only_as_a_record(page_for):
    portal = CatalogPortal()
    page = await page_for(portal)
    store = MemoryStore()
    store.update_vendor("example", {
        "display_name": "Example Parts", "aliases": ["Example Parts"], "adapter": "generic",
        "enabled": True, "login_url": f"{HOST}/account/login",
        "allowed_hosts": ["shop.example.com"],
    })
    sheet = FakeSheet([{"vendor": "Example Parts", "part": "35010-1", "by": "jane@co.com"},
                       {"vendor": "Example Parts", "part": "99999", "by": "jane@co.com"}])
    deps = Deps(
        sheets=sheet, store=store,
        creds=MemoryCredentialProvider({secret_key("example", SHARED):
                                        {"username": "jane", "password": "pw"}}),
        browser=PageBrowser(page), sink=FakeSink(sheet), notifier=FakeNotifier(),
        adapter_factory=default_adapter_factory(ReadingLlm()),
    )
    summary = await Pipeline(deps, "run-1").run()
    assert summary["counts"] == {"DONE": 1, "NOT_FOUND": 1}
    assert sheet.status_of("35010-1") == "DONE" and sheet.results
    saved = store.get_selectors("example")
    assert saved["part_url_template"] == f"{HOST}/search?q={{part}}"
    assert saved["username_input"] and saved["logout_link"]
    assert not portal.signed_in                            # logged out at the end
    assert store.get_account("example___shared")["state"] == "NEEDS_LOGIN"


# Results whose details only show after "Expand all" (as on Procart).
EXPAND_RESULTS = """<table><tr><td>{pn}</td><td>$42.50</td><td>Qty 7</td></tr>
<tr id="detail" style="display:none"><td>Part Condition: Factory New</td></tr></table>
<button onclick="document.getElementById('detail').style.display=''">Expand All Details</button>"""


class ExpandPortal(CatalogPortal):
    async def handle(self, route):
        url = urlparse(route.request.url)
        if url.path == "/search" and self.signed_in:
            pn = parse_qs(url.query).get("q", [""])[0]
            html = DASHBOARD.format(logout='<a href="/account/logout">Log out</a>') + \
                EXPAND_RESULTS.format(pn=pn)
            await route.fulfill(status=200, content_type="text/html", body=html)
            return
        await super().handle(route)


class TextLlm(ReadingLlm):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts: list[str] = []

    async def extract_offers(self, *, page_text, **kw):
        self.texts.append(page_text)
        return await super().extract_offers(page_text=page_text, **kw)


@pytest.mark.asyncio
async def test_details_are_expanded_before_reading(page_for):
    llm = TextLlm()
    page, a = await signed_in(page_for, ExpandPortal(), llm)
    outcome = await a.search(page, row("35010-1"))
    assert outcome.found
    assert "Factory New" in llm.texts[-1]
