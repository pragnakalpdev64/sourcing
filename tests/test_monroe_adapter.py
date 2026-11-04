"""MonroeAdapter against a mock of Monroe's store (BigCommerce), built from the
public pages seen 2026-11-04: search.php?search_query_adv=..., article.productCard
cards (price, "In Stock", Request Quote), product pages with "Minimum Purchase".
Skipped if no local Chromium."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.base import BulkSearchUnavailable
from sourcing.vendors.monroe import MonroeAdapter, _card_lines, search_query
from sourcing.vendors.monroe_prompt import MONROE_EXTRACT_SYSTEM

HOST = "https://monroeaerospace.com"
CARD = """<li class="product"><article class="productCard">
<div class="productCard-imageSection"><div class="productCard-actions"><a href="mailto:x">Email</a></div>
<a href="{host}/drill-bits/{slug}/?searchuuid=u1&search_query=x" class="productCard-imageLink"></a>
<div class="productCard-imagePrice" style="{pstyle}">{price}</div></div>
<div class="productCard-content"><h3 class="productCard-sku"><a href="{host}/drill-bits/{slug}/?searchuuid=u1&search_query=x">{sku}</a></h3>
<p class="productCard-name">DRILL BIT ({sku})</p>{stock}{action}</div></article></li>"""
STOCK = ('<div class="productCard-stock"><span class="productCard-stockText '
         'productCard-stockText--inStock">In Stock</span></div>')
PAGE = """<html><body><a href="/cart.php">Cart</a><h1 class="page-heading">8898 results for 'x'</h1>
<ul class="productGrid">{cards}</ul>{pager}</body></html>"""
NEXT = """<ul class="pagination-list"><li class="pagination-item pagination-item--next">
<a class="pagination-link" href="/search.php?search_query_adv={q}&page=2">Next</a></li></ul>"""
# As seen 2026-11-04: the dl comes hidden from the server, the Specifications
# section is outside .productView, the variants table is drawn by the page's
# script (Available per filter choice), the bulk rates sit in a hidden pop-up.
PRODUCT = """<html><body><div id="main-content" data-currency-code="USD"><div class="productView">
<h1>{sku}</h1><span class="price-value">US$3.75</span> / Unit
<span>In Stock - 30 units available</span>
<dl style="display: none;"><dt>Minimum Purchase:</dt><dd>5 units</dd></dl>
<div id="modal-bulk-pricing" class="modal" style="display:none"><ul class="bulk-discount-list">
<li>Buy 50 - 99 and pay only $3.50 each</li><li>Buy 100 - 249 and pay only $3.20 each</li></ul></div>
<section class="variantFilter" data-variant-filter-wrapper></section></div>
<section class="pdp-section--specifications"><div>note-Minimum Order Note:</div>
<div>Minimum order is 5. Must order in increments of 1.</div></section>
<section data-similar-products><h3>View Similar Products</h3>
<article class="productCard"><h3 class="productCard-sku"><a href="/x/">SIMILAR-99</a></h3></article>
</section></div>
<script>
const STOCK = {Any: '30', ACME: '10', BETA: '20'};
function draw(choice) {
  document.querySelector('[data-variant-filter-wrapper]').innerHTML = `<table class="variant-filter-table">
   <thead><tr><th>MFR Certificate</th><th>Manufacturer</th><th>Available</th><th>Unit of Measure</th><th>Unit Price</th></tr></thead>
   <tbody><tr><td><select data-variant-filter-select="MFR Certificate"><option>Any</option><option>Yes</option></select></td>
   <td><select data-variant-filter-select="Manufacturer"><option>Any</option><option>ACME</option><option>BETA</option></select></td>
   <td><span data-variant-filter-total-row>${STOCK[choice]}</span></td><td><span data-variant-filter-uom>EA</span></td><td>$3.75</td></tr></tbody></table>`;
  const sel = document.querySelector("select[data-variant-filter-select='Manufacturer']");
  sel.value = choice;
  sel.addEventListener('change', () => draw(sel.value));
}
if ({draw}) setTimeout(() => draw('Any'), 300);
</script></body></html>"""


def card(sku, priced=True, instock=True):
    return CARD.format(host=HOST, slug=sku.lower(), sku=sku,
                       price="$3.75" if priced else "$0.00",
                       pstyle="" if priced else "display:none",
                       stock=STOCK if instock else "",
                       action=('<a class="productCard-addToCart" href="#">Add to Cart</a>'
                               if priced else
                               '<a class="productCard-requestQuote" href="#">Request Quote</a>'))


class MockMonroe:
    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.gets: list[str] = []
        self.queries: list[str] = []
        self.fetched: list[str] = []
        self.draw_table = True            # False: the variants table never shows

    async def handle(self, route):
        url = urlparse(route.request.url)
        self.gets.append(url.path)
        q = parse_qs(url.query)
        if url.path == "/search.php":
            self.queries.append(url.query)
            term = q.get("search_query_adv", [""])[0]
            if term == "MS20034-1, TWD-4":                       # both on page 1, noise first
                body = PAGE.format(cards=card("ATTWD-4", priced=False) + card("MS20034-4")
                                   + card("TWD-4") + card("MS20034-1", instock=False), pager="")
            elif term == "TWD-4, ZZ-9, 3880938-1":                # 3880938-1 only on page 2
                if q.get("page") == ["2"]:
                    body = PAGE.format(cards=card("3880938-1"), pager="")
                else:
                    body = PAGE.format(cards=card("TWD-4") + card("3880938-10"),
                                       pager=NEXT.format(q=url.query.split("=", 1)[1].split("&")[0]))
            elif term in ("TWD-4", "TWD 4", "ZZ-9"):
                body = PAGE.format(cards=card("TWD-4") if "4" in term else card("ZZ-90"), pager="")
            else:
                body = PAGE.format(cards=card("OTHER-1"), pager="")
            await route.fulfill(status=200, content_type="text/html", body=body)
        elif url.path.startswith("/drill-bits/"):
            await route.fulfill(status=200, content_type="text/html", body=product_page(
                url.path, self.draw_table))
        elif url.path == "/account.php":
            if self.signed_in:
                await route.fulfill(status=200, content_type="text/html", body="<p>Account</p>")
            else:
                await route.fulfill(status=302, headers={"location": f"{HOST}/login.php?from=account.php"})
        else:
            await route.fulfill(status=200, content_type="text/html", body="<p>x</p>")


def product_page(path, draw=True):
    sku = path.strip("/").split("/")[-1].upper()
    return PRODUCT.replace("{sku}", sku).replace("{draw}", "true" if draw else "false")


def product_link(pn):
    return f"{HOST}/drill-bits/{pn.lower()}/"


class Reader:
    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []
        self.bulk_parts: list[list[str]] = []

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        found = "Card 1:" in page_text
        offers = [Offer(part_number, "3.75", "USD", "30", None, None, product_link(part_number))
                  ] if found else []
        return found, "results" if found else "no_results", offers, LlmUsage(calls=1)

    async def extract_offers_bulk(self, *, page_text, page_url, parts, system=""):
        self.texts.append(page_text)
        self.bulk_parts.append([p["part_number"] for p in parts])
        found = {p["part_number"]: [Offer(p["part_number"], "3.75", "USD", "30", None,
                                          None, product_link(p["part_number"]))]
                 for p in parts if f"Part number: {p['part_number']}" in page_text}
        return "results", found, LlmUsage(calls=1)

    async def not_sold_quote(self, *_, **__):
        return None, LlmUsage()


def adapter_for(portal, fetch=True, **kw):
    """page.route doesn't see page.request, so the account check and the product
    page fetch read the mock (fetch=False: every fetch fails -> browser fallback)."""
    adapter = MonroeAdapter(**kw)

    async def account(_page):
        return portal.signed_in

    async def fetch_html(_page, href):
        portal.fetched.append(urlparse(href).path)
        if not fetch:
            return None
        return product_page(urlparse(href).path)
    adapter._account_state = account
    adapter._fetch_html = fetch_html
    return adapter


def row(pn, n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="Monroe", part_number=pn)


@pytest_asyncio.fixture
async def monroe_page():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockMonroe()
    context = await browser.new_context()
    await context.route(f"{HOST}/**", portal.handle)    # product tabs too
    page = await context.new_page()
    await page.goto(f"{HOST}/account.php")
    yield page, portal
    await browser.close()
    await pw.stop()


def test_search_query_is_the_teams_format():
    assert search_query(["twd 2"]) == "twd+2"
    assert (search_query(["twd-4", "twd-5", "3M-2090-3/4"])
            == "twd-4%2C+twd-5%2C+3M-2090-3%2F4")
    assert (search_query(["MS21250-05022", "MS20034-1", "twd-4"])
            == "MS21250-05022%2C+MS20034-1%2C+twd-4")


def test_registered_for_the_sheet_and_records():
    for name in ("monroe", "monroe_aerospace", "monroeaerospace"):
        assert adapter_class(name) is MonroeAdapter
    assert adapter_class("x", {"adapter": "monroe"}) is MonroeAdapter


def test_hidden_zero_price_is_no_price():
    lines = _card_lines({"sku": "A-1", "name": "N", "price": "$0.00", "stock": "",
                         "quote": True, "cart": False, "href": ""})
    assert "Price: none shown" in lines and "Action: Request Quote" in lines


@pytest.mark.asyncio
async def test_one_part_keeps_the_exact_card_and_opens_its_product_page(monroe_page):
    page, portal = monroe_page
    reader = Reader()
    outcome = await adapter_for(portal, extractor=reader).search(page, row("TWD-4"))
    assert outcome.found
    assert portal.queries == ["search_query_adv=TWD-4"]
    text = reader.texts[0]
    assert "Card 1: https://monroeaerospace.com/drill-bits/twd-4/ ---" in text   # no searchuuid
    assert "Part number: TWD-4" in text and "Price: $3.75" in text and "Stock: In Stock" in text
    assert "In Stock - 30 units available" in text                   # the product page
    assert "Minimum order is 5." in text                             # outside .productView
    assert "Available: 30; Unit of Measure: EA; Unit Price: $3.75" in text   # the stock table
    assert "Currency code of the page: USD" in text
    # Its filter choices are neither tried nor passed on (team, 2026-11-04).
    assert "ACME" not in text and "BETA" not in text and "MFR Certificate" not in text
    assert "Buy 50 - 99 and pay only $3.50 each" in text             # the bulk rates pop-up
    assert "/drill-bits/twd-4/" in portal.gets and portal.fetched == []   # its own tab
    assert outcome.offers[0].vendor_note is None
    assert "SIMILAR-99" not in text                                  # similar products cut
    assert "8898 results" in text
    assert reader.systems[0] == MONROE_EXTRACT_SYSTEM
    assert not any("cart" in g or "logout" in g for g in portal.gets)
    assert outcome.offers[0].condition == "NE"                       # condition_not_shown default


@pytest.mark.asyncio
async def test_part_written_with_a_space_still_matches(monroe_page):
    page, portal = monroe_page
    reader = Reader()
    outcome = await adapter_for(portal, extractor=reader).search(page, row("TWD 4"))
    assert outcome.found and "Part number: TWD-4" in reader.texts[0]


@pytest.mark.asyncio
async def test_no_card_for_the_part_is_not_found(monroe_page):
    page, portal = monroe_page
    reader = Reader()
    outcome = await adapter_for(portal, extractor=reader).search(page, row("ZZ-9"))
    assert not outcome.found
    assert "no card is for ZZ-9" in reader.texts[0]
    assert "ZZ-90" not in reader.texts[0].replace("no card is for ZZ-9", "")   # look-alike dropped


@pytest.mark.asyncio
async def test_several_parts_drop_look_alikes_and_request_quote_card(monroe_page):
    page, portal = monroe_page
    reader = Reader()
    rows = [row("MS20034-1", 2), row("TWD-4", 3)]
    outcomes, usage = await adapter_for(portal, extractor=reader).search_many(page, rows)
    assert portal.queries == ["search_query_adv=MS20034-1%2C+TWD-4"]
    assert set(outcomes) == {r.row_id for r in rows}
    text = reader.texts[0]
    assert "ATTWD-4" not in text and "MS20034-4" not in text
    assert "Out of Stock" not in text and text.count("--- Product page of card") == 2
    assert text.count("--- Stock table of card") == 2
    assert usage.calls == 1


@pytest.mark.asyncio
async def test_exact_card_on_page_two_is_found_and_missing_part_searched_alone(monroe_page):
    page, portal = monroe_page
    reader = Reader()
    rows = [row("TWD-4", 2), row("ZZ-9", 3), row("3880938-1", 4)]
    outcomes, _ = await adapter_for(portal, extractor=reader).search_many(page, rows)
    assert "page=2" in portal.queries[1]                       # next results page read
    assert portal.queries[-1] == "search_query_adv=ZZ-9"       # no card anywhere: asked alone
    found = {r.part_number for r in rows if outcomes.get(r.row_id) and outcomes[r.row_id].found}
    assert found == {"TWD-4", "3880938-1"}
    # Searched alone too and not listed: an outcome, so the pipeline doesn't search
    # it a third time (no ghost retry).
    absent = outcomes[rows[1].row_id]
    assert absent.found is False and portal.queries.count("search_query_adv=ZZ-9") == 1
    assert "3880938-10" not in reader.texts[0]


@pytest.mark.asyncio
async def test_search_address_off_site_turns_bulk_off(monroe_page):
    page, portal = monroe_page
    adapter = adapter_for(portal, extractor=Reader(),
                            vendor_config={"part_url_template": "https://evil.example/?q={part}"})
    for _ in range(2):
        with pytest.raises(BulkSearchUnavailable):
            await adapter.search_many(page, [row("A"), row("B", 3)])
    assert adapter.bulk_unavailable


@pytest.mark.asyncio
async def test_signed_out_search_is_a_session_expiry(monroe_page):
    from sourcing.vendors.base import SessionExpired
    page, portal = monroe_page
    portal.signed_in = False
    with pytest.raises(SessionExpired):
        await adapter_for(portal, extractor=Reader()).search(page, row("TWD-4"))


class _Resp:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"location": location} if location else {}


class _AccountPage:
    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.url = HOST + "/"
        self.request = self

    async def get(self, url, **kw):
        assert kw.get("max_redirects") == 0
        return _Resp(200) if self.signed_in else _Resp(302, HOST + "/login.php?from=account.php")

    async def goto(self, url, **kw):
        self.url = url
        if "action=logout" in url:
            self.signed_in = False

    async def wait_for_load_state(self, *a, **kw):
        return None


@pytest.mark.asyncio
async def test_account_check_and_logout():
    from sourcing.vendors import monroe
    adapter = MonroeAdapter(extractor=Reader())
    assert await adapter._account_state(_AccountPage(True)) is True
    assert await adapter._account_state(_AccountPage(False)) is False
    page = _AccountPage(True)
    assert await adapter.logout(page) is True and page.url == monroe.LOGOUT_URL

    stuck = _AccountPage(True)

    async def stays(url, **kw):
        stuck.url = url
    stuck.goto = stays
    assert await adapter.logout(stuck) is False          # -> the Live View handoff


def test_login_page_comes_from_the_secret_only():
    # Team, 2026-11-04: the secret's url (https://monroeaerospace.com/#/login) is
    # used; the adapter has no login address of its own.
    from types import SimpleNamespace
    cred = SimpleNamespace(secret_key="sourcing-vendor-monroe",
                           url="https://monroeaerospace.com/#/login")
    adapter = MonroeAdapter()
    assert adapter.login_url_candidates(cred) == [
        ("secret sourcing-vendor-monroe url", "https://monroeaerospace.com/#/login")]
    no_url = SimpleNamespace(secret_key="sourcing-vendor-monroe", url="")
    assert adapter.login_url_candidates(no_url) == []     # -> "No usable login URL", no guess


@pytest.mark.asyncio
async def test_variants_table_that_never_shows_is_tried_once_more_then_fetched(monroe_page,
                                                                              monkeypatch):
    from sourcing.vendors import monroe
    monkeypatch.setattr(monroe, "VARIANTS_WAIT_SECONDS", 1)
    page, portal = monroe_page
    portal.draw_table = False
    reader = Reader()
    outcome = await adapter_for(portal, extractor=reader).search(page, row("TWD-4"))
    assert outcome.found
    assert portal.gets.count("/drill-bits/twd-4/") == 2           # its tab, then once more
    text = reader.texts[0]
    assert "In Stock - 30 units available" in text and "Stock table" not in text
    assert "Buy 50 - 99" in text                                   # tab's pop-up read


@pytest.mark.asyncio
async def test_page_no_tab_can_open_comes_from_its_html_then_the_browser(monroe_page, monkeypatch):
    page, portal = monroe_page
    reader = Reader()
    adapter = adapter_for(portal, extractor=reader)

    async def no_tab(_page, _href):
        return None
    monkeypatch.setattr(adapter, "_product_in_tab", no_tab)
    await adapter.search(page, row("TWD-4"))
    assert portal.fetched == ["/drill-bits/twd-4/"]
    text = reader.texts[0]
    assert "Minimum Purchase:\n5 units" in text                  # hidden dl kept in the HTML
    assert "Buy 50 - 99 and pay only $3.50 each" in text           # bulk pop-up kept
    assert "SIMILAR-99" not in text

    portal.fetched.clear()
    reader2 = Reader()
    adapter2 = adapter_for(portal, fetch=False, extractor=reader2)
    monkeypatch.setattr(adapter2, "_product_in_tab", no_tab)
    await adapter2.search(page, row("TWD-4"))
    assert "Minimum order is 5." in reader2.texts[0]               # opened in the browser


@pytest.mark.asyncio
async def test_account_page_redirect_to_order_status_is_signed_in():
    # Run 2026-11-04 16:20: /account.php -> 302 account.php?action=order_status
    # was read as "can't tell" and a good login was tried again.
    class Orders(_AccountPage):
        async def get(self, url, **kw):
            return _Resp(302, HOST + "/account.php?action=order_status")
    adapter = MonroeAdapter(extractor=Reader())
    assert await adapter._account_state(Orders(True)) is True
    assert await adapter._open_login_page(Orders(True), None) is False   # no second login
    assert adapter.signed_in_proof.startswith("account page")


# The 16:53 run (2026-11-04) against its saved page text: what the LLM wrote vs
# what the page states.
REAL_TEXT = """MS28773-04
RETAINER PACKING (MS28773-04)
$1.20 / Unit
In Stock - 3,130 units available
$12.00
Minimum Order Note:
Minimum order is 10. Must order in increments of 1.
Inventory Note:
Need additional stock? Call (877) 751-0435 or email
Industry Standards/Certification:
MS28773
Sold In:
EA"""


def test_page_facts_are_what_the_page_states():
    from sourcing.vendors.monroe import page_facts
    facts = page_facts({"text": REAL_TEXT,
                        "variants": ["Available: 3,130; Unit of Measure: EA; Unit Price: $1.20"],
                        "bulk": ["Buy 100 - 499 and pay only $1.16 each",
                                 "Buy 2500 - 9999 and pay only $0.91 each"]})
    assert facts == {"moq": "10", "tagged_by": "MS28773", "qty": "3130", "price": "1.20",
                     "bulk_shown": False,
                     "breaks": [("100", "499", "1.16"), ("2500", "9999", "0.91")]}
    empty = page_facts({"text": "Industry Standards/Certification:\nSold In:\nEA"})
    assert "tagged_by" not in empty and "moq" not in empty and empty["breaks"] == []


def test_llm_values_the_page_doesnt_state_are_replaced_or_cleared():
    from sourcing.vendors.monroe import page_facts
    adapter = MonroeAdapter()
    link = "https://monroeaerospace.com/o-rings/ms28773-04/"
    adapter._facts = {link: {**page_facts({
        "text": REAL_TEXT, "variants": ["Available: 3,130; Unit of Measure: EA; Unit Price: $1.20"],
        "bulk": ["Buy 100 - 499 and pay only $1.16 each"]}), "sku": "MS2877304"}}
    # What the 16:53 run wrote for it.
    offer = Offer("MS28773-04", "1.20", "USD", "3130", None, "In stock", link + "?searchuuid=x",
                  moq="100", tagged_by="MFR Certificate", trace="Trace To MFR",
                  warehouses="Warehouse: TE\nQty on hand: 2500",
                  price_breaks=[("1", "99", "1.20"), ("100", "499", "1.16")],
                  review="MOQ_NOT_ON_PAGE")
    [kept] = adapter._finish([offer])
    assert (kept.moq, kept.tagged_by, kept.trace, kept.warehouses) == ("10", "MS28773", None, None)
    assert kept.price_breaks == [("100", "499", "1.16")]        # no invented base row
    assert kept.qty_available == "3130" and kept.price == "1.20" and kept.condition == "NE"
    assert kept.lead_time == "In stock" and kept.review is None


def test_offer_for_no_product_read_is_dropped():
    adapter = MonroeAdapter()
    adapter._facts = {}
    assert adapter._finish([Offer("X-1", "1.00", "USD", "5", None, None, "https://x/")]) == []


def test_request_quote_line_gets_no_price_from_the_page():
    adapter = MonroeAdapter()
    link = "https://monroeaerospace.com/drill-bits/attwd-4/"
    adapter._facts = {link: {"breaks": [], "price": "0.50", "qty": "12", "sku": "ATTWD4"}}
    [kept] = adapter._finish([Offer("ATTWD-4", None, None, "12", None, None, link)])
    assert kept.price is None and kept.price_breaks == []


def test_currency_and_increments_come_from_the_page():
    # 17:26 run: "$0.27" only on the page -> no currency -> price breaks without "$".
    from sourcing.vendors.monroe import page_facts
    text = "Currency code of the page: USD\n" + REAL_TEXT.replace(
        "increments of 1.", "increments of 100.")
    facts = page_facts({"text": text})
    assert facts["currency"] == "USD" and facts["increment"] == "100"
    assert "increment" not in page_facts({"text": REAL_TEXT})          # increments of 1
    adapter = MonroeAdapter()
    link = "https://monroeaerospace.com/o-rings/ms28773-04/"
    adapter._facts = {link: {**facts, "sku": "MS2877304"}}
    [kept] = adapter._finish([Offer("MS28773-04", "1.20", None, "3130", None, None, link)])
    assert kept.currency == "USD" and kept.vendor_note == "SOLD IN INCREMENTS OF 100"
