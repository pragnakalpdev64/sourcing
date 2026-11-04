"""AarAdapter against an invented mock of AAR's shop (Magento), from the team's
description (2026-11-03): catalogsearch/result/?q=<part> (several parts joined
by %2C+), product cards with price or RFQ, stock, condition, lead time. The
real pages haven't been saved yet. Skipped if no local Chromium."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.aar import AarAdapter, search_query
from sourcing.vendors.aar_prompt import AAR_EXTRACT_SYSTEM
from sourcing.vendors.base import BulkSearchUnavailable

HOST = "https://shop.aarcorp.com"
HEAD = """<html><body><header><a href="/customer/account/logout/">Sign Out</a>
<a href="/checkout/cart/">My Cart</a></header>"""
CARD = """<li class="product-item"><div class="product-item-info">
<a class="product-item-link" href="/{slug}.html">{pn}</a><p>{desc}</p>
<p>Condition: NE</p>{price}<p>{stock}</p><p>Lead Time: 30 days</p>
<img alt="8130-3 tag" src="/media/tag.png"><button>Add to Cart</button></div></li>"""
RESULTS = HEAD + """<div class="toolbar-amount">Items {first}-{last} of 3</div>
<ol class="products list">{cards}</ol>{pager}</body></html>"""
NEXT = """<div class="pages"><ul><li class="pages-item-next">
<a class="action next" href="/catalogsearch/result/index/?p=2&q={q}">Next</a></li></ul></div>"""
NOTHING = HEAD + """<div class="message notice">Your search returned no results.</div></body></html>"""


def card(pn, desc="ELEMENT", price="<span class='price'>$412.00</span>", stock="In Stock: 6"):
    return CARD.format(slug=pn.lower(), pn=pn, desc=desc, price=price, stock=stock)


class MockAar:
    def __init__(self):
        self.gets: list[str] = []
        self.queries: list[str] = []

    async def handle(self, route):
        url = urlparse(route.request.url)
        self.gets.append(url.path)
        q = parse_qs(url.query).get("q", [""])[0]
        if url.path.startswith("/catalogsearch/result"):
            self.queries.append(url.query)
            if q == "3880938-1":
                # A similar number first (search matches words), the part on page 2.
                if parse_qs(url.query).get("p") == ["2"]:
                    body = RESULTS.format(first=3, last=3, pager="",
                                          cards=card("3880938-1"))
                else:
                    body = RESULTS.format(first=1, last=2, pager=NEXT.format(q=q),
                                          cards=card("3880938-10", price="<button>RFQ</button>",
                                                     stock="Out of Stock") + card("3880938-2"))
            elif "," in q:
                body = RESULTS.format(first=1, last=2, pager="", cards="".join(
                    card(p.strip()) for p in q.split(",")))
            else:
                body = NOTHING
        else:
            body = HEAD + "<p>Dashboard</p></body></html>"
        await route.fulfill(status=200, content_type="text/html", body=body)


class Reader:
    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []
        self.bulk_parts: list[list[str]] = []

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        found = "Card 1:" in page_text
        offers = [Offer(part_number, "412.00", "USD", "6", "NE", "30 days", page_url)] \
            if found else []
        return found, "results" if found else "no_results", offers, LlmUsage(calls=1)

    async def extract_offers_bulk(self, *, page_text, page_url, parts, system=""):
        self.texts.append(page_text)
        self.bulk_parts.append([p["part_number"] for p in parts])
        found = {p["part_number"]: [Offer(p["part_number"], "412.00", "USD", "6", "NE", None,
                                          page_url)]
                 for p in parts if p["part_number"] in page_text}
        return "results", found, LlmUsage(calls=1)

    async def not_sold_quote(self, *_, **__):
        return None, LlmUsage()


def row(pn, n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="AAR", part_number=pn)


@pytest_asyncio.fixture
async def aar_page():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockAar()
    page = await browser.new_page()
    await page.route(f"{HOST}/**", portal.handle)
    await page.goto(f"{HOST}/customer/account/")
    yield page, portal
    await browser.close()
    await pw.stop()


def test_search_query_is_the_teams_format():
    assert search_query(["44066 O-RING"]) == "44066+O-RING"
    assert search_query(["C100-013080"]) == "C100-013080"
    assert (search_query(["44066 O-RING", "1381-1176 RETAINER", "3880938-1 ELEMENT"])
            == "44066+O-RING%2C+1381-1176+RETAINER%2C+3880938-1+ELEMENT")


def test_registered_for_the_sheet_and_records():
    assert adapter_class("aar") is AarAdapter
    assert adapter_class("aar_corp") is AarAdapter
    assert adapter_class("x", {"adapter": "aar"}) is AarAdapter


@pytest.mark.asyncio
async def test_one_part_reads_every_results_page_requested_card_first(aar_page):
    page, portal = aar_page
    reader = Reader()
    outcome = await AarAdapter(extractor=reader).search(page, row("3880938-1"))
    assert outcome.found
    assert portal.queries == ["q=3880938-1", "p=2&q=3880938-1"]
    assert not any("cart" in g or "logout" in g for g in portal.gets[1:])
    text = reader.texts[0]
    assert "2 results page(s) read" in text
    # 3880938-1 is on page 2 but read first; the similar numbers still follow
    first = text.split("--- Card 2:")[0]
    assert "Card 1: https://shop.aarcorp.com/3880938-1.html" in first
    assert "3880938-10" in text and "RFQ" in text and "Out of Stock" in text
    assert "8130-3 tag" in first                       # icon alt text kept with its card
    assert "My Cart" not in text                       # only the cards
    assert reader.systems[0] == AAR_EXTRACT_SYSTEM


@pytest.mark.asyncio
async def test_no_results_reads_the_page_text(aar_page):
    page, _ = aar_page
    reader = Reader()
    outcome = await AarAdapter(extractor=reader).search(page, row("XYZ-1"))
    assert not outcome.found
    assert "no product cards" in reader.texts[0]
    assert "Your search returned no results." in reader.texts[0]


@pytest.mark.asyncio
async def test_several_parts_in_one_search(aar_page):
    page, portal = aar_page
    reader = Reader()
    rows = [row("44066", 2), row("1381-1176", 3), row("C100-013080", 4)]
    outcomes, usage = await AarAdapter(extractor=reader).search_many(page, rows)
    assert portal.queries == ["q=44066%2C+1381-1176%2C+C100-013080"]
    assert reader.bulk_parts == [["44066", "1381-1176", "C100-013080"]]
    assert set(outcomes) == {r.row_id for r in rows}
    assert usage.calls == 1


@pytest.mark.asyncio
async def test_multi_part_address_off_site_turns_bulk_off(aar_page):
    page, _ = aar_page
    adapter = AarAdapter(extractor=Reader(),
                         vendor_config={"part_url_template": "https://evil.example/?q={part}"})
    for _ in range(2):
        with pytest.raises(BulkSearchUnavailable):
            await adapter.search_many(page, [row("A"), row("B", 3)])
    assert adapter.bulk_unavailable


LANDING = """<html><body><header><div class="minicart-wrapper" id="myLogin">
<span class="miniuser-link" style="display:inline-block;width:30px;height:30px"
 onclick="document.getElementById('menu').style.display='block'"></span></div>
<div id="menu" style="display:none"><a href="/customer/account/login">Sign In or Register</a></div>
<ul class="header links" style="display:none"><li><a href="/customer/account/login/referer/x/">Sign In</a></li></ul>
</header><p>The right part. Right now.</p></body></html>"""
LOGIN_FORM = """<html><body><form id="login-form"><input id="email" type="email">
<input id="pass" type="password"><button id="send2">Sign In</button></form></body></html>"""


@pytest.mark.asyncio
async def test_sign_in_goes_landing_avatar_then_sign_in_link():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    gets: list[str] = []

    async def handle(route):
        path = urlparse(route.request.url).path
        gets.append(path)
        body = LOGIN_FORM if path.startswith("/customer/account/login") else LANDING
        await route.fulfill(status=200, content_type="text/html", body=body)
    try:
        page = await browser.new_page()
        await page.route(f"{HOST}/**", handle)
        adapter = AarAdapter(extractor=Reader())
        assert await adapter._open_login_page(page, None) is True
        assert gets[:2] == ["/", "/customer/account/login"]   # never the login page directly
        assert "login_page_url" not in adapter.learned        # next run starts on the landing page too
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_login_fields_typed_one_key_at_a_time(monkeypatch):
    # AAR, testing 2026-11-04: reCAPTCHA failed after an instant fill, so AAR types
    # like a person (one key event per character); other vendors still fill at once.
    import sourcing.vendors.generic as generic
    real_sleep = asyncio.sleep
    monkeypatch.setattr(generic.asyncio, "sleep", lambda s: real_sleep(0))
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    try:
        page = await browser.new_page()
        await page.set_content(LOGIN_FORM)
        await page.evaluate("""window.keys = {};
            for (const id of ['email', 'pass'])
              document.getElementById(id).addEventListener('keydown',
                () => window.keys[id] = (window.keys[id] || 0) + 1);""")
        adapter = AarAdapter(extractor=Reader())
        assert adapter.human_typing is True
        await adapter._type_login_field(page, "#email", "me@x.com")
        await adapter._type_login_field(page, "#pass", "s3cret!")
        assert await page.input_value("#email") == "me@x.com"
        assert await page.input_value("#pass") == "s3cret!"
        # One key per character, plus Shift for '@' and '!' (as on a real keyboard).
        assert await page.evaluate("window.keys") == {"email": 9, "pass": 8}

        plain = AarAdapter(extractor=Reader(), vendor_config={"human_typing": False})
        await plain._type_login_field(page, "#email", "other@x.com")
        assert await page.input_value("#email") == "other@x.com"
        assert await page.evaluate("window.keys.email") == 9     # filled, no key events
    finally:
        await browser.close()
        await pw.stop()


class _Resp:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"location": location} if location else {}


class _AccountPage:
    """Only what the AAR account check and logout use: page.request.get (Magento
    answers the account page with 200 signed in, a redirect to login signed out)."""
    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.url = "https://shop.aarcorp.com/"
        self.gets: list[str] = []
        self.request = self

    async def get(self, url, **kw):
        assert kw.get("max_redirects") == 0
        self.gets.append(url)
        if self.signed_in:
            return _Resp(200)
        return _Resp(302, "https://shop.aarcorp.com/customer/account/login/")

    async def goto(self, url, **kw):
        self.url = url
        if url.endswith("/customer/account/logout/"):
            self.signed_in = False

    async def wait_for_load_state(self, *a, **kw):
        return None


@pytest.mark.asyncio
async def test_account_check_reads_magento_redirect():
    # 2026-11-04: a login that went through couldn't be confirmed (logout link only
    # in the avatar menu); the account page tells without touching the open page.
    adapter = AarAdapter(extractor=Reader())
    assert await adapter._account_state(_AccountPage(signed_in=True)) is True
    assert await adapter._account_state(_AccountPage(signed_in=False)) is False


@pytest.mark.asyncio
async def test_logout_opens_magento_logout_and_confirms():
    from sourcing.vendors import aar
    adapter = AarAdapter(extractor=Reader())
    page = _AccountPage(signed_in=True)
    assert await adapter.logout(page) is True
    assert page.url == aar.LOGOUT_URL and page.signed_in is False
    assert await adapter.person_finished_logout(page) is True


@pytest.mark.asyncio
async def test_logout_not_confirmed_when_still_signed_in(monkeypatch):
    adapter = AarAdapter(extractor=Reader())
    page = _AccountPage(signed_in=True)

    async def stays(url, **kw):       # the logout address did nothing
        page.url = url
    page.goto = stays
    assert await adapter.logout(page) is False     # -> the Live View handoff, never a silent close


@pytest.mark.asyncio
async def test_home_page_after_submit_is_signed_in_when_account_page_opens(monkeypatch):
    # 2026-11-04 11:01: no CAPTCHA, AAR's home page after submit, "LLM says unknown".
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    try:
        page = await browser.new_page()
        await page.set_content("<html><body><div id='myLogin'><span class='miniuser-link'>"
                               "</span></div><h1>Welcome to AAR</h1></body></html>")
        adapter = AarAdapter(extractor=Reader())
        states = iter([True])

        async def account(_page):
            return next(states)
        monkeypatch.setattr(adapter, "_account_state", account)
        await adapter._after_submit(page, None, totp_tried=False)
        assert adapter.signed_in_proof.startswith("account page")

        await page.set_content("<html><body><p>reCAPTCHA verification failed.</p>"
                               "</body></html>")
        from sourcing.vendors.base import CaptchaFailed
        with pytest.raises(CaptchaFailed):           # CAPTCHA wording still wins
            await adapter._after_submit(page, None, totp_tried=False)
    finally:
        await browser.close()
        await pw.stop()
