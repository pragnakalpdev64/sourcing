"""Run the real ProponentAdapter (Playwright) against mock Procart pages.

The mock markup is a guess at Procart's ASP.NET pages; the point is to check
the adapter's control flow (login, TOTP, wrong password, search, expiry), not
Proponent's real markup. Skipped if no local Chromium is installed.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors.base import InvalidCredentials, MfaRequired, SessionExpired
from sourcing.vendors.proponent import ProponentAdapter

SEED = pyotp.random_base32()

LOGIN_HTML = """<html><body><h1>Procart Login</h1>{error}
<form method="post" action="/Login.aspx">
  <input id="ctl00_Main_UserName" name="user" type="text">
  <input id="ctl00_Main_Password" name="pass" type="password">
  <input type="submit" value="Log In">
</form></body></html>"""
MFA_HTML = """<html><body><p>Enter the verification code from your authenticator app</p>
<form method="post" action="/Mfa.aspx"><input name="otpCode" type="text">
<button type="submit">Verify</button></form></body></html>"""
HOME_HTML = """<html><body><a href="/Logout.aspx">Log out</a>
<form method="get" action="/Search.aspx">{search}</form></body></html>"""
RESULTS_HTML = "<html><body><table><tr><td>{pn}</td><td>NE</td><td>USD 42.50</td><td>Qty 7</td></tr></table></body></html>"
NO_RESULTS_HTML = "<html><body>No results found for {pn}</body></html>"


class MockProcart:
    def __init__(self, *, mfa=False, search_markup='<input id="ctl00_txtPartNumber" name="pn" type="text">',
                 login_error="<p>Invalid username or password.</p>"):
        self.mfa = mfa
        self.login_error = login_error   # what a failed login shows
        self.search_markup = search_markup
        self.signed_in = False
        self.pending_mfa = False
        self.searches: list[str] = []   # results addresses requested
        self.results_pages: list[str] = []  # served in order for results addresses, if set

    async def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        path = url.path.lower()
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        html = None

        if path == "/login.aspx":
            if req.method == "POST":
                if form.get("user") == "jane" and form.get("pass") == "pw":
                    if self.mfa:
                        self.pending_mfa, html = True, MFA_HTML
                    else:
                        self.signed_in, html = True, HOME_HTML.format(search=self.search_markup)
                else:
                    html = LOGIN_HTML.format(error=self.login_error)
            else:
                html = LOGIN_HTML.format(error="")
        elif path == "/mfa.aspx" and self.pending_mfa:
            if pyotp.TOTP(SEED).verify(form.get("otpCode", ""), valid_window=1):
                self.signed_in, self.pending_mfa = True, False
                html = HOME_HTML.format(search=self.search_markup)
            else:
                html = MFA_HTML
        elif path == "/gone.aspx":   # an address the site no longer serves: back home
            await route.fulfill(status=302, headers={"Location": "/"})
            return
        elif not self.signed_in:
            html = LOGIN_HTML.format(error="")  # Procart shows the login form when logged out
        elif path in ("/search.aspx", "/partsearchresults.aspx"):
            self.searches.append(req.url)
            query = parse_qs(url.query)
            pn = (query.get("pn") or query.get("q7") or query.get("id") or [""])[0]
            html = (self.results_pages.pop(0) if self.results_pages else
                    (RESULTS_HTML if pn.startswith("35010") else NO_RESULTS_HTML).format(pn=pn))
        else:
            html = HOME_HTML.format(search=self.search_markup)
        await route.fulfill(status=200, content_type="text/html", body=html)


class FakeExtractor:
    def __init__(self):
        self.picked = 0

    async def extract_offers(self, *, page_text, page_url, part_number, **_):
        if "No results" in page_text:
            return False, "no_results", [], LlmUsage(calls=1)
        return True, "results", [Offer(part_number, "42.50", "USD", "7", "NE", None, page_url)], LlmUsage(calls=1)

    async def pick_search_input(self, inputs):
        self.picked += 1
        idx = next((i["index"] for i in inputs if (i.get("placeholder") or "").startswith("Enter P/N")), None)
        return idx, LlmUsage(calls=1)


def cred(password="pw", totp=None):
    return VendorCredential("proponent", "jane@co.com", "sourcing-vendor-proponent__jane-co-com", "jane", password, totp)


def row(pn):
    return RequestRow(sheet_row=2, rfq_id="R1", vendor_raw="Proponent", part_number=pn)


@pytest_asyncio.fixture
async def page_for():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")

    async def make(mock: MockProcart):
        page = await browser.new_page()
        await page.route("https://procart.proponent.com/**", mock.handle)
        return page

    yield make
    await browser.close()
    await pw.stop()


@pytest.mark.asyncio
async def test_login_search_and_not_found(page_for):
    mock = MockProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())

    assert not await adapter.is_authenticated(page)
    await adapter.login(page, cred())
    assert await adapter.is_authenticated(page)
    assert adapter.learned["username_input"] == "input[id*='username' i]"

    found = await adapter.search(page, row("35010-38"))
    assert found.found and found.offers[0].price == "42.50"
    # Opened the results address directly: no search box needed.
    assert mock.searches == ["https://procart.proponent.com/PartSearchResults.aspx?id=35010-38"]
    assert found.offers[0].source_url == mock.searches[0]
    assert "search_input" not in adapter.learned

    missing = await adapter.search(page, row("XYZ-1"))
    assert not missing.found


@pytest.mark.asyncio
async def test_search_form_teaches_the_results_address_for_later_searches(page_for):
    mock = MockProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    adapter.default_part_url_template = ""     # nothing known: start with the form
    await adapter.login(page, cred())

    assert (await adapter.search(page, row("35010-38"))).found
    assert adapter.learned["search_input"] == "input[id*='partnumber' i]"
    template = "https://procart.proponent.com/Search.aspx?pn={part}"
    assert adapter.learned["part_url_template"] == template

    # Next run: straight to the address, from the home page or anywhere else.
    adapter2 = ProponentAdapter(extractor=FakeExtractor(), selectors=adapter.learned)
    await page.goto("https://procart.proponent.com/")
    outcome = await adapter2.search(page, row("35010-39 A"))
    assert outcome.found and mock.searches[-1].endswith("/Search.aspx?pn=35010-39+A")


@pytest.mark.asyncio
async def test_a_direct_address_that_lands_elsewhere_falls_back_to_the_form(page_for):
    mock = MockProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor(), vendor_config={
        "part_url_template": "https://procart.proponent.com/Gone.aspx?id={part}"})
    await adapter.login(page, cred())

    assert (await adapter.search(page, row("35010-38"))).found
    assert adapter.part_url_failed and adapter.learned["search_input"]
    assert mock.searches[-1].endswith("/Search.aspx?pn=35010-38")
    # The configured address is the admin's call: not replaced by a learned one.
    assert "part_url_template" not in adapter.learned


@pytest.mark.asyncio
async def test_wrong_password_raises_invalid_credentials(page_for):
    page = await page_for(MockProcart())
    with pytest.raises(InvalidCredentials):
        await ProponentAdapter(extractor=FakeExtractor()).login(page, cred(password="nope"))


@pytest.mark.asyncio
@pytest.mark.parametrize("error, retryable", [
    ("", False),                                        # no message: maybe a rejection
    ("<h1>Service Unavailable</h1>", True),             # the server failed, nothing judged
])
async def test_a_failed_login_says_whether_a_second_attempt_is_safe(page_for, error, retryable):
    from sourcing.vendors.base import LoginFailed

    page = await page_for(MockProcart(login_error=error))
    with pytest.raises(LoginFailed) as caught:
        await ProponentAdapter(extractor=FakeExtractor()).login(page, cred(password="nope"))
    assert caught.value.retryable is retryable


@pytest.mark.asyncio
async def test_a_security_message_stops_the_login(page_for):
    from sourcing.vendors.base import LoginBlocked

    page = await page_for(MockProcart(login_error="<p>Too many requests. Try later.</p>"))
    with pytest.raises(LoginBlocked):
        await ProponentAdapter(extractor=FakeExtractor()).login(page, cred(password="nope"))


@pytest.mark.asyncio
async def test_totp_is_submitted_automatically(page_for):
    mock = MockProcart(mfa=True)
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred(totp=SEED))
    assert mock.signed_in and adapter.learned["mfa_code_input"]


@pytest.mark.asyncio
async def test_2fa_without_seed_needs_a_person(page_for):
    page = await page_for(MockProcart(mfa=True))
    with pytest.raises(MfaRequired) as info:
        await ProponentAdapter(extractor=FakeExtractor()).login(page, cred())
    assert info.value.method == "totp_no_seed"


@pytest.mark.asyncio
async def test_expired_session_is_detected_during_search(page_for):
    mock = MockProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    mock.signed_in = False
    with pytest.raises(SessionExpired):
        await adapter.search(page, row("35010-38"))


@pytest.mark.asyncio
async def test_claude_picks_search_box_when_heuristics_miss(page_for):
    mock = MockProcart(search_markup='<input id="ctl00_q7" name="q7" placeholder="Enter P/N" type="text">'
                                     '<input id="ctl00_qty" name="qty" type="text"><input type="submit" value="Go">')
    page = await page_for(mock)
    extractor = FakeExtractor()
    form_only = {"part_url_template": ""}
    adapter = ProponentAdapter(extractor=extractor, vendor_config=form_only)
    await adapter.login(page, cred())
    outcome = await adapter.search(page, row("35010-38"))
    assert outcome.found
    assert extractor.picked == 1
    assert adapter.learned["search_input"] == '[id="ctl00_q7"]'

    # Next run starts from the saved selector: no second Claude call.
    adapter2 = ProponentAdapter(extractor=extractor, selectors=adapter.learned,
                                vendor_config=form_only)
    assert (await adapter2.search(page, row("35010-39"))).found
    assert extractor.picked == 1


# -- Mock built from Procart's real public markup (checked 2026-10-09) -------------
COOKIE_BAR = """<div id="catapult-cookie-bar"><span>This site uses cookies:</span>
<button type="button" id="catapultCookie" onclick="document.cookie='GDPRCookie=1;path=/';
location.reload();">Accept</button></div>"""
REAL_HEADER_OUT = '<div class="loginUserInfo"><a href="/Login.aspx" id="loginbutton" class="button secondary">Login</a></div>'
REAL_HEADER_IN = '<div class="loginUserInfo"><a href="/Logout.aspx">Logout</a></div>'
REAL_LOGIN = """<html><body>{header}{error}<form method="post" action="/Login.aspx">
<input name="ctl00$MainContent$txtUserName" type="text" id="MainContent_txtUserName" />
<input name="ctl00$MainContent$txtPassword" type="password" id="MainContent_txtPassword" />
<input type="submit" name="ctl00$MainContent$btnLogin" value="Login to Procart" id="MainContent_btnLogin" />
</form>{cookie}</body></html>"""
REAL_HOME = """<html><body>{header}<h1>Welcome to Proponent ProCart</h1>
<form method="get" action="/Search.aspx"><input id="ctl00_txtPartNumber" name="pn" type="text"></form>
{cookie}</body></html>"""


class RealisticProcart:
    """Public home and search pages (header shows a Login button when logged out),
    and a cookie bar whose Accept reloads the page, like the real site."""

    def __init__(self):
        self.signed_in = False
        self.cookie_accepted = False

    async def handle(self, route):
        req = route.request
        path = urlparse(req.url).path.lower()
        self.cookie_accepted = self.cookie_accepted or "GDPRCookie" in (req.headers.get("cookie") or "")
        cookie = "" if self.cookie_accepted else COOKIE_BAR
        header = REAL_HEADER_IN if self.signed_in else REAL_HEADER_OUT
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}

        if path == "/login.aspx":
            if req.method == "POST":
                if (form.get("ctl00$MainContent$txtUserName") == "jane"
                        and form.get("ctl00$MainContent$txtPassword") == "pw"):
                    self.signed_in = True
                    html = REAL_HOME.format(header=REAL_HEADER_IN, cookie=cookie)
                else:
                    html = REAL_LOGIN.format(header=header, cookie=cookie,
                                             error="<p>Invalid username or password.</p>")
            else:
                html = REAL_LOGIN.format(header=header, cookie=cookie, error="")
        elif path in ("/search.aspx", "/partsearchresults.aspx"):
            query = parse_qs(urlparse(req.url).query)
            pn = (query.get("pn") or query.get("id") or [""])[0]
            body = (RESULTS_HTML if self.signed_in and pn.startswith("35010")
                    else NO_RESULTS_HTML).format(pn=pn)
            html = body.replace("<body>", f"<body>{header}")
        else:
            html = REAL_HOME.format(header=header, cookie=cookie)
        await route.fulfill(status=200, content_type="text/html", body=html)


@pytest.mark.asyncio
async def test_public_home_page_is_not_treated_as_signed_in(page_for):
    page = await page_for(RealisticProcart())
    assert not await ProponentAdapter(extractor=FakeExtractor()).is_authenticated(page)


@pytest.mark.asyncio
async def test_cookie_banner_is_accepted_before_the_login_form_is_filled(page_for):
    mock = RealisticProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    assert mock.cookie_accepted and mock.signed_in
    assert adapter.learned["username_input"] == "#MainContent_txtUserName"
    assert adapter.learned["submit_button"] == "#MainContent_btnLogin"
    assert await adapter.is_authenticated(page)


@pytest.mark.asyncio
async def test_logged_out_search_is_session_expired_not_a_false_not_found(page_for):
    page = await page_for(RealisticProcart())
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await page.goto("https://procart.proponent.com/")
    with pytest.raises(SessionExpired):
        await adapter.search(page, row("XYZ-1"))


# -- Login URL: candidates, validation, and starting from a home page ------------

def test_login_url_candidates_order_and_validation():
    from sourcing.vendors.base import check_login_url

    assert check_login_url("http://procart.proponent.com/Login.aspx", ("procart.proponent.com",))
    assert check_login_url("https://evil.example.com/Login.aspx", ("procart.proponent.com",))
    assert check_login_url("https://procart.proponent.com/Login.aspx", ("procart.proponent.com",)) is None

    secret_url = VendorCredential("proponent", "_shared", "sourcing-vendor-proponent", "jane", "pw",
                                  None, "https://evil.example.com/login")
    adapter = ProponentAdapter(
        vendor_config={"login_url": "https://procart.proponent.com/"},
        selectors={"login_page_url": "https://procart.proponent.com/Login.aspx"},
    )
    # config first, then the learned page; the off-site secret URL is dropped;
    # the default equals the learned page, so it isn't repeated
    assert [u for _, u in adapter.login_url_candidates(secret_url)] == [
        "https://procart.proponent.com/", "https://procart.proponent.com/Login.aspx"]


@pytest.mark.asyncio
async def test_login_from_home_page_follows_the_login_link_and_remembers_it(page_for):
    mock = RealisticProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor(),
                               vendor_config={"login_url": "https://procart.proponent.com/"})
    await adapter.login(page, cred())
    assert mock.signed_in
    assert adapter.learned["login_page_url"] == "https://procart.proponent.com/Login.aspx"


@pytest.mark.asyncio
async def test_no_usable_login_url_fails_clearly():
    from sourcing.vendors.base import LoginFailed

    adapter = ProponentAdapter(vendor_config={"allowed_hosts": ["other.example.com"]})
    with pytest.raises(LoginFailed, match="No usable login URL"):
        await adapter.login(page=_NoPage(), cred=cred())


class _NoPage:
    def set_default_timeout(self, _):
        pass

    def set_default_navigation_timeout(self, _):
        pass


# Termly-style consent dialog, injected by script and covering the whole page
# (seen on the live site 2026-10-09: it intercepted the click on the Login button).
TERMLY = """<script>
if (!document.cookie.includes('termly=')) {
  const host = document.createElement('div'); host.id = 'termly-code-snippet-support';
  host.innerHTML = '<dialog open aria-label="Cookie Consent Prompt" style="position:fixed;inset:0;' +
    'width:100vw;height:100vh;margin:0;z-index:9999;background:#fff"><p>We use cookies</p>' +
    '<button id="t-accept">Accept</button><button id="t-decline">Decline</button></dialog>';
  document.body.appendChild(host);
  host.querySelectorAll('button').forEach(b => b.onclick = () => {
    document.cookie = 'termly=' + b.id + ';path=/'; host.remove(); });
}
</script>"""


class TermlyProcart(RealisticProcart):
    async def handle(self, route):
        original = route.fulfill

        async def fulfill(**kw):
            kw["body"] = kw["body"].replace("</body>", TERMLY + "</body>")
            await original(**kw)

        route.fulfill = fulfill
        await super().handle(route)


@pytest.mark.asyncio
async def test_consent_dialog_blocking_the_login_link_is_declined_then_login_works(page_for):
    mock = TermlyProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor(),
                               vendor_config={"login_url": "https://procart.proponent.com/"})
    await adapter.login(page, cred())
    assert mock.signed_in
    cookies = {c["name"]: c["value"] for c in await page.context.cookies()}
    assert cookies.get("termly") == "t-decline"   # the privacy-preserving choice


# -- Bulk search + logout on the realistic mock ----------------------------------
BULK_FORM = """<form method="post" action="/Default.aspx"><span id="MainContent_lblBulkSearchMsg"></span>
<textarea name="ctl00$MainContent$txtBulkPart" id="MainContent_txtBulkPart" rows="5"></textarea>
<input type="submit" name="ctl00$MainContent$btnBulkSearch" value="SEARCH" id="MainContent_btnBulkSearch"></form>"""


class BulkProcart(RealisticProcart):
    def __init__(self):
        super().__init__()
        self.bulk_lines = None

    async def handle(self, route):
        req = route.request
        path = urlparse(req.url).path.lower()
        if path == "/default.aspx" and req.method == "POST":
            form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
            self.bulk_lines = form.get("ctl00$MainContent$txtBulkPart", "").splitlines()
            rows = "".join(f"<tr><td>{l.split()[0]}</td><td>NE</td><td>USD 42.50</td></tr>"
                           for l in self.bulk_lines if l.startswith("35010"))
            header = REAL_HEADER_IN if self.signed_in else REAL_HEADER_OUT
            await route.fulfill(status=200, content_type="text/html",
                                body=f"<html><body>{header}<table>{rows}</table></body></html>")
            return
        if path == "/logout.aspx":
            self.signed_in = False
        original = route.fulfill

        async def fulfill(**kw):
            kw["body"] = kw["body"].replace("</h1>", "</h1>" + BULK_FORM)
            await original(**kw)

        route.fulfill = fulfill
        await super().handle(route)


async def _bulk_extract(self, *, page_text, page_url, parts):
    by_part = {p["part_number"]: [Offer(p["part_number"], "42.50", "USD", None, "NE", None, page_url)]
               for p in parts if p["part_number"] in page_text}
    return "results", by_part, LlmUsage(calls=1)

FakeExtractor.extract_offers_bulk = _bulk_extract


@pytest.mark.asyncio
async def test_bulk_search_sends_one_part_and_qty_per_line(page_for):
    mock = BulkProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    rows = [RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="35010-38",
                       qty_needed="2"),
            RequestRow(sheet_row=3, rfq_id="R", vendor_raw="Proponent", part_number="XYZ-1")]
    outcomes, usage = await adapter.search_many(page, rows)
    assert mock.bulk_lines == ["35010-38\t2", "XYZ-1\t1"]
    assert list(outcomes) == [rows[0].row_id] and usage.calls == 1


@pytest.mark.asyncio
async def test_bulk_search_logged_out_is_session_expired(page_for):
    page = await page_for(BulkProcart())
    with pytest.raises(SessionExpired):
        await ProponentAdapter(extractor=FakeExtractor()).search_many(page, [row("35010-38")])


@pytest.mark.asyncio
async def test_logout_signs_out(page_for):
    mock = BulkProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    assert await adapter.logout(page)
    assert not mock.signed_in
    assert adapter.learned["logout_link"]


@pytest.mark.asyncio
async def test_a_search_that_does_not_submit_is_an_error_not_results(page_for):
    """Enter in a form with two text fields and no submit button submits nothing.
    The old code then read the unchanged search page as the results."""
    mock = MockProcart(search_markup='<input id="ctl00_q7" name="q7" placeholder="Enter P/N" type="text">'
                                     '<input id="ctl00_qty" name="qty" type="text">')
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor(),
                               selectors={"search_input": '[id="ctl00_q7"]'},
                               vendor_config={"action_timeout_seconds": 1,
                                              "part_url_template": ""})
    await adapter.login(page, cred())
    with pytest.raises(RuntimeError, match="did not submit"):
        await adapter.search(page, row("35010-38"))


# -- Paged bulk results ("Viewing results 1 - 10 of 24", seen 2026-10-12) --------
class PagedProcart(BulkProcart):
    """Bulk results 10 per page, with an optional "Results per page" dropdown
    (autopostback) and first/prev/"Page [n] of N"/next/last controls."""

    def __init__(self, sizes=(10, 25, 50, 100), named_next=True):
        super().__init__()
        self.sizes, self.named_next = sizes, named_next
        self.size, self.page, self.found, self.postbacks = 10, 1, [], 0

    def _results(self) -> str:
        total = len(self.found)
        pages = max(1, -(-total // self.size))
        first = (self.page - 1) * self.size
        shown = self.found[first:first + self.size]
        rows = "".join(f"<tr><td>{pn}</td><td>USD 42.50</td></tr>" for pn in shown)
        options = "".join(f'<option value="{n}"{" selected" if n == self.size else ""}>'
                          f'Results per page: {n}</option>' for n in self.sizes)
        dropdown = (f'<select class="ui-pg-selbox" title="Records per Page" disabled '
                    f'style="display:none">{options}</select>'
                    f'<select name="ctl00$MainContent$ddlPageSize" id="MainContent_ddlPageSize" '
                    f'onchange="this.form.submit()">{options}</select>') if self.sizes else ""
        nxt = ('<input type="submit" name="ctl00$MainContent$btnNext" id="MainContent_btnNext" '
               'title="Next Page" value=">>">' if self.named_next else
               '<input type="submit" name="ctl00$MainContent$ib3" value=">>">')
        last_page = self.page >= pages
        pager = (f'<span>Viewing results {first + 1} - {first + len(shown)} of {total}</span>{dropdown}'
                 f'<input type="submit" name="ctl00$MainContent$ib1" value="|<">'
                 f'<input type="submit" name="ctl00$MainContent$ib2" value="<<">'
                 f'<span>Page <input type="text" name="pg" value="{self.page}"> of {pages}</span>'
                 + ("" if last_page else nxt) +
                 f'<input type="submit" name="ctl00$MainContent$ib4" value=">|">')
        return (f"<html><body>{REAL_HEADER_IN}<form method='post' action='/PartSearchResults.aspx'>"
                f"<table>{rows}</table>{pager}</form></body></html>")

    async def handle(self, route):
        req = route.request
        path = urlparse(req.url).path.lower()
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        if path == "/default.aspx" and req.method == "POST":
            self.bulk_lines = form.get("ctl00$MainContent$txtBulkPart", "").splitlines()
            self.found = [l.split()[0] for l in self.bulk_lines if l.startswith("P")]
            self.page = 1
        elif path == "/partsearchresults.aspx" and req.method == "POST":
            self.postbacks += 1
            size = form.get("ctl00$MainContent$ddlPageSize")
            if any(k.endswith(("btnNext", "ib3")) for k in form):
                self.page += 1
            elif size and int(size) != self.size:
                self.size, self.page = int(size), 1
        else:
            await super().handle(route)
            return
        await route.fulfill(status=200, content_type="text/html", body=self._results())


def _paged_rows():
    rows = [RequestRow(sheet_row=i + 2, rfq_id="R", vendor_raw="Proponent",
                       part_number=f"P{i:02d}") for i in range(24)]
    return rows + [RequestRow(sheet_row=26, rfq_id="R", vendor_raw="Proponent", part_number="XYZ-1")]


@pytest.mark.asyncio
async def test_bulk_results_page_size_is_raised_so_all_parts_are_on_one_page(page_for):
    mock = PagedProcart()
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    rows = _paged_rows()
    outcomes, usage = await adapter.search_many(page, rows)
    assert mock.size == 25                       # smallest size that fits 25 parts
    assert len(outcomes) == 24 and rows[-1].row_id not in outcomes
    assert usage.calls == 1


@pytest.mark.asyncio
async def test_bulk_results_walks_every_page_when_the_size_cannot_be_raised(page_for):
    mock = PagedProcart(sizes=(), named_next=False)   # no dropdown, unlabeled next button
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    rows = _paged_rows()
    outcomes, usage = await adapter.search_many(page, rows)
    assert mock.page == 3 and len(outcomes) == 24
    assert usage.calls == 1                      # three small pages, one LLM call


@pytest.mark.asyncio
async def test_bulk_results_largest_size_then_remaining_pages(page_for):
    mock = PagedProcart(sizes=(5, 10, 20))
    page = await page_for(mock)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter.login(page, cred())
    outcomes, _ = await adapter.search_many(page, _paged_rows())
    assert mock.size == 20 and mock.page == 2 and len(outcomes) == 24


def test_pager_helpers():
    from sourcing.vendors.base import BulkSearchUnavailable
    from sourcing.vendors.proponent import _chunk_pages, _page_size_choice, _pager

    assert _pager("Viewing results 1 - 10 of 24  Results per page") == (1, 10, 24)
    assert _pager("Viewing results 1,001 – 1,010 of 2,400") == (1001, 1010, 2400)
    assert _pager("no pager here") is None

    selects = [{"index": 0, "id": "ddlSort", "name": None, "visible": True,
                "options": [{"value": "a", "text": "Price", "selected": True}]},
               {"index": 1, "id": "MainContent_ddlPageSize", "name": None, "visible": True,
                "options": [{"value": str(n), "text": f"Results per page: {n}",
                             "selected": n == 10} for n in (10, 25, 50, 100)]}]
    assert _page_size_choice(selects, 24) == ('select[data-sourcing-sel="1"]', "25", 25, 10)
    jq = {"index": 10, "id": None, "name": None, "title": "Records per Page",
          "className": "ui-pg-selbox", "visible": True, "enabled": True,
          "options": [{"value": str(n), "text": str(n), "selected": n == 10} for n in (10, 50)]}
    hidden = {**jq, "index": 9, "visible": False}
    assert _page_size_choice([hidden, jq], 33) == ('select[data-sourcing-sel="10"]', "50", 50, 10)
    assert _page_size_choice(selects, 400)[2] == 100
    assert _page_size_choice(selects[:1], 24) is None

    assert _chunk_pages(["a" * 10], 50) == ["a" * 10]
    assert len(_chunk_pages(["a" * 20, "b" * 20, "c" * 20], 100)) == 2  # 2 pages per chunk
    with pytest.raises(BulkSearchUnavailable):
        _chunk_pages(["a" * 100], 50)


def test_too_long_grid_records_are_split_between_records():
    from sourcing.vendors.base import BulkResultsIncomplete
    from sourcing.vendors.proponent import _chunk_pages

    head = "Results read from the page's results grid (#jQGridDemo): 6 record(s)."
    records = [f"Record {n}:\n  Cust_part: P{n}\n  Price: {n}.00\n" + "  x" * 100
               for n in range(1, 7)]
    text = "\n\n".join([head, *records])
    chunks = _chunk_pages([text], 800)
    assert len(chunks) > 1 and all(len(c) <= 800 and c.startswith(head) for c in chunks)
    assert [r for c in chunks for r in c.split("\n\n")[1:]] == records   # none lost or cut
    with pytest.raises(BulkResultsIncomplete):   # one record alone is too long
        _chunk_pages([text], 300)


# -- Page details innerText leaves out --------------------------------------------
GRID_HTML = """<html><body><table>
<tr><td aria-describedby="grid_PartNumber">ABC-1</td><td aria-describedby="grid_Price">12.00</td>
<td aria-describedby="grid_Condition" style="display:none">NE</td>
<td aria-describedby="grid_Stock"><img src="x.png" alt="Factory new"><span title="Ships in 5 days">5</span></td>
<td><a href="https://procart.proponent.com/Part.aspx?pn=ABC-1">details</a></td></tr>
<tr><td>Plain row</td><td>nothing hidden</td></tr>
</table></body></html>"""


@pytest.mark.asyncio
async def test_page_text_adds_hidden_columns_and_tooltips(page_for, tmp_path, monkeypatch):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(GRID_HTML)
    monkeypatch.setenv("DEBUG_DUMP_PAGES", "1")
    monkeypatch.setattr(proponent, "DUMP_DIR", tmp_path)
    text = await proponent._page_text(page, "bulk")
    details = text.split("--- Page details", 1)[1]
    assert "Condition (hidden): NE" in details
    assert "Factory new" in details and "Ships in 5 days" in details
    assert "link: https://procart.proponent.com/Part.aspx?pn=ABC-1" in details
    assert "Plain row" not in details                 # rows with nothing extra are left out
    assert len(list(tmp_path.glob("*.txt"))) == 1 and len(list(tmp_path.glob("*.html"))) == 1


EXPAND_HTML = """<html><body><a id="ea" href="#"
 onclick="document.getElementById('d').style.display='block'; this.innerText='Collapse All Details'; return false;">Expand All Details</a>
<table><tr><td>ABC-1</td><td>12.00</td></tr></table>
<div id="d" style="display:none">Warehouse: Brea, CA  Date Next In Nov 26, 2026  Qty Due In 30</div>
</body></html>"""


@pytest.mark.asyncio
async def test_expand_all_details_is_clicked_before_reading(page_for):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(EXPAND_HTML)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    assert "Date Next In Nov 26, 2026" in text
    await adapter._expand_all_details(page)            # now reads "Collapse All": left alone
    assert await page.inner_text("#ea") == "Collapse All Details"


# -- Results read from the grid's own records ---------------------------------------
# A stand-in for jQuery + jqGrid with loadonce:true: every row in memory, only 10
# drawn on the page, so page text alone would miss the rest.
def _grid_page(rows, total=None, pager_total=None):
    data = __import__("json").dumps(rows)
    pager = (f"<span>Viewing results 1 - {min(10, len(rows))} of {pager_total}</span>"
             if pager_total else "")
    return f"""<html><body>{REAL_HEADER_IN}<table id="jQGridDemo" class="ui-jqgrid-btable"></table>{pager}
<script>
const DATA = {data};
window.jQuery = function (sel) {{
  const found = document.querySelector(sel);
  return {{ length: found ? 1 : 0, first() {{ return this; }}, attr() {{ return 'jQGridDemo'; }},
           jqGrid(cmd, arg) {{
             if (cmd === 'getGridParam') return arg === 'data' ? DATA : {total if total is not None else 'DATA.length'};
             return []; }} }};
}};
window.jQuery.fn = {{ jqGrid: true }};
</script></body></html>"""


GRID_ROWS = [
    {"_id_": "1", "Cust_part": "35010-38", "Item_number": "35010-38", "Price": "$42.50",
     "QtyAvailable": "7", "Item_description1": "BOLT", "isAOG": False,
     "other_fields": "<b>If out of stock</b>, approximate lead time is 59 days",
     "LstWarehouses": [{"DisplayName": "Brea, CA", "Due_date": "Nov 26, 2026", "TotQty": 30}]},
    {"_id_": "2", "Cust_part": "410946", "Item_number": "2758", "Price": "8,460.95",
     "QtyAvailable": "122"},
]


def test_grid_records_are_formatted_one_labeled_block_per_row():
    from sourcing.vendors.grid import format_records

    text = format_records(GRID_ROWS)
    assert text.startswith("Record 1:\n  Cust_part: 35010-38")
    assert "other_fields: If out of stock , approximate lead time is 59 days" in text
    assert "LstWarehouses #1:\n      DisplayName: Brea, CA" in text and "TotQty: 30" in text
    assert "isAOG" not in text and "_id_" not in text          # noise left out
    assert "Record 2:\n  Cust_part: 410946\n  Item_number: 2758" in text


@pytest.mark.asyncio
async def test_grid_records_are_read_from_the_page(page_for):
    page = await page_for(MockProcart())
    await page.set_content(_grid_page(GRID_ROWS))
    adapter = ProponentAdapter(extractor=FakeExtractor())
    text = await adapter._grid_text(page, "bulk")
    assert text.startswith("Results read from the page's results grid (#jQGridDemo): 2 record(s)")
    assert "Item_number: 2758" in text


@pytest.mark.asyncio
async def test_grid_with_fewer_records_than_the_pager_falls_back_to_page_text(page_for):
    page = await page_for(MockProcart())
    await page.set_content(_grid_page(GRID_ROWS, pager_total=24))
    assert await ProponentAdapter(extractor=FakeExtractor())._grid_text(page, "bulk") is None


@pytest.mark.asyncio
async def test_page_without_a_grid_falls_back_at_once(page_for):
    import time

    page = await page_for(MockProcart())
    await page.set_content("<html><body><table><tr><td>35010-38</td></tr></table></body></html>")
    started = time.time()
    assert await ProponentAdapter(extractor=FakeExtractor())._grid_text(page, "bulk") is None
    assert time.time() - started < 2


def test_source_url_names_this_searchs_part_not_the_first_one():
    from sourcing.vendors.proponent import part_results_url

    base = "https://procart.proponent.com/PartSearchResults.aspx"
    # second search posts back on the first search's page; analytics _gl is dropped
    stale = f"{base}?id=F1101385-01&_gl=1*5n46kw*_up*MQ..*_ga*ODYz"
    assert part_results_url(stale, "4066-0014") == f"{base}?id=4066-0014"
    assert part_results_url(f"{base}?id=F1101385-01", "F1101385-01") == f"{base}?id=F1101385-01"
    assert part_results_url(f"{base}?id=X", "AB 12/3") == f"{base}?id=AB+12%2F3"
    # no id parameter: the address is kept as it is
    assert part_results_url("https://procart.proponent.com/", "X") == "https://procart.proponent.com/"


def test_results_address_template_from_query_or_path():
    from sourcing.vendors.base import fill_part_url, part_url_template

    base = "https://procart.proponent.com/PartSearchResults.aspx"
    assert part_url_template(f"{base}?id=f1101385-01&_gl=1*abc", "F1101385-01") == f"{base}?id={{part}}"
    assert part_url_template(f"{base}?view=grid&id=X1", "X1") == f"{base}?view=grid&id={{part}}"
    assert part_url_template("https://shop.example.com/parts/AB%2012/detail?x=1", "AB 12") == \
        "https://shop.example.com/parts/{part}/detail?x=1"
    # the address doesn't hold this part (a postback page): nothing learned
    assert part_url_template(f"{base}?id=F1101385-01", "4066-0014") is None
    assert part_url_template("https://procart.proponent.com/", "X") is None

    assert fill_part_url(f"{base}?id={{part}}", "AB 12/3") == f"{base}?id=AB+12%2F3"
    assert fill_part_url("https://shop.example.com/parts/{part}", "AB 12/3") == \
        "https://shop.example.com/parts/AB%2012%2F3"


EMPTY_GRID_HTML = _grid_page([]).replace("</table>", "</table><div>No data found.</div>")


class CountingExtractor(FakeExtractor):
    calls = 0

    async def extract_offers(self, **kw):
        self.calls += 1
        return await super().extract_offers(**kw)


@pytest.mark.asyncio
async def test_an_empty_grid_is_loaded_again_then_reported_as_an_empty_page(page_for, monkeypatch):
    import sourcing.vendors.proponent as proponent

    monkeypatch.setattr(proponent, "GRID_WAIT_SECONDS", 0.5)
    mock = MockProcart()
    mock.signed_in, mock.results_pages = True, [EMPTY_GRID_HTML, EMPTY_GRID_HTML]
    page = await page_for(mock)
    extractor = CountingExtractor()
    out = await ProponentAdapter(extractor=extractor).search(page, row("5E2675-1"))
    assert out.empty_page and not out.found and len(mock.searches) == 2
    assert extractor.calls == 0                     # nothing for the LLM to read


@pytest.mark.asyncio
async def test_an_empty_grid_that_fills_on_the_second_load_is_read(page_for, monkeypatch):
    import sourcing.vendors.proponent as proponent

    monkeypatch.setattr(proponent, "GRID_WAIT_SECONDS", 0.5)
    mock = MockProcart()
    mock.signed_in, mock.results_pages = True, [EMPTY_GRID_HTML, _grid_page(GRID_ROWS)]
    page = await page_for(mock)
    out = await ProponentAdapter(extractor=FakeExtractor()).search(page, row("35010-38"))
    assert out.found and not out.empty_page and len(mock.searches) == 2


def test_price_at_qty_needed_reads_the_parts_own_price_break_table():
    from sourcing.vendors.proponent import price_at_qty

    page = ("Results read from the page's results grid (#jQGridDemo): 3 record(s).\n\n"
            "Record 1:\n  Cust_part: AS1895/7-300\n  Price: 23.7\n  Price_breaks: Yes\n"
            "  Expanded details (as shown on the page):\n"
            "    Increasing order quantity to 250 will reduce your price to 22.22\n"
            "    Order Quantity\n    Price\n    1-249\n    $23.70\n    250-499\n    $22.22\n"
            "    500+\n    $20.92\n    Warehouse Name\n\n"
            # a row read off screen runs the table together (seen 2026-10-14)
            "Record 2:\n  Cust_part: M83248/1-010\n  Price_breaks: Yes\n"
            "  Item_description1: approved substitute part for part #AS1895/7-300\n"
            "  Expanded details (as shown on the page):\n"
            "    Loading...Order QuantityPrice1-59 $1.8560-118 $0.66119-238 $0.371910+ $0.10\n\n"
            "Record 3:\n  Cust_part: AS15001-1P\n  Price_breaks: Yes\n"
            "  other_fields: Increasing order quantity to 50 will reduce your price to 2.42")
    assert price_at_qty(page, "AS1895/7-300", "23.70", "2") == "23.70"
    assert price_at_qty(page, "AS1895/7-300", "23.70", "300") == "22.22"
    assert price_at_qty(page, "AS1895/7-300", "23.70", "1,000") == "20.92"
    assert price_at_qty(page, "M83248/1-010", "1.85", "60") == "0.66"   # its own table only
    assert price_at_qty(page, "M83248/1-010", "1.85", "5000") == "0.10"
    assert price_at_qty(page, "AS15001-1P", "6.33", "10") == "6.33"    # below the first break
    assert price_at_qty(page, "AS15001-1P", "6.33", "50") is None      # table not on the page
    assert price_at_qty(page, "AS1895/7-300", "23.70", "1") is None    # the unit price applies
    assert price_at_qty(page, "NOT-THERE", "1.00", "5") is None

    from sourcing.models import Offer
    from sourcing.vendors.proponent import _use_single_unit_price, single_unit_price
    assert single_unit_price(page, "AS1895/7-300") == "23.70"
    assert single_unit_price(page, "M83248/1-010") == "1.85"
    assert single_unit_price(page, "AS15001-1P") is None      # no table on the page
    # the line showed the 250+ price (bulk search sent qty 300): Price becomes 1 piece's
    offer = Offer(part_number="AS1895/7-300", price="22.22", qty_available="5")
    _use_single_unit_price(offer, page)
    assert offer.price == "23.70"
    assert price_at_qty(page, offer.part_number, offer.price, "300") == "22.22"
    offer = Offer(part_number="AS15001-1P", price="6.33", qty_available="5")
    _use_single_unit_price(offer, page)
    assert offer.price == "6.33"                              # kept: nothing to compare with


def test_whole_price_break_table_comes_from_the_parts_own_record():
    from sourcing.models import Offer
    from sourcing.vendors.proponent import _use_price_breaks

    page = ("Record 1:\n  Cust_part: AS1895/7-300\n  Price: 23.7\n  Price_breaks: Yes\n"
            "  Expanded details (as shown on the page):\n"
            "    Order Quantity\n    Price\n    1-249\n    $23.70\n    250-499\n    $22.22\n"
            "    500+\n    $20.92\n\n"
            "Record 2:\n  Cust_part: M83248/1-010\n  Price_breaks: Yes\n"
            "  Expanded details (as shown on the page):\n"
            "    Loading...Order QuantityPrice1-59 $1.8560-118 $0.66119-238 $0.371910+ $0.10\n\n"
            "Record 3:\n  Cust_part: AS15001-1P\n  Price_breaks: Yes\n")
    # the model's copy is replaced by the record's own rows
    offer = Offer(part_number="AS1895/7-300", price="23.70", qty_available="5",
                  price_breaks=[("1", "249", "23.70")])
    _use_price_breaks(offer, page)
    assert offer.price_breaks == [("1", "249", "23.70"), ("250", "499", "22.22"),
                                  ("500", "", "20.92")]
    offer = Offer(part_number="M83248/1-010", price="1.85", qty_available="5")
    _use_price_breaks(offer, page)
    assert offer.price_breaks[:3] == [("1", "59", "1.85"), ("60", "118", "0.66"),
                                      ("119", "238", "0.37")]   # its own table only
    # no table in the record: the model's rows stay; out of stock: nothing is added
    offer = Offer(part_number="AS15001-1P", price="6.33", qty_available="5",
                  price_breaks=[("1", "", "6.33")])
    _use_price_breaks(offer, page)
    assert offer.price_breaks == [("1", "", "6.33")]
    offer = Offer(part_number="AS1895/7-300", qty_available="Out of stock")
    _use_price_breaks(offer, page)
    assert offer.price_breaks == []


def _grid_page_with_subgrids(rows):
    """A jqGrid whose rows are drawn and whose detail rows open only when asked
    (expandSubGridRow), like a row whose details didn't load the first time."""
    data = __import__("json").dumps(rows)
    trs = "".join(f'<tr id="{r["_id_"]}"><td>{r["Cust_part"]}</td></tr>' for r in rows)
    return f"""<html><body>{REAL_HEADER_IN}<table id="jQGridDemo" class="ui-jqgrid-btable">{trs}</table>
<script>
const DATA = {data};
window.expanded = [];
window.jQuery = function (sel) {{
  const found = document.querySelector(sel);
  return {{ length: found ? 1 : 0, first() {{ return this; }}, attr() {{ return 'jQGridDemo'; }},
           jqGrid(cmd, arg) {{
             if (cmd === 'getGridParam') return arg === 'data' ? DATA : DATA.length;
             if (cmd === 'getDataIDs') return DATA.map(r => r._id_);
             if (cmd === 'expandSubGridRow') {{
               window.expanded.push(arg);
               const tr = document.getElementById(arg);
               const sub = document.createElement('tr');
               sub.className = 'ui-subgrid';
               sub.innerHTML = '<td>Part Condition Factory New</td>';
               tr.after(sub);
               return; }}
             return []; }} }};
}};
window.jQuery.fn = {{ jqGrid: true }};
</script></body></html>"""


@pytest.mark.asyncio
async def test_in_stock_rows_without_details_are_opened_before_reading(page_for):
    page = await page_for(MockProcart())
    rows = [{"_id_": "1", "Cust_part": "35010-38", "QtyAvailable": "7"},
            {"_id_": "2", "Cust_part": "410946", "QtyAvailable": "Out of Stock"}]
    await page.set_content(_grid_page_with_subgrids(rows))
    text = await ProponentAdapter(extractor=FakeExtractor())._grid_text(page, "single")
    assert await page.evaluate("window.expanded") == ["1"]      # in stock only
    assert "Expanded details (as shown on the page):\n    Part Condition Factory New" in text


def test_a_look_alike_number_never_takes_another_records_prices():
    from sourcing.models import Offer
    from sourcing.vendors.proponent import _use_price_breaks, _use_single_unit_price, price_at_qty

    # two records, numbers differ only by a dash (seen 2026-10-19)
    page = ("Record 1:\n  Cust_part: ACHC-3-13\n  Price: 369.16\n  Price_breaks: Yes\n"
            "  Expanded details (as shown on the page):\n"
            "    Order Quantity\n    Price\n    1-1\n    $406.07\n    2-4\n    $386.73\n"
            "    5+\n    $369.16\n\n"
            "Record 2:\n  Cust_part: ACHC3-13\n  Price: 8004.22\n  Price_breaks: No\n")
    offer = Offer(part_number="ACHC3-13", price="8004.22", qty_available="10")
    _use_single_unit_price(offer, page)
    _use_price_breaks(offer, page)
    assert offer.price == "8004.22" and offer.price_breaks == []
    assert price_at_qty(page, "ACHC3-13", "8004.22", "25") == "8004.22"
    offer = Offer(part_number="ACHC-3-13", price="369.16", qty_available="349")
    _use_single_unit_price(offer, page)
    _use_price_breaks(offer, page)
    assert offer.price == "406.07" and len(offer.price_breaks) == 3
    # written differently on the page than requested, one record only: still found
    offer = Offer(part_number="ACHC313", price="369.16", qty_available="349")
    _use_single_unit_price(offer, page.split("\n\nRecord 2")[0])
    assert offer.price == "406.07"


def test_moq_comes_from_the_parts_own_record():
    from sourcing.models import Offer
    from sourcing.vendors.proponent import _use_moq

    page = ("Record 1:\n  Cust_part: AN901-4C\n  Default_qty: 500\n"
            "  other_fields: Minimum order quantity for this part is 1,000 EA Maximum qty\n\n"
            "Record 2:\n  Cust_part: AN901-4\n  Default_qty: 500\n")
    offer = Offer(part_number="AN901-4C", moq="50")
    _use_moq(offer, page)
    assert offer.moq == "1000"
    offer = Offer(part_number="AN901-4")   # no sentence; Default_qty is not a minimum
    _use_moq(offer, page)
    assert offer.moq is None


def test_closest_match_suggestions_the_portal_wont_sell_are_left_out():
    from sourcing.vendors.proponent import _drop_suggestions, _own_record

    # Run 2026-10-27: 426EN10-8's real line, then the portal's look-alike row with
    # the same Cust_part, no price and no add-to-cart; ACHC3-13 for ACHC-3-13 is a
    # closest match that can be bought (2026-10-19), so it stays.
    text = ("Results read from the page's results grid (#jQGridDemo): 3 record(s), one per "
            "result line. Field names are the site's own.\n\n"
            "Record 1:\n  Cust_part: 426EN10-8\n  Price: 3014.37\n  QtyAvailable: 10\n"
            "  lAddToCart: True\n\n"
            "Record 2:\n  Cust_part: 426EN10-8\n  Item_number: 426EN9-8\n"
            "  Item_description1: You entered 426EN10-8, closest match based on characters is "
            "426EN9-8.\n  QtyAvailable: 10\n\n"
            "Record 3:\n  Cust_part: ACHC3-13\n  Price: 8004.22\n"
            "  Item_description1: RECEPTACLE You entered ACHC-3-13, closest match based on "
            "characters is ACHC3-13.\n  lAddToCart: True")
    out = _drop_suggestions(text)
    assert "Record 2:" not in out and "Record 1:" in out and "Record 3:" in out
    assert out.startswith("Results read from the page's results grid")
    assert "3014.37" in _own_record(out, "426EN10-8")
    assert _drop_suggestions(out) == out          # nothing more to drop


def test_description_and_uom_come_from_the_parts_own_record():
    from sourcing.models import Offer
    from sourcing.vendors.proponent import _use_description_and_uom

    page = ("Record 1:\n  Cust_part: 2873-300\n"
            "  Item_description1: SEAL This is an approved substitute part for part #AS1895/7-300\n"
            "  Um: EA\n\n"
            "Record 2:\n  Cust_part: ACHC3-13\n"
            "  Item_description1: RECEPTACLE You entered ACHC-3-13, closest match based on "
            "characters is ACHC3-13.\n  Um: FT\n")
    offer = Offer(part_number="2873-300")
    _use_description_and_uom(offer, page)
    assert (offer.description, offer.uom) == ("SEAL", "EA")
    offer = Offer(part_number="ACHC3-13")
    _use_description_and_uom(offer, page)
    assert (offer.description, offer.uom) == ("RECEPTACLE", "FT")
    offer = Offer(part_number="NOT-THERE")
    _use_description_and_uom(offer, page)
    assert (offer.description, offer.uom) == (None, None)



# -- Results page layout seen 2026-10-27 (no jqGrid) --------------------------------
# Built from debug_pages/20261027-202926-bulk.html: a result row (toggle, part,
# price, ..., qty, UOM) then its detail row (warehouse table, product details);
# "Show N Approved Substitutes" adds closed substitute rows ("+"); a warehouse
# dropdown shows one warehouse's numbers at a time; a closest-match row the
# portal won't sell shows the number asked for, no price and no Add To Cart.
def _result(pn, price, desc, qty, details, toggle="minus", cls=""):
    return (f'<tr class="{cls}"><td><div><button type="button"><img src="/images/icon-{toggle}-new.svg">'
            f'</button></div></td><td class="customer-part"><span>{pn}</span></td><td>{price}</td>'
            f'<td><span>No</span></td><td>{desc}</td><td>{qty}</td><td>EA</td><td><p></p></td>'
            f'<td></td></tr><tr class="{cls}"><td colspan="9">{details}</td></tr>')


def _warehouse_table(first_cell, date, due, on_hand, cart=True):
    return ('<table><thead><tr><th>Warehouse Name</th><th>Date Next In</th><th>Qty Due In</th>'
            f'<th>Qty On Hand</th><th>Qty Needed</th><th>Order</th></tr></thead><tbody><tr>'
            f'<td>{first_cell}</td><td>{date}</td><td>{due}</td><td>{on_hand}</td>'
            '<td><input type="text" value="1"></td><td>'
            + ('<button type="button">Add To Cart</button>' if cart else '') + '</td></tr></tbody></table>')


NEW_LAYOUT_HTML = (
    f"<html><body>{REAL_HEADER_IN}<table><tbody>"
    + _result("426EN9-8", "$3,650.89", "SWITCH", "Out of Stock",
              _warehouse_table("Warehouse: Brea, CA", "", "", "0")
              + "<div>Part Condition Factory New</div>")
    + '<tr><td colspan="9"><button type="button" id="show" onclick="showSubs()">'
      'Show 1 Approved Substitutes for 426EN9-8</button></td></tr>'
    + _result("4066-0014", "$2,774.16", "FLOAT,ASSY", "22",
              _warehouse_table('<select id="wh"><option value="MAIN">Warehouse: Brea, CA</option>'
                               '<option value="SIN">Warehouse: Singapore</option></select>',
                               "Nov 26, 2026", "30", "17")
              + '<div id="whname">Details for Warehouse: Brea, CA</div>'
              + "<div>Part Condition Factory New</div>")
    + _result("426EN10-8", "$3,014.37", "SWITCH", "10",
              _warehouse_table("Warehouse: Brea, CA", "", "", "10")
              + "<div>Part Condition Factory New</div>")
    + _result("426EN10-8", "", "<span>You entered 426EN10-8, closest match based on characters "
              "is 426EN9-8.</span>", "10",
              _warehouse_table("Warehouse: Brea, CA", "Jan 4, 2027<br>Jan 18, 2027", "3<br>5", "9",
                               cart=False))
    + _result("ACHC3-13", "$8,004.22", "RECEPTACLE<br>You entered ACHC-3-13, closest match based "
              "on characters is ACHC3-13.", "10",
              _warehouse_table("Warehouse: Netherlands", "", "", "10"))
    + """</tbody></table>
<script>
// Brea, CA / Singapore numbers, shown a moment after the pick, like a page
// that loads them.
const WH = {MAIN: ['Nov 26, 2026', '30', '17', 'Brea, CA'], SIN: ['', '', '5', 'Singapore']};
window.picks = 0;
document.getElementById('wh').addEventListener('change', e => {
  window.picks++;
  const v = WH[e.target.value];
  setTimeout(() => {
    const cells = e.target.closest('tr').children;
    cells[1].innerText = v[0]; cells[2].innerText = v[1]; cells[3].innerText = v[2];
    document.getElementById('whname').innerText = 'Details for Warehouse: ' + v[3];
  }, 200);
});
function showSubs() {
  const btn = document.getElementById('show');
  btn.innerText = 'Hide 1 Approved Substitutes for 426EN9-8';
  const t = document.createElement('tbody');
  t.innerHTML = `<tr class="substitute-row"><td><div><button type="button" id="plus"
    onclick="this.querySelector('img').src='/images/icon-minus-new.svg';
             document.getElementById('subdetails').innerHTML += '<div>Part Condition Factory New</div>'">
    <img src="/images/icon-plus-new.svg"></button></div></td><td class="customer-part"><span>426HE13-8</span></td>
    <td>$2,524.60</td><td>No</td><td>SWITCH<br>This is an approved substitute part for part #426EN9-8</td>
    <td>37</td><td>EA</td><td></td><td></td></tr>
    <tr class="substitute-row"><td colspan="9" id="subdetails"><ul><li>Minimum order quantity for this part is 3 EA</li></ul>
    <table><tbody><tr><td>Warehouse: Brea, CA</td><td>Nov 14, 2026</td><td>5</td><td>37</td><td></td>
    <td><button type="button">Add To Cart</button></td></tr></tbody></table></td></tr>`;
  btn.closest('tr').before(...t.children);
}
</script></body></html>""")


@pytest.mark.asyncio
async def test_new_layout_substitutes_suggestions_and_every_warehouse(page_for):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(NEW_LAYOUT_HTML)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")

    # the substitute's own details opened: its condition is on the page
    sub = text[text.index("426HE13-8"):text.index("Hide 1 Approved")]
    assert "Part Condition Factory New" in sub
    # the closest-match row the portal won't sell is gone, with its 9 on hand;
    # the look-alike that can be bought keeps its own number
    assert "426EN9-8." not in text and "Jan 18, 2027" not in text
    assert text.count("426EN10-8") == 1 and "ACHC3-13\t$8,004.22" in text
    # both warehouses of 4066-0014, Brea, CA selected again
    block = text[text.index("4066-0014"):text.index("Details for Warehouse")]
    assert "Warehouse: Brea, CA\tNov 26, 2026\t30\t17" in block
    assert "Warehouse: Singapore\t\t\t5" in block
    assert await page.evaluate("document.getElementById('wh').value") == "MAIN"

    # a second pass changes nothing
    picks = await page.evaluate("window.picks")
    await adapter._expand_all_details(page)
    assert await page.evaluate("window.picks") == picks
    assert (await proponent._page_text(page, "bulk")).count("Warehouse: Singapore") == 1


# Page text as read 2026-10-27 (debug_pages/20261027-202926-bulk.txt), cut down.
NEW_LAYOUT_TEXT = (
    "\t426EN9-8\t$3,650.89\tNo\tSWITCH\tOut of Stock\tEA\t\n\n"
    "\t426HE13-8\t$2,524.60\tNo\tSWITCH\n"
    "This is an approved substitute part for part #426EN9-8\t37\tEA\t\n\n"
    "Hide 1 Approved Substitutes for 426EN9-8\n"
    "\tACHC3-13\t$8,004.22\tNo\tRECEPTACLE\n"
    "You entered ACHC-3-13, closest match based on characters is ACHC3-13.\t10\tEA\t\n\n"
    "--- Page details not shown as text ---\n"
    "BA3656-200 SEAL This is an approved substitute part for part #620-1-200 Out of S | Part Number: Copy\n")


def test_only_parts_the_page_calls_substitutes_stay_substitutes():
    from sourcing.models import Offer
    from sourcing.vendors.proponent import _keep_listed_substitutes, _listed_substitutes

    assert _listed_substitutes(NEW_LAYOUT_TEXT) == {("426HE13-8", "426EN9-8"),
                                                    ("BA3656-200", "620-1-200")}
    offers = [Offer(part_number="426EN9-8", listing="requested"),
              Offer(part_number="426HE13-8", listing="substitute"),
              Offer(part_number="426EN9-8", listing="substitute")]     # a look-alike, not listed
    _keep_listed_substitutes(offers, NEW_LAYOUT_TEXT, "426EN10-8")
    assert [o.listing for o in offers] == ["requested", "other", "other"]
    offers = [Offer(part_number="426HE13-8", listing="substitute"),
              Offer(part_number="ACHC3-13", listing="substitute")]
    _keep_listed_substitutes(offers, NEW_LAYOUT_TEXT, "426EN9-8")
    assert [o.listing for o in offers] == ["substitute", "other"]
    # grid records: the note's own record
    grid = ("Record 1:\n  Cust_part: 2873-300\n"
            "  Item_description1: SEAL This is an approved substitute part for part #AS1895/7-300\n")
    assert _listed_substitutes(grid) == {("2873-300", "AS1895/7-300")}


def test_substitute_part_number_with_a_space_is_listed():
    # 2026-11-04: "2758 CHARGED" (approved substitute for 410946) was cut to "2758"
    # and left out as "not listed on the page".
    from sourcing.vendors.proponent import _listed_substitutes
    text = ("\t2758 CHARGED\t$8,693.81\tNo\tBATTERY\nCHARGED, SAFT\n"
            "This is an approved substitute part for part #410946\t125\tEA\t\n")
    assert ("2758 CHARGED", "410946") in _listed_substitutes(text)


# The warehouse dropdown of the UI seen 2026-11-04 (debug_pages/20261104-175506-bulk.html):
# the <select> is hidden inside a custom control; only clicks on its opener and
# option change the warehouse (setting the select does nothing).
CUSTOM_WAREHOUSE = (
    '<div class="bb-custom-select-container bb-customSelect">'
    '<span class="bb-custom-select-opener" role="combobox" tabindex="0" onclick="'
    "this.parentNode.querySelector('.bb-custom-select-panel').style.display='block'\">"
    '<span id="whlabel">Warehouse: Brea, CA</span></span>'
    '<select tabindex="-1" style="display: none;"><option value="MAIN">Warehouse: Brea, CA</option>'
    '<option value="SIN">Warehouse: Singapore</option></select>'
    '<div class="bb-custom-select-panel" role="listbox" style="display:none">'
    '<div class="bb-custom-select-option" data-value="MAIN" role="option">Warehouse: Brea, CA</div>'
    '<div class="bb-custom-select-option" data-value="SIN" role="option">Warehouse: Singapore</div>'
    '</div></div>')
CUSTOM_LAYOUT_HTML = (
    f"<html><body>{REAL_HEADER_IN}<table><tbody>"
    + _result("4066-0014", "$2,774.16", "FLOAT,ASSY", "22",
              _warehouse_table(CUSTOM_WAREHOUSE, "Nov 26, 2026", "30", "17")
              + '<div id="whname">Details for Warehouse: Brea, CA</div>'
              + "<div>Part Condition Factory New</div>")
    + """</tbody></table>
<script>
const WH = {MAIN: ['Nov 26, 2026', '30', '17', 'Brea, CA'], SIN: ['', '', '5', 'Singapore']};
window.picks = 0;
for (const opt of document.querySelectorAll('[role=option]')) {
  opt.addEventListener('click', () => {
    window.picks++;
    const box = opt.closest('.bb-custom-select-container');
    box.querySelector('.bb-custom-select-panel').style.display = 'none';
    box.querySelector('select').value = opt.dataset.value;
    document.getElementById('whlabel').innerText = opt.innerText;
    const v = WH[opt.dataset.value];
    setTimeout(() => {
      const cells = box.closest('tr').children;
      cells[1].innerText = v[0]; cells[2].innerText = v[1]; cells[3].innerText = v[2];
      document.getElementById('whname').innerText = 'Details for Warehouse: ' + v[3];
    }, 200);
  });
}
</script></body></html>""")


@pytest.mark.asyncio
async def test_custom_warehouse_dropdown_is_picked_by_clicks(page_for):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(CUSTOM_LAYOUT_HTML)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    block = text[text.index("4066-0014"):text.index("Details for Warehouse")]
    assert "Nov 26, 2026\t30\t17" in block                      # Brea, CA, selected again
    assert "Warehouse: Singapore\t\t\t5" in block               # the other warehouse
    assert await page.evaluate("document.querySelector('select').value") == "MAIN"
    assert await page.evaluate("window.picks") == 2             # Singapore, then Brea, CA


# Run 2026-11-06 15:06: a pick redraws the dropdown, a new <select> without our
# mark (the second pass found Singapore selected; every read of "its row" had come
# back empty, all picks counted as failed). Two parts, so the second dropdown is
# found by its place after the first one was redrawn.
def _redrawn_dropdown(n):
    return f'<div id="wh{n}" class="whbox"></div>'


REDRAWN_LAYOUT_HTML = (
    f"<html><body>{REAL_HEADER_IN}<table><tbody>"
    + _result("4066-0014", "$2,774.16", "FLOAT,ASSY", "22",
              _warehouse_table(_redrawn_dropdown(0), "Nov 26, 2026", "30", "17")
              + '<div id="whname0">Details for Warehouse: Brea, CA</div>')
    + _result("40P6050", "$391.97", "VALVE", "148",
              _warehouse_table(_redrawn_dropdown(1), "", "", "113")
              + '<div id="whname1">Details for Warehouse: Netherlands</div>')
    + """</tbody></table>
<script>
const WH = [{MAIN: ['Nov 26, 2026', '30', '17', 'Brea, CA'], SIN: ['', '', '5', 'Singapore']},
            {AMS: ['', '', '113', 'Netherlands'], SIN: ['Dec 7, 2026', '10', '35', 'Singapore']}];
window.picks = 0;
function draw(n, value) {
  const opts = Object.keys(WH[n]);
  const box = document.getElementById('wh' + n);
  box.innerHTML = '<div class="bb-custom-select-container bb-customSelect">'
    + '<span class="bb-custom-select-opener" role="combobox" tabindex="0"><span>Warehouse: '
    + WH[n][value][3] + '</span></span><select tabindex="-1" style="display: none;">'
    + opts.map(v => `<option value="${v}"${v === value ? ' selected' : ''}>Warehouse: ${WH[n][v][3]}</option>`).join('')
    + '</select><div class="bb-custom-select-panel" role="listbox" style="display:none">'
    + opts.map(v => `<div class="bb-custom-select-option" data-value="${v}" role="option">Warehouse: ${WH[n][v][3]}</div>`).join('')
    + '</div></div>';
}
draw(0, 'MAIN'); draw(1, 'AMS');
document.addEventListener('click', e => {
  const opener = e.target.closest('.bb-custom-select-opener');
  if (opener) { opener.parentNode.querySelector('.bb-custom-select-panel').style.display = 'block'; return; }
  const opt = e.target.closest('[role=option]');
  if (!opt) return;
  window.picks++;
  const box = opt.closest('.whbox');
  const n = Number(box.id.slice(2));
  const v = WH[n][opt.dataset.value];
  setTimeout(() => {
    draw(n, opt.dataset.value);                     // a new <select>: our mark is gone
    const cells = box.closest('tr').children;
    cells[1].innerText = v[0]; cells[2].innerText = v[1]; cells[3].innerText = v[2];
    document.getElementById('whname' + n).innerText = 'Details for Warehouse: ' + v[3];
  }, 200);
});
</script></body></html>""")


@pytest.mark.asyncio
async def test_warehouse_dropdown_redrawn_by_each_pick_is_found_again(page_for):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(REDRAWN_LAYOUT_HTML)
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    first = text[text.index("4066-0014"):text.index("Details for Warehouse: Brea")]
    assert "Nov 26, 2026\t30\t17" in first and "Warehouse: Singapore\t\t\t5" in first
    second = text[text.index("40P6050"):text.index("Details for Warehouse: Netherlands")]
    assert "Warehouse: Singapore\tDec 7, 2026\t10\t35" in second and "\t113" in second
    values = await page.evaluate("Array.from(document.querySelectorAll('select')).map(s => s.value)")
    assert values == ["MAIN", "AMS"]                            # first warehouses again
    assert await page.evaluate("window.picks") == 4
    assert not adapter._warehouse_gaps and not adapter.assist.events

    # a second pass changes nothing
    await adapter._expand_all_details(page)
    assert await page.evaluate("window.picks") == 4
    assert (await proponent._page_text(page, "bulk")).count("Warehouse: Singapore") == 2


# The page fills each result's warehouse table from /Handlers/PartSearchHandler.ashx
# (its own script, debug_pages/20261104-175506-bulk.html). Shape assumed from that
# script (DisplayName, Item_site, WhsQty, Due_date, TotQty); not yet seen live.
def _handler_json(onhand_brea="17.0000"):
    return {
        "4066-0014": {"d": {"rows": [
            {"DisplayName": "Warehouse: Brea, CA", "Item_site": "MAIN", "WhsQty": onhand_brea,
             "Due_date": ["11/26/2026"], "TotQty": ["30"]},
            {"DisplayName": "Warehouse: Singapore", "Item_site": "SIN", "WhsQty": "5.0000",
             "Due_date": [""], "TotQty": [""]}]}},
        "40P6050": {"d": {"rows": [
            {"DisplayName": "Warehouse: Netherlands", "Item_site": "AMS", "WhsQty": "113",
             "Due_date": [], "TotQty": []},
            {"DisplayName": "Warehouse: Singapore", "Item_site": "SIN", "WhsQty": "35",
             "Due_date": ["12/7/2026"], "TotQty": ["10"]}]}},
    }


async def _served_results_page(page_for, data):
    import json as _json
    from urllib.parse import parse_qs as _pq, urlparse as _up
    page = await page_for(MockProcart())
    asked = []

    async def handler(route):
        part = _pq(_up(route.request.url).query).get("PartNo", [""])[0]
        asked.append(part)
        await route.fulfill(status=200, content_type="application/json; charset=utf-8",
                            body=_json.dumps(data.get(part, {"d": {"rows": []}})))

    async def results(route):
        await route.fulfill(status=200, content_type="text/html", body=REDRAWN_LAYOUT_HTML)
    await page.route("**/Handlers/PartSearchHandler.ashx*", handler)
    await page.route("**/PartSearchResults.aspx", results)
    await page.goto("https://procart.proponent.com/PartSearchResults.aspx")
    return page, asked


@pytest.mark.asyncio
async def test_every_warehouse_read_from_the_pages_data_without_the_dropdown(page_for):
    from sourcing.vendors import proponent

    page, asked = await _served_results_page(page_for, _handler_json())
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    first = text[text.index("4066-0014"):text.index("Details for Warehouse: Brea")]
    assert "Nov 26, 2026\t30\t17" in first and "Warehouse: Singapore\t\t\t5" in first
    second = text[text.index("40P6050"):text.index("Details for Warehouse: Netherlands")]
    assert "Warehouse: Singapore\tDec 7, 2026\t10\t35" in second
    assert await page.evaluate("window.picks") == 0             # dropdowns left alone
    assert sorted(asked) == ["4066-0014", "40P6050"]
    assert not adapter._warehouse_gaps


def test_warehouse_reply_read_as_named_fields_or_as_cell_lists():
    # The page's formatter takes either form (rowObject.Due_date or rowObject[6]).
    import json as _json
    from sourcing.vendors.proponent import _warehouse_records, _warehouse_values, _wh_name
    cells = {"page": 1, "rows": [
        {"id": "1", "cell": ["", "", "<span>Warehouse: Brea, CA</span>", "", "", "",
                             "11/26/2026<br />1/5/2027", "30<br />12", "17.0000", "1", "", ""]},
        {"id": "2", "cell": ["", "", "Warehouse: Singapore", "", "", "", "", "", "5", "1", "", ""]}]}
    recs = _warehouse_records(_json.dumps(cells))
    assert [_wh_name(r) for r in recs] == ["Warehouse: Brea, CA", "Warehouse: Singapore"]
    assert _warehouse_values(recs[0]) == ["Nov 26, 2026\nJan 5, 2027", "30\n12", "17"]
    assert _warehouse_values(recs[1]) == ["", "", "5"]
    named = {"d": _json.dumps({"rows": [{"DisplayName": "Warehouse: Singapore", "Item_site": "SIN",
                                        "WhsQty": "1,234.00", "Due_date": ["", "12/7/2026"],
                                        "TotQty": ["", "10"]}]})}
    recs = _warehouse_records(_json.dumps(named))
    assert _warehouse_values(recs[0]) == ["Dec 7, 2026", "10", "1234"]
    assert _warehouse_records("error") == [] and _warehouse_records('{"rows": []}') == []


@pytest.mark.asyncio
async def test_warehouse_data_not_matching_the_page_falls_back_to_the_dropdown(page_for):
    from sourcing.vendors import proponent

    page, _ = await _served_results_page(page_for, _handler_json(onhand_brea="99"))
    adapter = ProponentAdapter(extractor=FakeExtractor())
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    first = text[text.index("4066-0014"):text.index("Details for Warehouse: Brea")]
    assert "Warehouse: Singapore\t\t\t5" in first           # read by the dropdown
    assert "99" not in first
    assert await page.evaluate("window.picks") == 2             # Singapore, then Brea, CA


# The UI changes again (class names the code doesn't know; roles as before): the
# coded pick finds nothing, the LLM picks opener then warehouse, the row's
# numbers prove it, and the recipe saved does it next run without the LLM.
RENAMED_LAYOUT_HTML = (CUSTOM_LAYOUT_HTML
                       .replace("bb-custom-select-container", "zz-dropdown")
                       .replace("bb-custom-select-opener", "zz-opener")
                       .replace("bb-custom-select-panel", "zz-panel")
                       .replace("bb-custom-select-option", "zz-item")
                       .replace(".bb-custom-select-panel", ".zz-panel")
                       .replace("'.bb-custom-select-container'", "'.zz-dropdown'"))


class PickingExtractor(FakeExtractor):
    """Stands in for the LLM's pick_element: the opener, then the option named in
    the goal. Records each call."""

    def __init__(self):
        super().__init__()
        self.picks: list[str] = []

    async def pick_element(self, elements, what):
        self.picks.append(what)
        label = what.split("choose '")[-1].split("'")[0]
        option = next((e for e in elements if e.get("role") == "option"
                       and e.get("text") == label), None)
        opener = next((e for e in elements if e.get("role") == "combobox"), None)
        el = option or opener
        return (el["index"] if el else None), LlmUsage(calls=1)


@pytest.mark.asyncio
async def test_changed_dropdown_is_done_by_the_llm_checked_and_learned(page_for):
    from sourcing.vendors import proponent

    page = await page_for(MockProcart())
    await page.set_content(RENAMED_LAYOUT_HTML)
    llm = PickingExtractor()
    adapter = ProponentAdapter(extractor=llm)
    await adapter._expand_all_details(page)
    text = await proponent._page_text(page, "bulk")
    block = text[text.index("4066-0014"):text.index("Details for Warehouse")]
    assert "Warehouse: Singapore\t\t\t5" in block and "Nov 26, 2026\t30\t17" in block
    assert await page.evaluate("document.querySelector('select').value") == "MAIN"
    assert llm.picks                                         # the LLM was asked
    assert adapter.usage.calls == len(llm.picks)             # and its cost counted
    assert "assist_proponent_warehouse_pick" in adapter.confirmed_learned()
    assert any("done with the LLM" in e for e in adapter.assist.events)

    # Next run: the saved recipe, no LLM.
    page2 = await page_for(MockProcart())
    await page2.set_content(RENAMED_LAYOUT_HTML)
    llm2 = PickingExtractor()
    adapter2 = ProponentAdapter(extractor=llm2, selectors=adapter.confirmed_learned())
    await adapter2._expand_all_details(page2)
    text2 = await proponent._page_text(page2, "bulk")
    assert "Warehouse: Singapore\t\t\t5" in text2 and llm2.picks == []
    assert any("saved recipe" in e for e in adapter2.assist.events)


@pytest.mark.asyncio
async def test_warehouse_not_read_is_flagged_on_its_line(page_for):
    from sourcing.models import Offer

    page = await page_for(MockProcart())
    await page.set_content(RENAMED_LAYOUT_HTML)
    adapter = ProponentAdapter(extractor=FakeExtractor())      # no pick_element: no LLM
    await adapter._expand_all_details(page)
    offer = Offer(part_number="4066-0014", price="2774.16", qty_available="22")
    adapter._mark_gaps([offer])
    assert "WAREHOUSES_INCOMPLETE" in offer.review
    assert offer.vendor_note == "OTHER WAREHOUSES NOT READ: SINGAPORE"
    assert any("not reached" in e for e in adapter.assist.events)


def test_risky_elements_are_never_offered_to_the_llm():
    from sourcing.vendors.assist import _risky
    for text in ("Add To Cart", "UPDATE", "UPDATE SLR", "Log out", "Checkout", "Delete"):
        assert _risky({"text": text})
    for text in ("Warehouse: Singapore", "Bulk Discounts", "Show 2 Approved Substitutes"):
        assert not _risky({"text": text})
