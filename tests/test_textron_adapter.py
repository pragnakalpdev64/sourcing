"""TextronAdapter against a mock of ww2.txtav.com, built from the public pages seen
2026-11-12: PartsDetail/<part> blocks (hidden [n].Uom / MinOrder / iMROCondition /
ErrorMsg / Substitutes fields, YourPriceUS_n), "Part Not Found." with HTTP 200, the
Multiple Part Search form (#SearchRows, Add Another, POST then a redirect to
PartsDetail?PageType=Multiple), the login form whose Log In button the page's script
enables after reCAPTCHA v3, and a 2FA code page. What a signed-in block shows is a
guess ("Your Price", "12 Available") until the first live run. Skipped if no local
Chromium."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.base import LoginBlocked, MfaRequired, SessionExpired
from sourcing.vendors.registry import VendorRegistry
from sourcing.vendors.textron import TextronAdapter, block_section, detail_url, requested_qty
from sourcing.vendors.textron_prompt import TEXTRON_EXTRACT_SYSTEM

HOST = "https://ww2.txtav.com"
PARTS = {
    "A-7079B-24": dict(desc="LAMP DEICE LIGHT ASSEMBLY", uom="EA", us="45.51", eur="40.55",
                       min=1, lead="Contact customer support for availability", stock=160,
                       notes="PRICE VALID UNTIL STOCK DEPLETED",
                       subs=[("Cessna Alternate", "34-0070371-01", "FOR CESSNA APPLICATION ONLY"),
                             ("Beech-One-Way Supersedure", "WL-L7079-24",
                              "SEE TECHNICAL PUBLICATIONS FOR APPLICATION")]),
    "MS20470AD4-4": dict(desc="RIVET", uom="LB", us="81.95", eur="73.02", min=5,
                         lead="Order today with estimated availability 12/27/2026", subs=[],
                         stock=0, notes="THIS ITEM IS SOLD BY THE POUND"),
    # From the 19-part run, 2026-11-12.
    "101-320234-3": dict(desc="PLACARD- AIR INSTRUMENT IDENT", uom="EA", us="0", eur="0", min=1,
                         lead="Contact customer support for availability.", subs=[], stock=None,
                         notes="Extended Inactivity - This part has had no recent activity which may "
                               "result in extended quote timing<br>PLEASE CONTACT US FOR ASSISTANCE "
                               "WITH PRICING."),
    "M83461-1-122": dict(desc="PACKING", uom="EA", us="0", eur="0", min=1, stock=None,
                         lead="Contact customer support for availability.",
                         notes="NO LONGER AVAILABLE.<br>PLEASE SEE OPTIONS BELOW.",
                         subs=[("Cessna & Beech-One Way Supersedure", "M83461/1-122",
                                "REPLACEMENT FOR CESSNA &amp; BEECH APPLICATIONS")]),
    "6839": dict(desc="LAMP", uom="EA", us="1.42", eur="1.26", min=10, stock=7440, subs=[],
                 lead="Order today with estimated availability 6/26/2026",
                 notes="", extra="<li>Must be a Multiple of: 10</li><li>Maximum Shelf Life: 12 Months</li>"),
    "C40187": dict(desc="BLOCK ASSEMBLY", uom="EA", us="1760.63", eur="0", min=1, stock=3, subs=[],
                   lead="Contact customer support for availability", notes="",
                   ships="Columbus, GA", extra="<li>Hazardous Material</li>"),
}
HEADER_IN = '<a class="profile" href="#">Hello, Jane</a><a onclick="logout()">Log Off</a>'
HEADER_OUT = '<a class="profile" href="#">Hello, Log In!</a><a onclick="GoToLogin()">Log In</a>'
PAGE = """<html><head><title>{title}</title></head><body>
<form action="/Account/LogOff" id="logoutForm" method="post"><input name="__RequestVerificationToken" type="hidden" value="t"></form>
<header>{header}<span>0 items</span></header>{body}
<div>Service Center Stock</div><div>Location</div><div>On Hand Quantity</div>
<footer>&copy; 2026 Textron Aviation</footer>
<script>function logout() {{ document.getElementById('logoutForm').submit(); }}</script></body></html>"""


def block(n, pn, signed_in=True):
    p = PARTS.get(pn)
    if p is None:
        hidden = (f'<input id="ProductPartNumber_{n}" name="[{n}].PartNumber" type="hidden" value="{pn}">'
                  f'<input name="[{n}].ErrorMsg" type="hidden" value="Part Not Found.">'
                  f'<input name="[{n}].Uom" type="hidden" value="">')
        return (f'<div class="col-xs-12 col-sm-8 col-lg-9 pb-2">{hidden}<span>TEXTRON AVIATION PART '
                f'NUMBER</span><h1>{pn}</h1><p id="partDescription_{n}"></p></div>'
                f'<div class="col-xs-12 col-sm-4 col-lg-3 pb-2"><a>Remove Item</a>'
                f'<div>Part Not Found.</div></div><div id="RecommendedItemsDiv_{n}"></div>'
                f'<div class="col-xs-12"><hr></div>')
    subs = "".join(
        f'<input name="[{n}].Substitutes[{k}].Substitle" type="hidden" value="{t}">'
        f'<input name="[{n}].Substitutes[{k}].PartNumber" type="hidden" value="{s}">'
        f'<input name="[{n}].Substitutes[{k}].SubDesc" type="hidden" value="{d}">'
        for k, (t, s, d) in enumerate(p["subs"]))
    hidden = (f'<input type="hidden" id="YourPriceUS_{n}" value="{p["us"]}">'
              f'<input type="hidden" id="YourPriceEUR_{n}" value="{p["eur"]}">'
              f'<input id="ProductPartNumber_{n}" name="[{n}].PartNumber" type="hidden" value="{pn}">'
              f'<input name="[{n}].MinOrder" type="hidden" value="{p["min"]}">'
              f'<input name="[{n}].Uom" type="hidden" value="{p["uom"]}">'
              f'<input name="[{n}].ErrorMsg" type="hidden" value="">'
              f'<input name="[{n}].PotentialLeadTime" type="hidden" value="{p["lead"]}">'
              f'<input name="[{n}].iMROCondition" type="hidden" value="NEW">{subs}')
    alts = "".join(f'<li>{d} <br><a>{s}</a></li>' for t, s, d in p["subs"])
    # The signed-in wording seen at onboarding, 2026-11-12.
    euro = f' / {p["eur"].replace(".", ",")} €' if p["eur"] != "0" else ""
    your = f'<div>Your Price: ${p["us"]}{euro}</div><div>LIST PRICE: $99.99 DISCOUNT: 40%</div>' \
        if p["us"] != "0" else ""
    stock = (f'<div>{p["stock"]} IN STOCK</div><label>Quantity</label><input name="qty" value="1">'
             f'<button>Add to Cart</button><div>Ships from {p.get("ships", "Wichita")}</div>'
             if p["stock"] is not None else "")
    price = (your + stock if signed_in else
             '<h2>Want to see more?</h2><h3><a href="/Account/Login">Log in</a> or <a>Register as '
             'a New User</a> to view pricing and availability.</h3>')
    return (f'<div class="col-xs-12 col-sm-8 col-lg-9 pb-2">{hidden}'
            f'<h3><a>Categories</a> / <a>Hardware</a></h3><h1 class="part-detail-title">{pn}</h1>'
            f'<p id="partDescription_{n}">{p["desc"]}</p><ul><li>Unit of Measure: <span>{p["uom"]}</span></li>'
            f'<li>Weight: 0.03 lbs</li>{p.get("extra", "")}</ul><h4>NOTES</h4><p>{p["notes"]}</p><h2>Applicable Aircraft</h2><ul><li>Citation CJ3</li></ul>'
            f'<p>List not comprehensive, may be applicable on other models.</p>'
            f'<span>Each delivered OEM part will meet the original manufacturer’s specification or, '
            f'if the part is not an OEM part, it will meet or exceed the original manufacturer’s '
            f'specification.</span></div>'
            f'<div class="col-xs-12 col-sm-4 col-lg-3 pb-2"><a>Remove Item</a>'
            f'<span>In-stock and ready to ship!</span>{price}<ul>{alts}</ul></div>'
            f'<div id="RecommendedItemsDiv_{n}"></div><div class="col-xs-12"><hr></div>')


def blocks_page(parts, signed_in):
    body = ('<div class="availability-div"><div class="row">'
            + "".join(block(n, pn, signed_in) for n, pn in enumerate(parts)) + "</div></div>")
    return body


MULTIPLE = """<form action="/Parts/PartSearch/Multiple" method="post">
<h1>Multiple Part Search</h1><span>Limit of 20 parts per search.</span>
<div id="SearchRows">{rows}</div>
<a class="btn" onclick="AddPartNoRow()">Add Another</a>
<button type="submit" class="btn btn-primary">Search</button></form>
<form action="/Parts/PartSearch/Multiple" id="FeedBackForm" method="post"><button type="submit">Send</button></form>
<script>
const QTY = {qty};
function row(i) {{ return '<div class="form-group"><input type="text" name="AddParts" class="form-control AddParts">'
  + (QTY ? '<input type="text" name="AddQtys" placeholder="Qty">' : '') + '</div>'; }}
function AddPartNoRow() {{ if (document.getElementsByName('AddParts').length < 20)
  document.getElementById('SearchRows').insertAdjacentHTML('beforeend', row()); }}
</script>"""
LOGIN = """<form action="/Account/Login" id="thisLoginForm" method="post" role="form">
<label>Log In</label><input type="text" id="Email" name="Email" placeholder="Email Address">
<input id="password-field" name="Password" placeholder="Password" type="password">
<div id="divReCaptchaV2" style="display: none"><div class="g-recaptcha">box</div></div>
<a href="ForgotPassword">Forgot Password?</a>
<button disabled type="button" class="btn btn-primary" id="btnSignIn"
 onclick="document.getElementById('thisLoginForm').submit()">Log In</button></form>
<script>setTimeout(() => {{ if ({captcha}) document.getElementById('divReCaptchaV2').style.display = 'block';
  else document.getElementById('btnSignIn').removeAttribute('disabled'); }}, 300);</script>"""
# The 2FA choice the team described (2026-11-12): pick Email, Send Code, then the box.
CHOICE_PAGE = """<form action="/Account/SendCode" method="post"><h2>Two-Factor Authentication</h2>
<p>Choose where to receive your verification code.</p>
<label><input type="radio" name="Provider" value="Phone" checked> Text message</label>
<label><input type="radio" name="Provider" value="Email"> Email</label>
<button type="submit">Send Code</button></form>"""
CODE_PAGE = """<form action="/Account/VerifyCode" method="post"><h2>Verify your identity</h2>
<p>Enter the verification code we sent to your email.</p>
<input type="text" id="Code" name="Code"><button type="submit">Verify</button></form>
<a href="/Account/SendCode">I didn't receive the code. Take me to the previous step.</a>"""


class MockTextron:
    def __init__(self, signed_in=True, qty_boxes=True):
        self.signed_in = signed_in
        self.qty_boxes = qty_boxes
        self.captcha = False
        self.two_fa = True
        self.logins: list[dict] = []
        self.multiple_posts: list[dict] = []
        self.details: list[str] = []
        self.last_multiple: list[str] = []
        self.refuse = False
        self.codes_sent: list[str] = []

    def page(self, body, title="Textron Aviation"):
        return PAGE.format(title=title, header=HEADER_IN if self.signed_in else HEADER_OUT,
                           body=body)

    async def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        path = url.path
        html = lambda body: route.fulfill(status=200, content_type="text/html", body=body)  # noqa: E731
        go = lambda to: html(f"<script>location.replace('{to}')</script>")  # noqa: E731
        if self.refuse:                    # what a headless browser got, 2026-11-12
            await route.fulfill(status=404, content_type="text/html", body=(
                "<html><body>HOME<h1>Oops, This Page Could Not Be Found!</h1><p>We're sorry, "
                "but the page you were looking for doesn't exist.</p>404</body></html>"))
        elif path == "/Account/Login" and req.method == "POST":
            self.logins.append({k: v[0] for k, v in parse_qs(req.post_data or "").items()})
            if self.two_fa:
                await html(self.page(CHOICE_PAGE, "Verify"))
            else:
                self.signed_in = True
                await go("/Dashboard")
        elif path == "/Account/Login":
            await html(self.page(LOGIN.format(captcha="true" if self.captcha else "false"), "Log in"))
        elif path == "/Account/SendCode" and req.method == "GET":
            await html(self.page(CHOICE_PAGE, "Verify"))
        elif path == "/Account/SendCode" and req.method == "POST":
            self.codes_sent.append(parse_qs(req.post_data or "").get("Provider", [""])[0])
            # The real site redirects to VerifyCode?Provider=Email (2026-11-13).
            await go("/Account/VerifyCode?Provider=Email&RememberMe=False")
        elif path == "/Account/VerifyCode" and req.method == "GET":
            await html(self.page(CODE_PAGE, "Verify"))
        elif path == "/Account/VerifyCode" and req.method == "POST":
            self.signed_in = True
            await go("/Dashboard")
        elif path == "/Account/LogOff" and req.method == "POST":
            self.signed_in = False
            await go("/")
        elif path == "/Dashboard":
            if not self.signed_in:
                await go("/Account/Login?ReturnUrl=%2FDashboard")
            else:
                await html(self.page("<h1>Customer Portal Dashboard</h1>"))
        elif path == "/Parts/PartSearch/Multiple" and req.method == "POST":
            form = parse_qs(req.post_data or "", keep_blank_values=True)
            self.multiple_posts.append(form)
            self.last_multiple = [p for p in form.get("AddParts", []) if p.strip()]
            await go("/Parts/PartSearch/PartsDetail?PageType=Multiple")
        elif path == "/Parts/PartSearch/Multiple":
            rows = "".join('<div class="form-group"><input type="text" name="AddParts" '
                           'class="form-control AddParts">'
                           + ('<input type="text" name="AddQtys" placeholder="Qty">'
                              if self.qty_boxes else "") + "</div>" for _ in range(6))
            await html(self.page(MULTIPLE.format(rows=rows, qty="true" if self.qty_boxes else "false"),
                                 "Multiple Part Search"))
        elif path == "/Parts/PartSearch/PartsDetail" and "Multiple" in url.query:
            # The site lists every posted part but a duplicate (assumed) in order.
            await html(self.page(blocks_page(self.last_multiple, self.signed_in)))
        elif path.startswith("/Parts/PartSearch/PartsDetail/"):
            pn = unquote(path.rsplit("/", 1)[-1]).upper()
            self.details.append(pn)
            await html(self.page(blocks_page([pn], self.signed_in)))
        else:
            await html(self.page("<p>Home</p>"))


class Reader:
    """Stands in for the LLM: one offer per part block, its USD price and stock."""

    def __init__(self, price_from="us"):
        self.texts: list[str] = []
        self.systems: list[str] = []
        self.price_from = price_from

    def offers(self, page_text):
        out = []
        for chunk in re.split(r"(?=--- Part \d+: )", page_text):
            head = re.match(r"--- Part \d+: (\S+)", chunk)
            if not head:
                continue
            pn = re.search(r"^Part number: (\S+)", chunk, re.M).group(1)
            req = re.search(r"^Is the requested part (\S+):", chunk, re.M).group(1)
            us = re.search(r"USD ([\d.]+)", chunk)
            eur = re.search(r"EUR ([\d.]+)", chunk)
            pick = us if self.price_from == "us" else eur if self.price_from == "eur" else None
            # No price in the data: the LLM taking a figure anyway (the program clears it).
            price = pick.group(1) if pick else "99.99" if self.price_from == "other" else "5.00"
            out.append((req, Offer(pn, price, None, "12", None, None, head.group(1))))
        return out

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        offers = [o for _, o in self.offers(page_text)]
        return bool(offers), "results", offers, LlmUsage(calls=1)

    async def extract_offers_bulk(self, *, page_text, page_url, parts, system=""):
        self.texts.append(page_text)
        self.systems.append(system)
        by_part: dict = {}
        for req, offer in self.offers(page_text):
            by_part.setdefault(req, []).append(offer)
        return "results", by_part, LlmUsage(calls=1)

    async def not_sold_quote(self, *_, **__):
        return None, LlmUsage()


def rid(n):
    return row("x", n=n).row_id


def row(pn, qty="", n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="TEXTRON AVIATION", part_number=pn,
                      qty_needed=qty)


@pytest_asyncio.fixture
async def textron(monkeypatch):
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockTextron()

    # page.request isn't routed (it would reach the real site): the dashboard check
    # reads the mock's state instead.
    async def account_state(self, page):
        return portal.signed_in
    monkeypatch.setattr(TextronAdapter, "_account_state", account_state)
    context = await browser.new_context()
    await context.route(f"{HOST}/**", portal.handle)
    page = await context.new_page()
    await page.goto(f"{HOST}/")
    yield page, portal
    await browser.close()
    await pw.stop()


def adapter(reader=None, **config):
    return TextronAdapter(extractor=reader, vendor_config={"human_typing": False, **config})


def cred():
    return VendorCredential(vendor_id="textron_aviation", owner="_shared",
                            secret_key="sourcing-vendor-textron-aviation", username="buyer@x.com",
                            password="pw", url=f"{HOST}/Account/Login")


def test_registered_for_the_sheet_and_records():
    for name in ("textron_aviation", "textron", "txtav"):
        assert adapter_class(name) is TextronAdapter
    assert adapter_class("x", {"adapter": "textron"}) is TextronAdapter
    # What a team member types in the Vendor column.
    from sourcing.store import MemoryStore
    assert VendorRegistry(MemoryStore()).resolve_id("TEXTRON AVIATION") == "textron_aviation"


def test_addresses_and_quantity():
    assert detail_url("A-7079B-24") == f"{HOST}/Parts/PartSearch/PartsDetail/A-7079B-24"
    assert requested_qty("10 EA") == "10" and requested_qty("") is None


def test_login_page_comes_from_the_secret_only():
    assert adapter().login_url_candidates(cred()) == [
        ("secret sourcing-vendor-textron-aviation url", f"{HOST}/Account/Login")]


def test_block_text_drops_the_aircraft_list_and_the_oem_sentence():
    text = block_section({"main": "A-7079B-24\nApplicable Aircraft\nCitation CJ3\nList not "
                                  "comprehensive, may be applicable on other models.\nWeight: 1",
                          "side": "Remove Item\nIn-stock and ready to ship!"})
    assert "Citation" not in text and "Remove Item" not in text
    assert "Weight: 1" in text and "In-stock and ready to ship!" in text


@pytest.mark.asyncio
async def test_one_part_by_its_address(textron):
    page, portal = textron
    reader = Reader()
    outcome = await adapter(reader).search(page, row("A-7079B-24", "5"))
    assert portal.details == ["A-7079B-24"] and outcome.found
    text = reader.texts[0]
    assert f"--- Part 1: {HOST}/Parts/PartSearch/PartsDetail/A-7079B-24" in text
    assert "Your price in the page's data: USD 45.51 / EUR 40.55" in text
    assert "Is the requested part A-7079B-24: yes" in text and "Condition: NEW" in text
    assert "Alternate listed: 34-0070371-01 FOR CESSNA APPLICATION ONLY (Cessna Alternate)" in text
    assert "Citation CJ3" not in text and "Service Center Stock" not in text
    assert reader.systems[0] == TEXTRON_EXTRACT_SYSTEM
    offer = outcome.offers[0]
    assert (offer.price, offer.currency, offer.uom, offer.condition) == ("45.51", "USD", "EA", "NE")
    assert offer.qty_available == "160"           # the page's "160 IN STOCK", not the LLM's 12
    assert offer.vendor_note == "PRICE VALID UNTIL STOCK DEPLETED"
    assert "Stock shown: 160 IN STOCK" in text and "Ships from: Wichita" in text
    assert offer.listing == "requested" and offer.moq is None and offer.review is None
    assert outcome.alternates == ["34-0070371-01", "WL-L7079-24"]


@pytest.mark.asyncio
async def test_part_not_found_needs_no_llm(textron):
    page, _ = textron
    reader = Reader()
    outcome = await adapter(reader).search(page, row("NOSUCHPART123"))
    assert outcome.found is False and outcome.page_kind == "no_results" and reader.texts == []


@pytest.mark.asyncio
async def test_signed_out_part_page_is_a_session_expiry(textron):
    page, portal = textron
    portal.signed_in = False
    with pytest.raises(SessionExpired):
        await adapter(Reader()).search(page, row("A-7079B-24"))


@pytest.mark.asyncio
async def test_price_none_of_the_pages_own_is_flagged(textron):
    page, _ = textron
    outcome = await adapter(Reader(price_from="other")).search(page, row("A-7079B-24"))
    assert outcome.offers[0].review == "PRICE_NOT_IN_PAGE_DATA"
    outcome = await adapter(Reader(price_from="eur")).search(page, row("A-7079B-24"))
    assert outcome.offers[0].currency == "EUR" and outcome.offers[0].review is None


@pytest.mark.asyncio
async def test_multiple_part_search_with_quantities(textron):
    page, portal = textron
    reader = Reader()
    rows = [row(f"P-{i}", n=i) for i in range(5)] + [
        row("A-7079B-24", "5", n=10), row("MS20470AD4-4", "2 LB", n=11), row("NOSUCHPART123", "1", n=12)]
    outcomes, usage = await adapter(reader).search_many(page, rows)
    post = portal.multiple_posts[0]
    assert len(post["AddParts"]) == 8                 # 6 boxes + 2 added with Add Another
    assert post["AddParts"][5:] == ["A-7079B-24", "MS20470AD4-4", "NOSUCHPART123"]
    assert post["AddQtys"][5:] == ["5", "2", "1"]
    assert portal.details == []                        # every part had its block: no tabs
    assert outcomes[rid(10)].found and outcomes[rid(11)].found
    assert outcomes[rid(12)].found is False and outcomes[rid(0)].found is False   # Part Not Found
    rivet = outcomes[rid(11)].offers[0]
    assert rivet.uom == "LB" and rivet.moq == "5" and rivet.currency == "USD"
    assert rivet.qty_available == "Out of stock" and rivet.vendor_note == "SOLD BY THE POUND"
    assert "Quantity typed into the Multiple Part Search: 2" in reader.texts[0]
    assert len(reader.texts) == 1 and usage.calls == 1


@pytest.mark.asyncio
async def test_form_without_quantity_boxes_searches_the_parts_alone(textron):
    page, portal = textron
    portal.qty_boxes = False
    outcomes, _ = await adapter(Reader()).search_many(page, [row("A-7079B-24", "5", n=2),
                                                             row("MS20470AD4-4", "2", n=3)])
    assert portal.multiple_posts[0]["AddParts"][:2] == ["A-7079B-24", "MS20470AD4-4"]
    assert "AddQtys" not in portal.multiple_posts[0]
    assert outcomes[rid(2)].found and outcomes[rid(3)].found


@pytest.mark.asyncio
async def test_parts_the_form_misses_are_opened_in_tabs(textron):
    page, portal = textron
    original = portal.handle

    async def drop_rivet(route):             # the results leave one part out
        if "PageType=Multiple" in route.request.url:
            portal.last_multiple = [p for p in portal.last_multiple if p != "MS20470AD4-4"]
        await original(route)
    await page.context.unroute(f"{HOST}/**")
    await page.context.route(f"{HOST}/**", drop_rivet)
    outcomes, _ = await adapter(Reader()).search_many(page, [row("A-7079B-24", n=2),
                                                             row("MS20470AD4-4", n=3)])
    assert portal.details == ["MS20470AD4-4"]
    assert outcomes[rid(2)].found and outcomes[rid(3)].found
    assert len(page.context.pages) == 1                 # the tabs were closed


@pytest.mark.asyncio
async def test_tabs_mode_opens_every_part_by_its_address(textron):
    page, portal = textron
    parts = ["A-7079B-24", "MS20470AD4-4", "NOSUCHPART123"]
    outcomes, _ = await adapter(Reader(), bulk_mode="tabs").search_many(
        page, [row(p, n=i) for i, p in enumerate(parts, start=2)])
    assert portal.multiple_posts == [] and sorted(portal.details) == sorted(parts)
    assert outcomes[rid(2)].found and outcomes[rid(3)].found and outcomes[rid(4)].found is False


@pytest.mark.asyncio
async def test_signed_out_multiple_results_are_a_session_expiry(textron):
    page, portal = textron
    portal.signed_in = False
    with pytest.raises(SessionExpired):
        await adapter(Reader()).search_many(page, [row("A-7079B-24", n=2), row("MS20470AD4-4", n=3)])


@pytest.mark.asyncio
async def test_login_then_2fa_finished_by_a_person(textron):
    page, portal = textron
    portal.signed_in = False
    textron_adapter = adapter()
    with pytest.raises(MfaRequired) as asked:
        await textron_adapter.login(page, cred())
    assert portal.logins[0]["Email"] == "buyer@x.com" and portal.logins[0]["Password"] == "pw"
    assert portal.codes_sent == ["Email"]          # Email chosen, Send Code clicked
    assert asked.value.input_selector == "input[id*='code' i]" or asked.value.input_selector
    assert await textron_adapter.person_finished_login(page) is False
    # The person types the code in Live View.
    await page.fill("#Code", "123456")
    await page.click("button:has-text('Verify')")
    await page.wait_for_url(re.compile(r"/Dashboard"))
    assert await textron_adapter.person_finished_login(page) is True
    assert textron_adapter.signed_in_proof


@pytest.mark.asyncio
async def test_login_without_2fa_is_signed_in(textron):
    page, portal = textron
    portal.signed_in, portal.two_fa = False, False
    textron_adapter = adapter()
    await textron_adapter.login(page, cred())
    assert portal.signed_in and textron_adapter.signed_in_proof


@pytest.mark.asyncio
async def test_not_a_robot_box_goes_to_a_person(textron):
    page, portal = textron
    portal.signed_in, portal.captcha = False, True
    with pytest.raises(MfaRequired) as asked:
        await adapter().login(page, cred())
    assert asked.value.method == "captcha" and portal.logins == []     # nothing sent


@pytest.mark.asyncio
async def test_logout_by_the_header_link(textron):
    page, portal = textron
    await page.goto(f"{HOST}/Dashboard")
    textron_adapter = adapter()
    assert await textron_adapter.is_authenticated(page) is True
    assert await textron_adapter.logout(page) is True and portal.signed_in is False
    assert await textron_adapter.person_finished_logout(page) is True


@pytest.mark.asyncio
async def test_the_sites_error_page_stops_the_search_never_not_found(textron):
    page, portal = textron
    portal.refuse = True
    with pytest.raises(LoginBlocked, match="Page Could Not Be Found"):
        await adapter(Reader()).search(page, row("A-7079B-24"))
    with pytest.raises(LoginBlocked):
        await adapter(Reader()).search_many(page, [row("A-7079B-24", n=2), row("MS20470AD4-4", n=3)])
    with pytest.raises(LoginBlocked):
        await adapter(Reader(), bulk_mode="tabs").search_many(
            page, [row("A-7079B-24", n=2), row("MS20470AD4-4", n=3)])
    portal.signed_in = False
    with pytest.raises(LoginBlocked):
        await adapter().login(page, cred())


@pytest.mark.asyncio
async def test_signed_out_miss_is_not_a_not_found(textron):
    # The real "Part Not Found." block has no sign-in line (2026-11-12): the
    # dashboard check decides.
    page, portal = textron
    portal.signed_in = False
    with pytest.raises(SessionExpired):
        await adapter(Reader()).search(page, row("NOSUCHPART123"))


@pytest.mark.asyncio
async def test_real_run_wordings_19_parts(textron):
    # The 19-part run, 2026-11-12: price 0 + "contact us", no longer available with a
    # replacement, multiples of 10, a dollar-only price, Columbus GA, hazardous.
    page, _ = textron
    reader = Reader()
    rows = [row(p, n=i) for i, p in enumerate(["101-320234-3", "M83461-1-122", "6839", "C40187",
                                               "MS20470AD4-4"], start=2)]
    outcomes, _ = await adapter(reader).search_many(page, rows)
    text = reader.texts[0]
    assert "M83461-1-122" not in text                # not sold: never sent to the LLM
    assert "Your price in the page's data: none (PLEASE CONTACT US" in text
    assert "EUR 0" not in text and "USD 0 " not in text
    placard = outcomes[rid(2)].offers[0]
    assert placard.price == "Not Present on website"
    assert "EXTENDED INACTIVITY: QUOTE MAY TAKE LONGER" in placard.vendor_note
    gone = outcomes[rid(3)]
    assert gone.found is False and gone.not_sold.startswith("NO LONGER AVAILABLE. PLEASE SEE OPTIONS BELOW.")
    assert "M83461/1-122 (REPLACEMENT FOR CESSNA & BEECH APPLICATIONS)" in gone.not_sold
    assert gone.alternates == []        # the same number in Textron's spelling: named in not_sold
    lamp = outcomes[rid(4)].offers[0]
    assert lamp.moq == "10" and lamp.qty_available == "7440" and lamp.lead_time == "In stock"
    assert "SOLD IN MULTIPLES OF 10" in lamp.vendor_note and "MAX SHELF LIFE 12 MONTHS" in lamp.vendor_note
    assert lamp.warehouses == "Warehouse: Wichita\nQty on hand: 7440"
    block = outcomes[rid(5)].offers[0]
    assert (block.price, block.currency) == ("1760.63", "USD")
    assert block.warehouses == "Warehouse: Columbus, GA\nQty on hand: 3"
    assert "HAZARDOUS MATERIAL" in block.vendor_note
    rivet = outcomes[rid(6)].offers[0]
    assert rivet.qty_available == "Out of stock"
    assert rivet.lead_time == "Estimated availability 12/27/2026"
    assert rivet.warehouses == "Warehouse: Wichita\nQty on hand: 0"


@pytest.mark.asyncio
async def test_no_longer_available_one_by_one(textron):
    page, _ = textron
    reader = Reader()
    outcome = await adapter(reader).search(page, row("M83461-1-122"))
    assert outcome.found is False and reader.texts == []
    assert "NO LONGER AVAILABLE" in outcome.not_sold and "M83461/1-122" in outcome.not_sold


@pytest.mark.asyncio
async def test_a_2fa_that_ends_without_signing_in_stops_the_wait(textron, monkeypatch):
    # 2026-11-13: after the code step the page went back to Send Code and stayed; the
    # run waited out its 10 minutes. Now the check says the sign-in ended.
    from sourcing.vendors import textron as textron_module
    page, portal = textron
    portal.signed_in = False
    monkeypatch.setattr(textron_module, "SEND_CODE_IDLE_SECONDS", 0)
    t = adapter()
    with pytest.raises(MfaRequired):
        await t.login(page, cred())
    assert await t.person_finished_login(page) is False          # on the code page: waiting
    await page.click("a:has-text('previous step')")              # "I didn't receive the code"
    await page.wait_for_url(re.compile(r"/Account/SendCode"))
    assert await t.person_finished_login(page) is False          # first seen: still waiting
    with pytest.raises(MfaRequired, match="Send Code"):
        await t.person_finished_login(page)                      # stayed there: ended
    # Back on the login form after the code step ends it at once.
    await page.goto(f"{HOST}/Account/Login")
    with pytest.raises(MfaRequired, match="sign-in page"):
        await t.person_finished_login(page)


@pytest.mark.asyncio
async def test_captcha_handoff_on_the_login_form_keeps_waiting(textron):
    # "I'm not a robot": the person signs in on the login form; that isn't an ended sign-in.
    page, portal = textron
    portal.signed_in, portal.captcha = False, True
    t = adapter()
    with pytest.raises(MfaRequired):
        await t.login(page, cred())
    assert await t.person_finished_login(page) is False
