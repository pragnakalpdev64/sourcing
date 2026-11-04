"""AviallAdapter against an invented mock of shop.boeing.com, from the team's
description (2026-10-30, sign-in steps 2026-11-02): Log in links in the avatar
menu, "Log in to Boeing Distribution" -> "Log in with Boeing OneLogin ID" ->
username -> password -> 2FA box -> the login URL again, the multi-line bulk box. The real pages haven't
been saved yet. Skipped if no local Chromium."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.aviall import SEARCH_URL, AviallAdapter
from sourcing.vendors.aviall_prompt import AVIALL_EXTRACT_SYSTEM
from sourcing.vendors.base import CaptchaFailed, LoginBlocked, MfaRequired, fill_part_url

HOST = "https://shop.boeing.example.com"

# The login URL: no form, only the step-1 button. It opens a dialog on the
# same page, not a new page (team, 2026-11-02); drawn a moment later.
LOGIN_PAGE = """<html><body><h1>Boeing Distribution</h1>
<button onclick="setTimeout(() => document.getElementById('choose').style.display='block', 1500)">
Log in to Boeing Distribution</button>
<div role="dialog" id="choose" style="display:none"><h2>Choose how to log in</h2>
<a href="/register">Create an account</a>
<button onclick="location.href='/onelogin'">Log in with Boeing OneLogin ID</button>
</div></body></html>"""
USERNAME_PAGE = """<html><body><h1>Boeing</h1><form method="post" action="/onelogin/user">
<input id="username" name="u" type="text"><button type="submit">Next</button></form></body></html>"""
PASSWORD_PAGE = """<html><body><h1>Boeing</h1><form method="post" action="/onelogin/password">
<input id="password" name="p" type="password"><button type="submit">Sign in</button></form>
</body></html>"""
MFA_PAGE = """<html><body><p>We sent a verification code by text message (SMS)</p>
<form method="post" action="/mfa"><input name="otpCode" autocomplete="one-time-code">
<button type="submit">Verify</button></form></body></html>"""
# Signed out, both Log in links are only in the menu the avatar opens.
HOME = """<html><body><h1>Boeing Distribution</h1>
<button aria-label="Account" onclick="document.getElementById('menu').style.display='block'">
<img alt=""></button><div id="menu" style="display:none">
<a href="/login">Log in</a></div></body></html>"""
# As in the saved HTML (2026-11-02): the header's Multi-line Ordering button
# opens a <boeing-generic-popover> with Line-by-line boxes, the Copy & Paste
# textarea.bulkSearchTextArea and Clear All / Search (" Search " in a span);
# the page's own search form has a Search button too. The "recently searched"
# box is from the team's screenshots.
BULK_PAGE = """<html><body><div class="accounts-bar"><div class="account-detail">10016784 - SKYLINK INC</div></div>
<form action="/boeingsearch"><input name="query"><button>Search</button></form>
<button onclick="document.getElementById('multi').style.display='block'">Multi-line Ordering</button>
<boeing-generic-popover id="multi" style="display:none"><h2>Multi-line Ordering</h2>
<form action="/bulkresults"><h2>Line-by-line</h2>
<input name="line1" placeholder="part #"><input name="qty1" value="1">
<h2>Copy &amp; Paste</h2><textarea class="bulkSearchTextArea" name="lines"
 placeholder="For example: PKAB244CKIT=2V, 1 (Max of 150 lines)"></textarea>
<label><input type="checkbox" name="recent" checked> Include 4 recently searched part numbers.</label>
<button formaction="/cart">Add to Cart</button>
<div class="actions"><button type="reset">Clear All</button><button><span> Search </span></button></div>
</form></boeing-generic-popover></body></html>"""
# One-part results: an icon label, a stock-location pin that shows its text on
# hover only, and a Price Breaks pop-up opened by a click (screenshots 2026-10-30).
RESULTS = """<html><body><div class="accounts-bar"><div class="account-detail">10016784 - SKYLINK INC</div></div>
<div>Products in Cart (1)</div><div>HEAT SHRINKABLE SLEEVE Part # M23053/5-105-9 Qty: 100 $0.67 FT</div>
<a href="/cart">View Cart &amp; Checkout</a><a href="/">Continue Shopping</a><div>2 results</div>
<div>Home &gt; Search : 021200-21181=27</div>
<div data-testid="productListView" class="brandx-plp-single-item-container"><a href="/p/1">EN2267-010A006S</a><p>EN2267-010A006S High Temp Airframe Cable</p>
<svg aria-label="8130 Certificate" width="20" height="20"><rect width="20" height="20"/></svg>
<button popoverclass="uom-popup-container" class="open-button bdsi-price-break"
 onclick="document.getElementById('pb').style.display='block'"><span>Price Breaks</span></button>
<a href="/cpd/bdi_en2267-010a006s"><svg width="16" height="16"><rect width="16" height="16"/></svg></a>
<div><b>$0.49</b> MR</div>
<span>In Stock (15838)</span>
<button popoverclass="warehouse-stock-info" class="open-button"
 onclick="document.getElementById('loc').style.display='block'"><img ngbtooltip="Stock By Location"
 width="16" height="16" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></button>
<div><button class="panda-plp-quantity-plus-btn"><img width="8" height="8" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></button></div>
<svg width="16" height="16" onmouseenter="document.getElementById('tip').style.display='block'"
 onmouseleave="document.getElementById('tip').style.display='none'"><rect width="16" height="16"/></svg>
<button>Add to Cart</button><a href="/support">Request Support</a></div>
<div data-testid="productListView" class="brandx-plp-single-item-container"><a href="https://shop.boeing.example.com/cpd/bdi_usm14-14j4g%3Dab">USM14-14J4G</a><span>Out Of Stock</span><a href="/support">Request Support</a></div>
<div>You may also be interested in</div><div>3012680 Nut, Option Ic</div><div>DISCLAIMER: as is.</div>
<div data-testid="productListView" class="brandx-plp-single-item-container"><a href="https://shop.boeing.example.com/cpd/bdi_021200-21181%3D27">021200-21181</a>
<span>Superseded</span> Superseded by part number 021200-21181=32
<a href="https://shop.boeing.example.com/cpd/bdi_021200-21181%3D32">View Replacement</a><a href="/support">Request Support</a></div>
<div role="dialog" id="loc" style="display:none">Dallas, TX 15838</div>
<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend',
 '<div id="walkme-overlay-all" style="position:fixed;inset:0;background:rgba(0,0,0,.4)"></div>' +
 '<div id="walkme-player" style="position:fixed;top:30%;left:30%">Tour Our New Features! ' +
 '<button class="wm-visual-design-button">No thanks.</button><button>Show me!</button></div>'), 300);
</script>
<div role="tooltip" id="tip" style="display:none">Dallas, TX: 15838</div>
<div role="dialog" id="pb" style="display:none">Price Break Quantity Price Break 1 $0.49/MR 500 $0.49/MR</div>
<script>document.addEventListener('keydown', e => { if (e.key === 'Escape')
  { document.getElementById('pb').style.display = 'none';
    document.getElementById('loc').style.display = 'none'; } });</script></body></html>"""
# Signed in, as on the live site (2026-11-02): the "Shopping as" bar, My Account
# is a link to the account page, no Log out on the shop's pages.
# The avatar beside the cart opens "Welcome, <name>" and Log out (saved pages).
ACCOUNTS_BAR = """<div class="accounts-bar"><div class="account-title">Shopping as:</div>
<div class="account-detail">10016784 - SKYLINK INC</div></div><a href="/account">My Account</a>
<a href="/cart">Cart</a><bc-header-my-account><div id="my-account-profile-dropdown" class="dropdown">
<button class="dropdown-toggle" aria-expanded="false" onclick="const m = document.getElementById(
 'my-account-profile'); const open = m.style.display !== 'block'; m.style.display = open ? 'block' : 'none';
 this.setAttribute('aria-expanded', String(open))"><svg width="24" height="24"><rect width="24" height="24"/></svg></button>
<div id="my-account-profile" class="dropdown-menu" style="display:none"><div class="account-details">
<span class="initial">J</span> Welcome, <a href="/account">JANE</a></div>
<button class="menus" onclick="document.getElementById('confirm').style.display='block'"><span>Log Out</span></button>
</div></div></bc-header-my-account>
<div class="modal" id="confirm" style="display:none"><p>Are you sure you want to log out?</p>
<div class="modal-ctas"><button>Cancel</button><a href="/logout">Log out</a></div></div>"""
DASHBOARD = f"""<html><body><h1>Welcome back</h1>{ACCOUNTS_BAR}</body></html>"""
MENU_DASHBOARD = DASHBOARD
# "Asked again" (team, 2026-11-02): the first dialog's Log out opens a second
# dialog; only its Log out signs out. A stray "Log out" link elsewhere is never used.
TWICE_DASHBOARD = DASHBOARD.replace(
    '<a href="/logout">Log out</a></div></div>',
    '<button onclick="document.getElementById(\'confirm\').remove();'
    ' document.getElementById(\'confirm2\').style.display=\'block\'">Log out</button></div></div>'
    '<div class="modal" id="confirm2" style="display:none"><p>Are you sure you want to log out?</p>'
    '<div class="modal-ctas"><button>Cancel</button><a href="/logout">Log out</a></div></div>'
    '<footer><a href="/elsewhere">Log out</a></footer>')
# The product page's Technical Information (team's screenshot, 2026-11-02).
PRODUCT_PAGE = """<html><body><h1>USM14-14J4G Swivel Assembly</h1><h2>Technical Information</h2>
<table><tr><td>UNSPSC Code</td><td>26121600</td></tr><tr><td>Lead Time (Days)</td><td>80</td></tr>
<tr><td>Unit Of Measure</td><td>EA</td></tr></table></body></html>"""
ACCOUNT_PAGE = """<html><body><h1>Account details</h1>
<button onclick="location.href='/logout'">Log out</button></body></html>"""
# A page with no avatar menu, e.g. where a run that hit an error stopped.
PLAIN = "<html><body><p>Order details</p></body></html>"
CAPTCHA_PAGE = "<html><body><p>Please complete the CAPTCHA to continue.</p></body></html>"


class MockAviall:
    """mfa: the password ends on a 2FA box. relogin: what opening the login URL
    again shows after it ("ok" signed in, "button" signed in only after the
    two buttons, "back" as "button" but Boeing sends us back to the login URL
    itself, "mfa" the 2FA box again, "form" the username page, "captcha")."""

    def __init__(self, *, mfa=True, relogin="ok", menu_logout=False, confirm_twice=False,
                 covered=False):
        self.mfa, self.relogin, self.covered = mfa, relogin, covered
        self.dashboard = (TWICE_DASHBOARD if confirm_twice else
                          MENU_DASHBOARD if menu_logout else DASHBOARD)
        self.signed_in = False
        self.boeing_session = False       # Boeing's sign-in after the password
        self.user = None
        self.lines: list[str] = []
        self.recent = None
        self.gets: list[str] = []
        self.product_pages: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    async def handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        if req.method == "POST":
            self.posts.append((path, form))
        else:
            self.gets.append(path)
        if path == "/onelogin/user" and req.method == "POST":
            self.user = form.get("u")
            html = PASSWORD_PAGE
        elif path == "/onelogin/password" and req.method == "POST":
            ok = self.user == "jane" and form.get("p") == "pw"
            self.boeing_session = ok
            if ok and not self.mfa:
                self.signed_in = True
            html = (PASSWORD_PAGE if not ok else MFA_PAGE if self.mfa else self.dashboard)
        elif path == "/login":
            if self.boeing_session and self.relogin == "ok":
                self.signed_in = True
            html = (CAPTCHA_PAGE if self.boeing_session and self.relogin == "captcha"
                    else self.dashboard if self.signed_in else LOGIN_PAGE)
            if self.covered and html is LOGIN_PAGE:
                # A layer over the whole page (cookie banner / tour, 2026-11-02).
                html = html.replace("</body>", '<div style="position:fixed;inset:0;z-index:99;'
                                    'background:rgba(0,0,0,.3)"></div></body>')
        elif path == "/onelogin" and self.boeing_session and self.relogin == "back":
            # As in the run log 2026-11-02: back on the same address, signed in.
            self.signed_in = True
            html = "<html><body><script>location.replace('/login')</script></body></html>"
        elif path == "/onelogin":
            if self.boeing_session and self.relogin == "button":
                self.signed_in = True
            html = (self.dashboard if self.signed_in else
                    MFA_PAGE if self.boeing_session and self.relogin == "mfa" else USERNAME_PAGE)
        elif path == "/bulksearch":
            html = BULK_PAGE
        elif path == "/boeingsearch":
            html = RESULTS
        elif path in ("/bulkresults", "/cart"):
            query = parse_qs(urlparse(req.url).query)
            self.lines = query.get("lines", [""])[0].splitlines()
            self.recent = "recent" in query
            html = (f"<html><body>{ACCOUNTS_BAR}<boeing-generic-popover><div class='genericPopoverHeader'>"
                    "Multi-line Ordering <button class='closeBtn' onclick=\"this.closest("
                    "'boeing-generic-popover').remove()\">x</button></div></boeing-generic-popover>"
                    f"<div>0 results</div><p>{path}</p></body></html>")
        elif path.startswith("/cpd/"):
            self.product_pages.append(path)
            html = PRODUCT_PAGE
        elif path == "/plain":
            html = PLAIN
        elif path == "/account":
            html = ACCOUNT_PAGE if self.signed_in else LOGIN_PAGE
        elif path == "/logout":
            self.signed_in = self.boeing_session = False
            html = HOME
        else:
            html = self.dashboard if self.signed_in else HOME
        await route.fulfill(status=200, content_type="text/html", body=html)


class FakeLlm:
    def __init__(self):
        self.calls: list[str] = []

    async def pick_element(self, elements, what):
        self.calls.append("element")
        return None, LlmUsage(calls=1)

    async def classify_login_page(self, page_text, page_url):
        self.calls.append("classify")
        return "unknown", LlmUsage(calls=1)

    async def extract_offers_bulk(self, page_text, page_url, parts, system):
        self.calls.append("bulk")
        self.system = system
        return "results", {}, LlmUsage(calls=1)

    async def extract_offers(self, page_text, page_url, part_number, qty_needed,
                             condition_wanted, system):
        self.calls.append("extract")
        self.page_text = page_text
        return True, "results", [], LlmUsage(calls=1)

    async def not_sold_quote(self, page_text, part_number):
        self.calls.append("not_sold")
        return None, LlmUsage(calls=1)


def adapter(llm=None, **config) -> AviallAdapter:
    return AviallAdapter(extractor=llm or FakeLlm(), vendor_config={
        "allowed_hosts": ["boeing.example.com"], "login_url": f"{HOST}/login",
        "home_url": f"{HOST}/", "bulk_search_url": f"{HOST}/bulksearch",
        "logout_page_url": f"{HOST}/account", **config})


def cred():
    return VendorCredential("aviall", "", "sourcing-vendor-aviall", "jane", "pw", None)


@pytest_asyncio.fixture
async def page_for():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")

    async def make(portal: MockAviall):
        # A context of its own, as the real backends open: the logout check
        # opens a second tab in it, which must reach the mock portal too.
        context = await browser.new_context()
        await context.route("https://*.example.com/**", portal.handle)
        return await context.new_page()

    yield make
    await browser.close()
    await pw.stop()


def test_aviall_has_its_own_adapter():
    assert adapter_class("aviall") is AviallAdapter
    assert adapter_class("aviall", {"adapter": "generic", "auto_created_from": "x"}) is AviallAdapter
    a = AviallAdapter()
    assert a.extract_system == AVIALL_EXTRACT_SYSTEM and "## 5. Aviall Pages" in a.extract_system
    assert fill_part_url(a.part_url, "AV2539512=F1") == (
        "https://shop.boeing.com/boeingsearch?query=AV2539512%3DF1")
    assert a.part_url == SEARCH_URL and a.hosts == ("boeing.com", "aviall.com")
    # Boeing's site is slow at times (team, 2026-11-02): longer than the generic 20s / 90s.
    assert a.action_timeout_ms == 45_000 and a.page_load_timeout_ms == 180_000


@pytest.mark.asyncio
async def test_login_link_is_found_in_the_avatar_menu(page_for):
    portal = MockAviall(mfa=False)
    a = adapter(login_url=None, website=f"{HOST}/")
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and not a.relogin_tried
    assert a.learned["account_menu"]


@pytest.mark.asyncio
async def test_sign_in_clicks_both_buttons_then_username_then_password(page_for):
    portal = MockAviall(mfa=False)
    a = adapter()
    await a.login(await page_for(portal), cred())
    assert "/account" not in portal.gets          # My Account is never opened
    assert portal.signed_in and not a.relogin_tried
    assert portal.gets[:2] == ["/login", "/onelogin"]     # step 1 is a dialog, no page
    assert "/register" not in portal.gets
    assert [p for p, _ in portal.posts] == ["/onelogin/user", "/onelogin/password"]
    learned = a.confirmed_learned()
    assert learned["distribution_login_button"] and learned["onelogin_button"]


@pytest.mark.asyncio
async def test_2fa_box_then_the_login_url_again_signs_in(page_for):
    portal = MockAviall()
    a = adapter()
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and a.relogin_tried
    assert portal.gets.count("/login") == 2
    assert [p for p, _ in portal.posts] == ["/onelogin/user", "/onelogin/password"]  # no code


@pytest.mark.asyncio
async def test_2fa_box_then_the_login_url_and_its_buttons_sign_in(page_for):
    portal = MockAviall(relogin="button")
    a = adapter()
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and a.relogin_tried
    assert portal.gets.count("/login") == 2 and portal.gets.count("/onelogin") == 2
    assert [p for p, _ in portal.posts] == ["/onelogin/user", "/onelogin/password"]  # typed once


@pytest.mark.asyncio
async def test_relogin_back_on_the_same_address_with_log_out_in_the_menu(page_for):
    portal = MockAviall(relogin="back", menu_logout=True)
    a = adapter()
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and a.relogin_tried
    assert a.signed_in_proof and "account" in a.signed_in_proof   # "Shopping as" bar, no click
    assert "/account" not in portal.gets
    assert [p for p, _ in portal.posts] == ["/onelogin/user", "/onelogin/password"]


@pytest.mark.asyncio
async def test_2fa_again_after_the_login_url_asks_a_person(page_for):
    portal = MockAviall(relogin="mfa")
    with pytest.raises(MfaRequired) as exc:
        await adapter().login(await page_for(portal), cred())
    assert exc.value.method == "sms" and exc.value.input_selector   # code form can be used
    assert portal.gets.count("/login") == 2                         # reopened once only
    assert not portal.signed_in


@pytest.mark.asyncio
async def test_sign_in_form_after_the_login_url_asks_a_person_without_typing(page_for):
    portal = MockAviall(relogin="form")
    with pytest.raises(MfaRequired) as exc:
        await adapter().login(await page_for(portal), cred())
    assert exc.value.method == "relogin"
    assert [p for p, _ in portal.posts] == ["/onelogin/user", "/onelogin/password"]


@pytest.mark.asyncio
async def test_captcha_after_the_login_url_is_a_retryable_failure(page_for):
    with pytest.raises(CaptchaFailed, match="CAPTCHA"):
        await adapter().login(await page_for(MockAviall(relogin="captcha")), cred())


@pytest.mark.asyncio
async def test_bulk_search_types_one_part_per_line_and_never_adds_to_cart(page_for):
    portal = MockAviall(mfa=False)
    portal.signed_in = True
    llm = FakeLlm()
    rows = [RequestRow(sheet_row=i, rfq_id="", vendor_raw="aviall", part_number=pn, qty_needed=q)
            for i, (pn, q) in enumerate([("AV2539512=F1", "50"), ("MS24665-132", "")], start=2)]
    outcomes, _ = await adapter(llm).search_many(await page_for(portal), rows)
    assert portal.lines == ["AV2539512=F1, 50", "MS24665-132"]
    assert portal.recent is False                         # "recently searched" unticked
    assert "/bulkresults" in portal.gets and "/cart" not in portal.gets
    assert llm.calls == ["bulk"] and llm.system == AVIALL_EXTRACT_SYSTEM and outcomes == {}


@pytest.mark.asyncio
async def test_hover_text_icon_labels_and_price_breaks_are_written_into_the_card(page_for):
    portal = MockAviall(mfa=False)
    portal.signed_in = True
    llm = FakeLlm()
    row = RequestRow(sheet_row=2, rfq_id="", vendor_raw="aviall", part_number="EN2267-010A006S")
    a = adapter(llm, part_url_template=f"{HOST}/boeingsearch?query={{part}}")
    await a.search(await page_for(portal), row)
    text = llm.page_text
    card = text[text.index("EN2267-010A006S"):text.index("USM14-14J4G")]
    assert "[icon: 8130 Certificate]" in card
    assert "(shown on hover" not in card               # hovering is off: values are in pop-ups
    assert "(Price Breaks pop-up: Price Break Quantity Price Break 1 $0.49/MR 500 $0.49/MR)" in card
    assert "Dallas" not in text[text.index("USM14-14J4G"):].split("---")[0]   # not in the next card
    # The "=" suffix from the product link; the cart, recommendations and tour left out.
    assert "[full part number: USM14-14J4G=AB]" in text
    assert "(Stock By Location pop-up: Dallas, TX 15838)" in card
    # The Superseded card's own number, not its View Replacement link's.
    assert "[full part number: 021200-21181=27]" in text and "21181=32]" not in text
    assert "M23053/5-105-9" not in text and "3012680" not in text and "Tour Our" not in text


@pytest.mark.asyncio
async def test_logout_from_the_avatar_menu_at_the_end_of_a_run(page_for):
    portal = MockAviall(mfa=False, menu_logout=True)
    page = await page_for(portal)
    a = adapter()
    await a.login(page, cred())
    assert portal.signed_in
    assert await a.logout(page) and not portal.signed_in
    assert "/account" not in portal.gets          # the avatar's Log out, not the account page


@pytest.mark.asyncio
async def test_logout_from_a_page_without_the_menu_goes_home_first(page_for):
    portal = MockAviall(mfa=False, menu_logout=True)
    page = await page_for(portal)
    a = adapter()
    await a.login(page, cred())
    await page.goto(f"{HOST}/plain")
    assert await a.logout(page) and not portal.signed_in
    assert "/logout" in portal.gets


@pytest.mark.asyncio
async def test_logout_after_the_relogin_sign_in(page_for):
    portal = MockAviall(menu_logout=True)          # 2FA box -> the login URL signs in
    page = await page_for(portal)
    a = adapter()
    await a.login(page, cred())
    assert portal.signed_in and a.relogin_tried
    assert await a.logout(page) and not portal.signed_in


def test_condition_not_shown_is_new_unless_the_page_names_one():
    from sourcing.models import Offer
    offers = [Offer(part_number="A"), Offer(part_number="B", condition="Not Present on website"),
              Offer(part_number="C", condition="OH")]
    AviallAdapter()._fill_condition(offers)
    assert [o.condition for o in offers] == ["NE", "NE", "OH"]
    offers = [Offer(part_number="A")]
    AviallAdapter(vendor_config={"condition_not_shown": ""})._fill_condition(offers)
    assert offers[0].condition is None


@pytest.mark.asyncio
async def test_sign_in_check_on_the_home_page_reads_the_shopping_as_bar(page_for):
    portal = MockAviall(mfa=False)
    page = await page_for(portal)
    a = adapter()
    assert a.probe_url == f"{HOST}/"
    assert not await a.is_authenticated(page)
    portal.signed_in = True
    assert await a.is_authenticated(page)


class PickingLlm(FakeLlm):
    """Picks the element whose text is `wanted`."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted

    async def pick_element(self, elements, what):
        self.calls.append("element")
        self.what = what
        index = next((e["index"] for e in elements if e.get("text") == self.wanted), None)
        return index, LlmUsage(calls=1)


STEP_PAGE = """<html><body><nav><a href="/a">Products</a><a href="/b">Support</a></nav>
<div role="dialog"><button>Add to Cart</button><button>Go find them</button><textarea></textarea></div>
</body></html>"""


@pytest.mark.asyncio
async def test_llm_step_is_told_the_job_and_never_gets_a_cart_button(page_for):
    page = await page_for(MockAviall(mfa=False))
    await page.set_content(STEP_PAGE)
    a = adapter(PickingLlm("Add to Cart"))
    assert await a._llm_step(page, "the button that searches the parts") is None
    assert "never adds to the cart" in a.extractor.what
    a = adapter(PickingLlm("Go find them"))
    picked = await a._llm_step(page, "the button that searches the parts")
    assert picked and await page.locator(picked).inner_text() == "Go find them"



class OfferLlm(FakeLlm):
    """Reads one priced, out-of-stock line for the second card."""

    async def extract_offers(self, page_text, page_url, part_number, qty_needed,
                             condition_wanted, system):
        from sourcing.models import Offer
        self.calls.append("extract")
        offer = Offer(part_number="USM14-14J4G=AB", price="5.00", qty_available="Out of stock",
                      listing="requested")
        return True, "results", [offer], LlmUsage(calls=1)


@pytest.mark.asyncio
async def test_out_of_stock_line_gets_its_lead_time_from_the_product_page(page_for):
    portal = MockAviall(mfa=False)
    portal.signed_in = True
    page = await page_for(portal)
    row = RequestRow(sheet_row=2, rfq_id="", vendor_raw="aviall", part_number="USM14-14J4G=AB")
    a = adapter(OfferLlm(), part_url_template=f"{HOST}/boeingsearch?query={{part}}")
    outcome = await a.search(page, row)
    assert outcome.offers[0].lead_time == "80 days" and outcome.offers[0].condition == "NE"
    assert portal.product_pages == ["/cpd/bdi_usm14-14j4g%3Dab"]       # that card's page only
    assert "/boeingsearch" in page.url                                  # results page kept
    a = adapter(OfferLlm(), part_url_template=f"{HOST}/boeingsearch?query={{part}}",
                product_pages="off")
    portal.product_pages.clear()
    assert (await a.search(page, row)).offers[0].lead_time is None and not portal.product_pages



@pytest.mark.asyncio
async def test_logout_answers_the_confirmation_asked_twice(page_for):
    assert TWICE_DASHBOARD != DASHBOARD
    portal = MockAviall(mfa=False, confirm_twice=True)
    page = await page_for(portal)
    a = adapter(action_timeout_seconds=5)        # the first Log out opens no page
    await a.login(page, cred())
    assert portal.signed_in
    assert await a.logout(page) and not portal.signed_in
    assert "/elsewhere" not in portal.gets          # only the dialogs' own Log out



@pytest.mark.asyncio
async def test_sign_in_buttons_under_a_layer_are_still_clicked(page_for):
    portal = MockAviall(mfa=False, covered=True)
    a = adapter()
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and "/onelogin" in portal.gets



@pytest.mark.asyncio
async def test_results_page_tells_signed_in_from_signed_out(page_for):
    page = await page_for(MockAviall(mfa=False))
    a = adapter()
    await page.set_content("""<div data-testid="productListView">EN2267 <span data-testid="panda-price">$0.49</span>
        <button>Add to Cart</button></div>""")
    assert not await a._signed_out_here(page)
    await page.set_content("<div data-testid='productListView'>EN2267 Log in to view the price</div>")
    assert await a._signed_out_here(page)


@pytest.mark.asyncio
async def test_bulk_search_closes_the_dialog_over_the_results(page_for):
    portal = MockAviall(mfa=False)
    portal.signed_in = True
    page = await page_for(portal)
    rows = [RequestRow(sheet_row=2, rfq_id="", vendor_raw="aviall", part_number="MS24665-132",
                       qty_needed="5"), RequestRow(sheet_row=3, rfq_id="", vendor_raw="aviall",
                                                   part_number="AN960-10L", qty_needed="")]
    a = adapter()
    await a.search_many(page, rows)
    assert portal.lines == ["MS24665-132, 5", "AN960-10L"]
    assert not await page.locator("boeing-generic-popover").count()      # closed
    assert a._actions >= 4                                                # each step logged


SUPPLIER_PAGE = """<html><body><div>2 results</div>
<div data-testid="productListView"><a href="/cpd/bdi_a1%3D1f">A1</a>
 <div data-testid="panda-supplierName"><span> Boeing Distribution (formerly Aviall) </span></div>
 <span>$10.10 each</span><a href="/support">Request Support</a></div>
<div data-testid="productListView"><a href="/cpd/bdsis4_a1">A1</a>
 <div data-testid="panda-supplierName"><span>Boeing Distribution Services</span></div>
 <span>$170.00 PK</span><a href="/support">Request Support</a></div></body></html>"""


@pytest.mark.asyncio
async def test_only_the_aviall_suppliers_cards_are_read(page_for):
    page = await page_for(MockAviall(mfa=False))
    await page.set_content(SUPPLIER_PAGE)
    llm = FakeLlm()
    a = adapter(llm)
    await a._reveal(page)
    text = await page.inner_text("body")
    assert "$10.10" in text and "$170.00" not in text            # the other supplier's card left out
    await page.set_content(SUPPLIER_PAGE)
    await adapter(llm, supplier="")._reveal(page)                # "" reads every supplier
    assert "$170.00" in await page.inner_text("body")


def test_an_availability_date_becomes_days_and_the_line_is_quoted():
    from datetime import date
    from sourcing.models import Offer, ResultRow
    offer = Offer(part_number="MS20995C32=8D", price="76.95", qty_available="Out of stock",
                  lead_time="Available 11/26/2026")                 # "On Order (Available ...)"
    AviallAdapter._date_to_days(offer, today=date(2026, 11, 2))
    assert offer.lead_time == "24 days"
    row = ResultRow(rfq_id="", vendor="Aviall", part_number=offer.part_number,
                    requested_part=offer.part_number, price="76.95", qty_available="Out of stock",
                    condition="NE", lead_time=offer.lead_time, qty_needed="1",
                    fetched_at="2026-11-02 12:00:00Z")
    rec = row.as_rfq_record()
    assert (rec["Type Quoted"], rec["Unit Price"], rec["Delivery"]) == ("Outright", "76.95", "24")
    past = Offer(part_number="X", price="1", qty_available="Out of stock",
                 lead_time="Available 10/01/2026")
    AviallAdapter._date_to_days(past, today=date(2026, 11, 2))
    assert past.lead_time is None                                    # product page read instead


class _ReplacementPortal:
    """A results page with a Superseded card whose replacement has no card of its
    own (saved page 2026-11-02: 161A2114-1=IH -> View Replacement, /cpd/...=ag)."""

    def __init__(self):
        self.gets: list[str] = []

    async def handle(self, route):
        path = urlparse(route.request.url).path
        self.gets.append(path)
        if path.startswith("/cpd/"):
            body = ("<html><body><h1>161A2114-1=AG</h1><p>Stop Valve</p><p>$412.00 each</p>"
                    "<p>In Stock (7)</p><p>Lead Time (Days) 60</p></body></html>")
        else:
            body = ('<html><body><div data-testid="productListView"><span> [full part number: '
                    '161A2114-1=IH] </span>161A2114-1 Superseded <p>Superseded by part number '
                    '161A2114-1=AG</p><a data-testid="panda-viewReplacement" target="_blank" '
                    f'href="{HOST}/cpd/bdi_161a2114-1=ag">View Replacement</a></div>'
                    '<div data-testid="productListView"><span> [full part number: 0L6839=1F] '
                    '</span>0L6839 <a data-testid="panda-viewReplacement" '
                    f'href="{HOST}/cpd/bdi_0l6839=1f">View Replacement</a></div></body></html>')
        await route.fulfill(status=200, content_type="text/html", body=body)


@pytest.mark.asyncio
async def test_replacement_without_its_own_card_is_read_from_its_product_page(page_for):
    portal = _ReplacementPortal()
    page = await page_for(portal)
    await page.goto(f"{HOST}/boeingsearch?query=161A2114-1")
    text = await page.inner_text("body")
    added = await adapter()._replacement_pages(page, text)
    assert "Replacement product page 1 of 1" in added
    assert "Superseded card 161A2114-1=IH" in added and "$412.00 each" in added
    # 0L6839=1F has its own card in the results: its product page isn't opened
    assert portal.gets.count("/cpd/bdi_161a2114-1=ag") == 1
    assert "/cpd/bdi_0l6839=1f" not in portal.gets


@pytest.mark.asyncio
async def test_replacement_number_only_goes_to_the_note_when_substitutes_are_off(page_for):
    # Team, 2026-11-04: the replacement of a Superseded card is listed in the
    # requested part's Internal Comments; its page isn't opened, no entry.
    portal = _ReplacementPortal()
    page = await page_for(portal)
    await page.goto(f"{HOST}/boeingsearch?query=161A2114-1")
    ad = adapter()
    assert not ad.show_substitutes
    await ad._note_replacements(page)
    assert ad.listed_alternates(RequestRow(sheet_row=2, rfq_id="", vendor_raw="Aviall",
                                           part_number="161A2114-1=IH")) == ["161A2114-1=AG"]
    # asked without the =IH code: the same card
    assert ad.listed_alternates(RequestRow(sheet_row=2, rfq_id="", vendor_raw="Aviall",
                                           part_number="161A2114-1")) == ["161A2114-1=AG"]
    # its "replacement" is its own number: nothing to list
    assert ad.listed_alternates(RequestRow(sheet_row=2, rfq_id="", vendor_raw="Aviall",
                                           part_number="0L6839=1F")) == []
    assert not any(g.startswith("/cpd/") for g in portal.gets)
    on = adapter(show_substitutes=True)
    assert on.listed_alternates(RequestRow(sheet_row=2, rfq_id="", vendor_raw="Aviall",
                                           part_number="161A2114-1=IH")) == []
