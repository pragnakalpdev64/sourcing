"""WencorAdapter against an invented mock of Wencor's portal, from the team's
description (2026-11-03): the dashboard's search bar lists the parts found,
the product page of the exact part number holds the values. The real pages
haven't been saved yet. Skipped if no local Chromium."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.wencor import WencorAdapter
from sourcing.vendors.wencor_prompt import WENCOR_EXTRACT_SYSTEM

HOST = "https://shop.wencor.com"
DASHBOARD = """<html><body><a href="/account/logout">Log out</a>
<form action="/search"><input type="search" name="q" placeholder="Search part number">
<button>Search</button></form></body></html>"""
# Two similar numbers and the exact one; the exact one twice (two manufacturers).
LIST = """<html><body><a href="/account/logout">Log out</a><h1>Results for {q}</h1>
<div><a href="/product-details/-/o/d/MS20995C32-1/ecom-item/1">MS20995C32-1</a> Lockwire, other part</div>
<div><div><a href="/product-details/-/o/d/MS20995C32/ecom-item/2">MS20995C32</a> Lockwire .032</div><div>MOQ: 10</div></div>
<div><a href="/product-details/-/o/d/MS20995C32/ecom-item/3"><b>MS20995C32</b><br>Lockwire .032, other mfr</a></div>
<div><a href="/product-details#ecom-item/2">MS20995C32</a></div>
<div><a href="/cart/add?p=2">MS20995C32</a></div></body></html>"""
# As saved 2026-11-03: the minimum only in the quantity box's data-min; the
# "Availability for All Warehouses" link adds a pop-up at the end of the body
# (outside #main-content) for the quantity in the box.
PRODUCT = """<html><body><a href="/account/logout">Log out</a><a href="/cart">Cart</a>
<form action="/search"><input type="search" name="q" placeholder="Search part number">
<button>Search</button></form>
<div id="main-content"><h1>{pn}</h1><p>WIRE, LOCK product {n}</p>
<p>$3.10 / EA</p><div class="product-information"><div class="related-items">Interchanges:
<a href="/product-details/-/o/ecom-item/9">MS20995C32-SUB</a></div></div><p>In Stock: 40</p><p>Ships from Georgia (USA) Distribution on November 3, 2026</p><input id="qty" name="Quantity" value="1">
<input name="quantity" data-item="{n}" data-min="5" data-step="0" data-defaultquantity="1" value="1">
<a href="#" class="availability-trigger" onclick="setTimeout(() => {{ const d = document.createElement('div');
 d.innerText = 'Availability By Warehouse\\nGeorgia (USA) Distribution *\\n' + document.getElementById('qty').value
 + ' available on November 3, 2026'; document.body.appendChild(d); }}, 800); return false;">Availability for All Warehouses</a>
<button>Add to Cart</button></div></body></html>"""
# PowerSearch (/search) as saved 2026-11-03: a Copy and Paste box; the link
# turns each line into a row (li.field) with the part (h4), its hidden item
# number and, a moment later, its minimum-quantity warning.
POWERSEARCH = """<html><body><a href="/account/logout">Log out</a>
<form action="/search"><input type="search" name="q" placeholder="Search part number">
<button>Search</button></form>
<textarea id="paste"></textarea><a href="#" onclick="setTimeout(() => {
  const ul = document.getElementById('rows');
  for (const line of document.getElementById('paste').value.split('\\n')) {
    const [pn, qty] = line.split(' ');
    const li = document.createElement('li'); li.className = 'field';
    li.innerHTML = '<div class=helper-msg><span class=error-text></span></div>'
      + '<div class=item-details-preview><div class=item-preview><h4>' + pn + '</h4>'
      + '<ul><li class=item-number style=display:none><span>Item number: </span><span>2</span></li></ul>'
      + '<div>In Stock: 40</div></div></div>';
    ul.appendChild(li);
    // As on Wencor: the warning only for a quantity below the minimum.
    if (pn === 'MS20995C32' && parseInt(qty || '1', 10) < 10) setTimeout(() => { li.querySelector('.error-text').textContent =
      'Quantity entered is below minimum allowed quantity: 10'; }, 1500);
  }
}, 500); return false;">Click Here to Add To Quick Order Entry</a>
<ul id="rows"></ul></body></html>"""
NOTHING = """<html><body><a href="/account/logout">Log out</a><p>No results for {q}</p></body></html>"""


class MockWencor:
    def __init__(self):
        self.gets: list[str] = []

    async def handle(self, route):
        url = urlparse(route.request.url)
        self.gets.append(url.path)
        if url.path == "/search":
            q = parse_qs(url.query).get("q", [""])[0]
            body = (LIST if q.upper() == "MS20995C32" else NOTHING).format(q=q)
        elif url.path.startswith("/product-details/"):
            n = url.path.rsplit("/", 1)[1]
            pn = {"1": "MS20995C32-1", "9": "MS20995C32-SUB"}.get(n, "MS20995C32")
            body = PRODUCT.format(pn=pn, n=n)
        else:
            body = DASHBOARD
        await route.fulfill(status=200, content_type="text/html", body=body)


class Reader:
    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        found = "product page" in page_text
        offers = [Offer(part_number, "3.10", "USD", "40", None, None, page_url)] if found else []
        return found, "results" if found else "no_results", offers, LlmUsage(calls=1)

    async def not_sold_quote(self, *_, **__):
        return None, LlmUsage()


def row(pn):
    return RequestRow(sheet_row=2, rfq_id="R1", vendor_raw="Wencor", part_number=pn)


@pytest_asyncio.fixture
async def wencor_page():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockWencor()
    page = await browser.new_page()
    await page.route(f"{HOST}/**", portal.handle)
    await page.goto(f"{HOST}/dashboard")
    yield page, portal
    await browser.close()
    await pw.stop()


@pytest.mark.asyncio
async def test_exact_part_number_product_pages_are_read_not_similar_ones(wencor_page):
    page, portal = wencor_page
    reader = Reader()
    adapter = WencorAdapter(extractor=reader, vendor_config={"part_url_template": "",
                                                            "show_substitutes": True})
    outcome = await adapter.search(page, row("ms20995c32"))
    assert outcome.found
    opened = [g for g in portal.gets if g.startswith("/product-details")
              and not g.endswith("/9")]
    # item 2 listed twice (its link and "product-details#ecom-item/2", 2026-11-03): read once
    assert opened == ["/product-details/-/o/d/MS20995C32/ecom-item/2",
                      "/product-details/-/o/d/MS20995C32/ecom-item/3"]
    assert not any(g.startswith("/cart") for g in portal.gets)
    text = reader.texts[0]
    assert "Wencor product page 1 of 2" in text and "Wencor product page 2 of 2" in text
    assert "Results for" not in text                  # only the part's own list entry
    # its interchange (Wencor: "Interchanges / Substitutes"), read once as its own block
    assert "Substitute product page 1 of 1" in text and "listed as MS20995C32-SUB" in text
    assert portal.gets.count("/product-details/-/o/ecom-item/9") == 1
    first = text.split("Wencor product page 2 of 2")[0]
    assert "Its entry in the search list" in first and "MOQ: 10" in first
    assert "Minimum order quantity: 5" in first
    assert "with 5 in the quantity box" in first and "5 available on November 3, 2026" in first
    assert outcome.alternates == []                   # substitutes on: they have entries instead
    assert outcome.page_url == f"{HOST}/product-details/-/o/d/MS20995C32/ecom-item/2"
    assert outcome.offers[0].condition == "NE"        # Wencor shows none (2026-11-03)
    assert reader.systems[0] == WENCOR_EXTRACT_SYSTEM


@pytest.mark.asyncio
async def test_no_substitute_pages_by_default(wencor_page):
    # Team, 2026-11-03: no substitutes for Wencor; its interchanges aren't opened.
    page, portal = wencor_page
    reader = Reader()
    adapter = WencorAdapter(extractor=reader, vendor_config={"part_url_template": ""})
    assert not adapter.show_substitutes
    outcome = await adapter.search(page, row("ms20995c32"))
    assert outcome.found
    assert "Substitute product page" not in reader.texts[0]
    assert "/product-details/-/o/ecom-item/9" not in portal.gets
    # ... but the numbers it lists go to the requested part's Internal Comments
    # (team, 2026-11-04): no page opened, no entry of their own.
    assert outcome.alternates == ["MS20995C32-SUB"]


@pytest.mark.asyncio
async def test_no_listed_part_reads_the_list_itself(wencor_page):
    page, _ = wencor_page
    reader = Reader()
    adapter = WencorAdapter(extractor=reader, vendor_config={"part_url_template": ""})
    outcome = await adapter.search(page, row("XYZ-1"))
    assert not outcome.found
    assert "No results for XYZ-1" in reader.texts[0]


@pytest.mark.asyncio
async def test_bulk_takes_the_minimum_from_powersearch_and_the_rest_from_product_pages(
        wencor_page):
    page, portal = wencor_page
    await page.route(f"{HOST}/search", lambda route: route.fulfill(
        status=200, content_type="text/html", body=POWERSEARCH))
    reader = Reader()
    adapter = WencorAdapter(extractor=reader, vendor_config={
        "part_url_template": "", "powersearch_url": f"{HOST}/search"})
    # 20 needed, minimum 10: PowerSearch is still asked with 1, so the minimum shows.
    rows = [RequestRow(sheet_row=2, rfq_id="R1", vendor_raw="Wencor", part_number="MS20995C32",
                       qty_needed="20"), RequestRow(sheet_row=3, rfq_id="R1", vendor_raw="Wencor",
                                          part_number="XYZ-1")]
    outcomes, usage = await adapter.search_many(page, rows)
    assert set(outcomes) == {rows[0].row_id, rows[1].row_id}
    assert outcomes[rows[0].row_id].found and not outcomes[rows[1].row_id].found
    text = reader.texts[0]
    assert "Its row in PowerSearch" in text
    assert "below minimum allowed quantity: 10" in text.split("Wencor product page 2 of 2")[0]
    assert outcomes[rows[0].row_id].offers[0].vendor_note == (
        "Ships from Georgia (USA) Distribution on November 3, 2026")
    assert usage.calls == 2


def test_wencor_has_its_own_adapter_and_prompt():
    assert adapter_class("wencor") is WencorAdapter
    assert "## 5. Wencor Pages" in WENCOR_EXTRACT_SYSTEM


# fly.wencor.com/log-in (2026-11-03): the header's hidden Log in drop-down form
# comes before the page's own form.
TWO_FORMS = """<html><body>
<button id="util-item-login">Log in</button>
<div style="display:none"><form class="login-form" id="_p_INSTANCE_1234_login-form">
<input id="_p_INSTANCE_1234_username" type="text"><input id="_p_INSTANCE_1234_password" type="password">
<button type="submit">Log in</button></form></div>
<form class="login-form" id="_p_INSTANCE_Al8rQt3TrPh8_login-form">
<input id="_p_INSTANCE_Al8rQt3TrPh8_username" type="text" placeholder="Email Address">
<input id="_p_INSTANCE_Al8rQt3TrPh8_password" type="password"><button type="submit">Log in</button>
</form></body></html>"""


@pytest.mark.asyncio
async def test_login_uses_the_pages_own_form_not_the_header_drop_down(wencor_page):
    page, _ = wencor_page
    await page.set_content(TWO_FORMS)
    adapter = WencorAdapter(extractor=Reader(), selectors={})
    for key in ("username_input", "password_input", "submit_button"):
        selector = await adapter._saved_fit(page, key)
        assert await page.locator(selector).count() == 1
        assert "Al8rQt3TrPh8" in await page.locator(selector).evaluate(
            "el => el.id || el.form.id")
    assert await adapter._has_form(page)


@pytest.mark.asyncio
async def test_login_page_never_opens_the_header_drop_down(wencor_page):
    page, portal = wencor_page

    async def login_page(route):
        await route.fulfill(status=200, content_type="text/html", body=TWO_FORMS.replace(
            '<button id="util-item-login">',
            '<button id="util-item-login" onclick="document.title=&quot;clicked&quot;">'))
    await page.route(f"{HOST}/log-in", login_page)
    cred = VendorCredential(vendor_id="wencor", owner="_shared", secret_key="sourcing-vendor-wencor",
                            username="u@example.com", password="x", url=f"{HOST}/log-in")
    adapter = WencorAdapter(extractor=Reader(), selectors={})
    assert await adapter._open_login_page(page, cred)
    assert await page.title() != "clicked"
