"""AircraftSpruceAdapter against a mock of aircraftspruce.com, built from the
public pages seen 2026-11-05: catalog/pnpages/<part>.php product pages (#dd-pn,
#np "$77.65/Kit", "Part# / MFR Model#", a stock line the page's script draws for
the quantity in the box), "Page Not Found" with HTTP 200, the search box (a POST)
that opens a product page or lists .item.filteredItem cards, and the navbar's
Logout / Sign In link. Skipped if no local Chromium."""
from __future__ import annotations

import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.aircraft_spruce import (
    AircraftSpruceAdapter, product_facts, product_url, rank_by_description, requested_qty,
)
from sourcing.vendors.aircraft_spruce_prompt import AIRCRAFT_SPRUCE_EXTRACT_SYSTEM
from sourcing.vendors.base import SessionExpired

HOST = "https://www.aircraftspruce.com"
NAV = """<div class="nav"><a href="/">Aircraft Spruce</a>{auth}<a href="/cart.php">Cart 0</a>
<form action="/search/search.php" method="post"><input type="text" id="search_text" name="s"
 placeholder="Search by part #, keyword, phrase, etc."></form></div>"""
SIGNED_IN = '<a href="/logout.php">Logout</a>'
SIGNED_OUT = '<a href="/account.html">Sign In</a>'
# Product pages as seen 2026-11-05 (01-08800, 10-05620, AN470AD-4-4). QP-1 has a
# price per quantity (what a signed-in account may show; not seen yet).
PRODUCTS = {
    "01-08800": dict(title="Liquid X-30 Foam Quart Kit X-40", price=77.65, unit="Kit",
                     model="25405", avail=6),
    "10-05620": dict(title="Electronics International Xo-8 Extension 8Foot Overlap Connectors",
                     price=52.75, unit="Each", model="XO-8", avail=0),
    "AN470AD-4-4": dict(title="Solid Aluminum Rivets AN470Ad-4-4", price=46.50, unit="Pound",
                        model="15700-1", avail=86.6),
    "04-00359": dict(title="AN426Ad Rivet Assortment Csk-Hard", price=71.50, unit="Kit",
                     model="AN426AD", avail=10),
    "QP-1": dict(title="Quantity Priced Part", price=10.00, unit="Each", model="", avail=100,
                 qty_price=(10, 8.00)),
}
# The site's own part numbers for addresses it answers with another product.
SAME_PAGE = {"MS20470AD4-4": "AN470AD-4-4"}
PRODUCT = """<html><head><title>{title}</title></head><body>{nav}
<div class="breadcrumb">Home / Composite Materials / Foam / {title}</div>
<h1>{title}</h1><div>5 Reviews 4 Answered Questions</div>
<div class="prPrice"><div id='np'>${price:.2f}/{unit} </div><div id='gp'></div></div>
<form id='dd_order_form'><input type='hidden' id='dd-pn' name='p' value='{pn}'>
<fieldset><legend>Quantity</legend><input type="tel" id='dd-qty' name="q" value="1"></fieldset></form>
<button>Add to Cart</button><a href="/wishlist.php">Add to Wishlist</a>
<div class="prShortDesc"><div class="modelstatus"><div class="prModel">Part# {pn}{model}</div>
<div class="prStockStatus"></div><div class="prDelivery"></div></div>
<div>FREE Shipping</div><div>30 Day Risk Free Purchase</div></div>
<div id="quickshop"><div class="prStockStatus" id="qsStockStatus"></div></div>
<h2>Overview</h2><p>Mfg test reports are available for an additional charge per line item.</p>
<h3>Frequently Purchased With</h3><div class="other">BID FIBERGLASS 38 RA7725 $10.50</div>
<h2>Reviews</h2><p>Great product</p>
<footer>Prices subject to change without notice. Invoice currency USD.</footer>
<script>
const AVAIL = {avail}, PRICE = {price}, BREAK = {brk};
function draw() {{
  const q = parseFloat(document.querySelector('#dd-qty').value) || 1;
  const st = document.querySelector('.modelstatus .prStockStatus');
  const dl = document.querySelector('.modelstatus .prDelivery');
  if (BREAK && q >= BREAK[0]) document.querySelector('#np').textContent = '$' + BREAK[1].toFixed(2) + '/Each';
  if (AVAIL >= q) {{ st.textContent = ' In Stock'; dl.textContent = 'Expected to Ship: Thursday, Nov. 5'; }}
  else if (AVAIL > 0) {{ st.textContent = 'Partial Stock';
    dl.innerHTML = AVAIL + ' Expected to ship: Thursday, Nov. 5<br>' + (q - AVAIL) + ' Backordered Expected to ship: TBD'; }}
  else {{ st.textContent = 'No Stock'; dl.textContent = 'Expected to Ship: Friday, Dec. 4'; }}
}}
setTimeout(draw, 200);
document.querySelector('#dd-qty').addEventListener('change', () => setTimeout(draw, 100));
</script></body></html>"""
NOT_FOUND = """<html><body>{nav}<h1>Page Not Found</h1><p>Error 404</p>
<p>We're sorry, the page you requested, /catalog/pages/, is no longer valid.</p></body></html>"""
CARD = """<div class="item filteredItem"><div class="prTitle"><a href="/catalog/pnpages/{pn}.php">{title}</a></div>
<div class="prPrice discount"><strong>{price}</strong></div>
<div class="prModel"><span tpl-html="pn">{pn}</span><span> | <span tpl-html="mpn">{mpn}</span></span></div>
<div class="prStockStatus">In Stock</div>{series}<a class="quickshop">Add to Cart</a></div>"""
RESULTS = """<html><body>{nav}<h2>{n} search results for "{term}"</h2><div>Sort by</div>
<div class="results">{cards}</div></body></html>"""


def card(pn, title, mpn="", price="$71.50", series=False):
    return CARD.format(pn=pn, title=title, mpn=mpn, price=price,
                       series='<span>Product Series Page</span>' if series else "")


SEARCHES = {
    "XO-8": [card("10-05621", "XO-80 Probe", "XO-80"),
             card("10-05620", "Electronics International Xo-8 Extension", "XO-8", "$52.75")],
    "Solid Rivet Assortment": [
        card("SERIES-1", "Solid Rivet Assortment", price="From $60.25 to $76.25", series=True),
        card("08-11111", "Rivet Gun Kit"),
        card("04-00359", "AN426Ad Rivet Assortment Csk-Hard", "AN426AD")],
}


class MockSpruce:
    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.gets: list[str] = []
        self.searches: list[str] = []

    def nav(self):
        return NAV.format(auth=SIGNED_IN if self.signed_in else SIGNED_OUT)

    async def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        path = url.path
        self.gets.append(f"{req.method} {path}")
        html = lambda body: route.fulfill(status=200, content_type="text/html", body=body)  # noqa: E731
        if path == "/search/search.php" and req.method == "POST":
            term = parse_qs(req.post_data or "").get("s", [""])[0]
            self.searches.append(term)
            if term == "JUMP-1":          # the site's search opens a product page itself
                # Not a 302: Playwright doesn't route a fulfilled redirect's target, so
                # the browser would open the real site.
                await html("<script>location.replace('/catalog/pnpages/AN470AD-4-4.php')</script>")
                return
            cards = SEARCHES.get(term, [])
            await html(RESULTS.format(nav=self.nav(), n=len(cards), term=term, cards="".join(cards)))
        elif path.startswith("/catalog/pnpages/"):
            pn = path.rsplit("/", 1)[-1].removesuffix(".php").upper()
            pn = SAME_PAGE.get(pn, pn)
            if pn not in PRODUCTS:
                await html(NOT_FOUND.format(nav=self.nav()))
                return
            p = PRODUCTS[pn]
            await html(PRODUCT.format(
                nav=self.nav(), pn=pn, title=p["title"], price=p["price"], unit=p["unit"],
                model=f"<br>MFR Model# {p['model']}" if p["model"] else "", avail=p["avail"],
                brk=list(p["qty_price"]) if p.get("qty_price") else "null"))
        elif path == "/logout.php":
            self.signed_in = False
            await html(f"<html><body>{self.nav()}<p>You are signed out</p></body></html>")
        else:
            await html(f"<html><body>{self.nav()}<p>Home</p></body></html>")


class Reader:
    """Stands in for the LLM: one offer per product block, as read."""

    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        links = re.findall(r"--- Product \d+: (\S+)", page_text)
        offers = [Offer(part_number, "1.00", None, "In stock", None, None, link) for link in links]
        return bool(offers), "results", offers, LlmUsage(calls=1)

    async def not_sold_quote(self, *_, **__):
        return None, LlmUsage()


def row(pn, qty="", description="", n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="Aircraft Spruce", part_number=pn,
                      qty_needed=qty, part_description=description)


@pytest_asyncio.fixture
async def spruce_page():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockSpruce()
    context = await browser.new_context()
    await context.route(f"{HOST}/**", portal.handle)
    page = await context.new_page()
    await page.goto(f"{HOST}/")
    yield page, portal
    await browser.close()
    await pw.stop()


def test_registered_for_the_sheet_and_records():
    for name in ("aircraft_spruce", "aircraftspruce", "spruce"):
        assert adapter_class(name) is AircraftSpruceAdapter
    assert adapter_class("x", {"adapter": "aircraft_spruce"}) is AircraftSpruceAdapter


def test_product_address_is_the_teams_format():
    assert product_url("03-28010") == f"{HOST}/catalog/pnpages/03-28010.php"
    assert product_url("MS 20470 AD4-4") == f"{HOST}/catalog/pnpages/MS%2020470%20AD4-4.php"


def test_requested_quantity():
    assert requested_qty("5") == 5 and requested_qty("10 EA") == 10
    assert requested_qty("0.25") == 0.25          # rivets by the pound
    assert requested_qty("") is None and requested_qty("0") is None


@pytest.mark.asyncio
async def test_in_stock_for_the_requested_quantity(spruce_page):
    page, portal = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(page, row("01-08800", "5"))
    assert outcome.found and portal.searches == []                  # its address, no search
    text = reader.texts[0]
    assert "Product 1: https://www.aircraftspruce.com/catalog/pnpages/01-08800.php" in text
    assert "Part#: 01-08800" in text and "MFR Model#: 25405" in text
    assert "Quantity typed into the Quantity box: 5" in text
    assert "Stock line for quantity 5: In Stock" in text
    assert "Is the requested part 01-08800: yes" in text
    assert "Frequently Purchased With" not in text and "BID FIBERGLASS" not in text
    assert "Great product" not in text and "Cart 0" not in text      # reviews, navbar out
    assert reader.systems[0] == AIRCRAFT_SPRUCE_EXTRACT_SYSTEM
    offer = outcome.offers[0]
    assert (offer.price, offer.uom, offer.currency) == ("77.65", "Kit", "USD")
    assert offer.qty_available == "5" and offer.qty_price == "77.65"
    assert "IN STOCK FOR THE REQUESTED QTY 5" in offer.vendor_note
    assert offer.listing == "requested" and offer.condition == "NE"
    assert not any("cart" in g or "wishlist" in g or "logout" in g for g in portal.gets)


@pytest.mark.asyncio
async def test_partial_stock_takes_what_ships_now(spruce_page):
    page, _ = spruce_page
    outcome = await AircraftSpruceAdapter(extractor=Reader()).search(page, row("01-08800", "50"))
    offer = outcome.offers[0]
    assert offer.qty_available == "6"
    assert "PARTIAL STOCK FOR QTY 50" in offer.vendor_note and "44 BACKORDERED" in offer.vendor_note


@pytest.mark.asyncio
async def test_no_stock_is_out_of_stock(spruce_page):
    page, _ = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(page, row("10-05620", "2"))
    assert outcome.offers[0].qty_available == "Out of stock"
    assert "Delivery line: Expected to Ship: Friday, Dec. 4" in reader.texts[0]


@pytest.mark.asyncio
async def test_price_shown_for_the_requested_quantity(spruce_page):
    page, _ = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(page, row("QP-1", "10"))
    offer = outcome.offers[0]
    assert offer.price == "10.00" and offer.qty_price == "8.00"
    assert "Price shown for that quantity: $8.00/Each" in reader.texts[0]


@pytest.mark.asyncio
async def test_manufacturer_number_found_by_the_search_box(spruce_page):
    page, portal = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(page, row("XO-8", "1"))
    assert portal.searches == ["XO-8"]                            # address said Page Not Found
    assert reader.texts[0].count("--- Product ") == 1 and "10-05621" not in reader.texts[0]
    offer = outcome.offers[0]
    assert offer.part_number == "XO-8" and offer.listing == "requested"
    assert offer.vendor_note == "AIRCRAFT SPRUCE PART# 10-05620"     # no stock: no qty note
    assert offer.qty_available == "Out of stock"


@pytest.mark.asyncio
async def test_another_part_number_is_other(spruce_page):
    page, portal = spruce_page
    # The address itself answers with AN470AD-4-4 (seen 2026-11-05).
    outcome = await AircraftSpruceAdapter(extractor=Reader()).search(page, row("MS20470AD4-4", "100"))
    offer = outcome.offers[0]
    assert offer.listing == "other" and offer.part_number == "AN470AD-4-4"
    assert offer.qty_available == "86.6"                          # sold by the pound
    # The site's search opening a product page itself: the same.
    outcome = await AircraftSpruceAdapter(extractor=Reader()).search(page, row("JUMP-1"))
    assert portal.searches == ["JUMP-1"]
    assert outcome.offers[0].listing == "other" and outcome.offers[0].part_number == "AN470AD-4-4"


@pytest.mark.asyncio
async def test_not_found_needs_no_llm(spruce_page):
    page, portal = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(page, row("NOPE-1", "3"))
    assert outcome.found is False and reader.texts == []
    assert portal.searches == ["NOPE-1"]


@pytest.mark.asyncio
async def test_description_search_when_the_part_number_finds_nothing(spruce_page):
    page, portal = spruce_page
    reader = Reader()
    outcome = await AircraftSpruceAdapter(extractor=reader).search(
        page, row("NOPE-1", "3", description="Solid Rivet Assortment"))
    assert portal.searches == ["NOPE-1", "Solid Rivet Assortment"]
    text = reader.texts[0]
    assert "04-00359" in text and "SERIES-1" not in text and "08-11111" not in text
    offer = outcome.offers[0]
    assert offer.listing == "other" and offer.review == "FOUND_BY_DESCRIPTION"
    assert offer.part_number == "04-00359"


@pytest.mark.asyncio
async def test_signed_out_product_page_is_a_session_expiry(spruce_page):
    page, portal = spruce_page
    portal.signed_in = False
    with pytest.raises(SessionExpired):
        await AircraftSpruceAdapter(extractor=Reader()).search(page, row("01-08800", "5"))


@pytest.mark.asyncio
async def test_navbar_logout_link_is_the_signed_in_check_and_the_logout(spruce_page):
    page, portal = spruce_page
    adapter = AircraftSpruceAdapter(extractor=Reader())
    assert await adapter.is_authenticated(page) is True
    assert await adapter.logout(page) is True and portal.signed_in is False
    assert await adapter.is_authenticated(page) is False


def test_login_page_comes_from_the_secret_only():
    cred = SimpleNamespace(secret_key="sourcing-vendor-aircraft-spruce",
                           url="https://www.aircraftspruce.com/account.html")
    adapter = AircraftSpruceAdapter()
    assert adapter.login_url_candidates(cred) == [
        ("secret sourcing-vendor-aircraft-spruce url", "https://www.aircraftspruce.com/account.html")]
    assert adapter.login_url_candidates(SimpleNamespace(secret_key="s", url="")) == []


def test_line_total_is_never_the_price_at_quantity():
    facts = product_facts({"price_at_1": "$5.00/Each", "price": "$50.00/Each", "stock": "In Stock"},
                          10)
    assert facts["price"] == "5.00" and "qty_price" not in facts
    assert facts["qty"] == "10"


def test_description_ranking_skips_series_and_unrelated_cards():
    cards = [{"title": "Solid Rivet Assortment", "href": "x", "series": True},
             {"title": "Rivet Gun", "href": "y", "series": False},
             {"title": "AN470A Rivet Assortment Soft", "href": "z", "series": False}]
    assert [c["href"] for c in rank_by_description(cards, "Solid Rivet Assortment", 3)] == ["z"]


# /account.html as seen 2026-11-05: "User ID / Dealer #" has no label or placeholder,
# and the footer's newsletter box is the page's only input[type=email].
LOGIN_PAGE = """<html><body>{nav}
<form name="user_login" action="/account.html" method="post">
<fieldset><legend>User ID / Dealer #</legend><input type="text" name="email" id="user_email"></fieldset>
<fieldset><legend>Password</legend><input type="password" name="psw" id="user_psw"></fieldset>
<button class="g-recaptcha" id="login_button" data-size="invisible">Sign In</button></form>
<form id="form"><input type="email" id="EmailAddress" name="EMAIL" placeholder="Email Address">
<input type="submit" id="submitnews"></form></body></html>"""


@pytest.mark.asyncio
async def test_login_types_the_user_id_not_the_newsletter_box(spruce_page):
    from sourcing.vendors import web
    page, portal = spruce_page
    await page.set_content(LOGIN_PAGE.format(nav=portal.nav()))
    adapter = AircraftSpruceAdapter(extractor=Reader())
    assert adapter.human_typing                                   # invisible reCAPTCHA
    user = await adapter._pick(page, "username_input", web.USERNAME_SELECTORS)
    password = await adapter._pick(page, "password_input", web.PASSWORD_SELECTORS)
    submit = await adapter._pick_button(page, "submit_button", web.SUBMIT_SELECTORS)
    assert await page.locator(user).get_attribute("id") == "user_email"
    assert await page.locator(password).get_attribute("id") == "user_psw"
    assert await page.locator(submit).get_attribute("id") == "login_button"
    # Saved from an earlier run (the newsletter box): the pinned form still wins.
    stale = AircraftSpruceAdapter(selectors={"username_input": "input[type='email']"})
    assert stale.selectors["username_input"].endswith("input#user_email")


# Google's challenge frame: in the page all along, hidden until Google shows it.
CHALLENGE = ('<div style="visibility: {vis}; position: absolute;"><iframe title="recaptcha challenge '
             'expires in two minutes" width="400" height="580" '
             'src="https://www.google.com/recaptcha/api2/bframe?k=x"></iframe></div>')


@pytest.mark.asyncio
async def test_image_challenge_goes_to_a_person_not_a_retry(spruce_page, monkeypatch):
    from sourcing.vendors import aircraft_spruce
    from sourcing.vendors.base import MfaRequired
    page, portal = spruce_page
    await page.context.route("https://www.google.com/**",          # never the real Google
                             lambda r: r.fulfill(status=200, body="<p>images</p>"))
    monkeypatch.setattr(aircraft_spruce, "CHALLENGE_WAIT_SECONDS", 1)
    monkeypatch.delenv("DEV_ALLOW_CAPTCHA_SOLVING", raising=False)
    adapter = AircraftSpruceAdapter(extractor=Reader())
    await page.set_content(LOGIN_PAGE.format(nav=portal.nav()) + CHALLENGE.format(vis="visible"))
    with pytest.raises(MfaRequired) as err:
        await adapter._after_submit(page, None, totp_tried=False)
    assert err.value.method == "captcha"
    # Hidden (no challenge asked): not a person's job.
    await page.set_content(LOGIN_PAGE.format(nav=portal.nav()) + CHALLENGE.format(vis="hidden"))
    assert await adapter._image_challenge(page) is False
    # Off the sign-in form (signed in): no waiting for a challenge.
    await page.set_content(f"<html><body>{portal.nav()}<p>My Account</p></body></html>")
    assert await adapter._image_challenge(page) is False


@pytest.mark.asyncio
async def test_dev_switch_waits_for_the_solver_then_a_person(spruce_page, monkeypatch):
    # REMOVE-AFTER-CAPTCHA-TRIAL (2026-11-05): DEV_ALLOW_CAPTCHA_SOLVING=1.
    from sourcing.vendors import aircraft_spruce
    from sourcing.vendors.base import MfaRequired
    page, portal = spruce_page
    await page.context.route("https://www.google.com/**",
                             lambda r: r.fulfill(status=200, body="<p>images</p>"))
    monkeypatch.setattr(aircraft_spruce, "CHALLENGE_WAIT_SECONDS", 1)
    monkeypatch.setattr(aircraft_spruce, "SOLVER_WAIT_SECONDS", 2)
    monkeypatch.setenv("DEV_ALLOW_CAPTCHA_SOLVING", "1")
    adapter = AircraftSpruceAdapter(extractor=Reader())
    html = LOGIN_PAGE.format(nav=portal.nav()) + CHALLENGE.format(vis="visible")
    # The solver clears it (frame hidden after ~1s): no person asked.
    await page.set_content(html.replace("<div style", "<div id='c' style"))
    await page.evaluate("setTimeout(() => document.getElementById('c')"
                        ".style.visibility = 'hidden', 800)")
    assert await adapter._image_challenge(page) is True
    assert await adapter._solver_cleared(page) is True
    # Still shown when the wait ends: a person, as before.
    await page.set_content(html)
    with pytest.raises(MfaRequired):
        await adapter._after_submit(page, None, totp_tried=False)
