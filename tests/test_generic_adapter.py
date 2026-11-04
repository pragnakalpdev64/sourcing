"""GenericAdapter login against mock portals (docs/GENERIC_VENDOR_PLAN.md phase 3).

The mock pages are invented on purpose: field ids the generic lists don't know,
2-step sign-in, a login link on a home page, TOTP, SMS, wrong password, CAPTCHA.
A fake LLM stands in for the model and records every call, so each test also
shows when the LLM was (or wasn't) asked. Skipped if no local Chromium."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage
from sourcing.vendors.base import (
    CaptchaFailed, InvalidCredentials, LoginBlocked, LoginFailed, MfaRequired,
)
from sourcing.vendors.generic import MAX_DISCOVERY_CALLS, GenericAdapter

HOST = "https://shop.example.com"
SEED = pyotp.random_base32()

STD_FIELDS = """<input id="username" name="u" type="text">
<input id="password" name="p" type="password">
<button type="submit">Sign in</button>"""
LOGIN_PAGE = """<html><body><h1>Customer portal</h1>{error}
<form method="post" action="/account/login">{fields}</form>
<a href="/account/forgot">Forgot password?</a></body></html>"""
STEP1_PAGE = """<html><body><h1>Sign in</h1>
<form method="post" action="/account/login"><input id="email" name="u" type="text" autocomplete="username">
<button type="submit">Next</button></form></body></html>"""
STEP2_PAGE = """<html><body><h1>Enter your password</h1>
<form method="post" action="/account/password"><input id="pw" name="p" type="password">
<button type="submit">Sign in</button></form></body></html>"""
HOME_PUBLIC = """<html><body><h1>Parts for aircraft</h1>
<form action="/newsletter" method="post"><input type="email" name="news" placeholder="Your email">
<button type="submit">Subscribe</button></form>
<a href="{login_href}">{login_text}</a></body></html>"""
DASHBOARD = """<html><body><h1>Welcome back</h1><a href="/catalog">Catalog</a>
{logout}</body></html>"""
MFA_PAGE = """<html><body><p>{prompt}</p><form method="post" action="/account/mfa">
<input name="otpCode" autocomplete="one-time-code"><button type="submit">Verify</button>
</form></body></html>"""
# Logout only inside a menu that a profile button opens (DASI, 2026-10-29).
MENU = """<button id="{id}" {attrs} onclick="document.getElementById('menu').style.display='block'">
JD</button><div id="menu" style="display:none"><a href="/account/settings">Settings</a>
<a href="/account/logout">Log out</a></div>"""
CAPTCHA_PAGE = "<html><body><p>Please complete the CAPTCHA to continue.</p></body></html>"


class MockPortal:
    def __init__(self, *, fields=STD_FIELDS, two_step=False, mfa=None, captcha=False,
                 logout_link=True, login_href="/account/login", login_text="Sign in",
                 menu_attrs=None):
        self.fields, self.two_step, self.mfa, self.captcha = fields, two_step, mfa, captcha
        self.logout_link, self.login_href, self.login_text = logout_link, login_href, login_text
        self.menu_attrs = menu_attrs              # logout inside a menu button with these attrs
        self.signed_in = False
        self.pending_mfa = False
        self.user_ok = False
        self.posts: list[tuple[str, dict]] = []

    def _after_password(self, ok: bool) -> str:
        if not ok:
            return LOGIN_PAGE.format(error="<p>Invalid username or password.</p>", fields=self.fields)
        if self.captcha:
            return CAPTCHA_PAGE
        if self.mfa:
            self.pending_mfa = True
            prompt = ("Enter the code from your authenticator app" if self.mfa == "totp"
                      else "We sent a code by text message (SMS)")
            return MFA_PAGE.format(prompt=prompt)
        self.signed_in = True
        return self._dashboard()

    def _dashboard(self) -> str:
        if self.menu_attrs is not None:
            return DASHBOARD.format(logout=MENU.format(id="me", attrs=self.menu_attrs))
        return DASHBOARD.format(logout='<a href="/account/logout">Log out</a>'
                                if self.logout_link else "")

    async def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        path = url.path.lower()
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        if req.method == "POST":
            self.posts.append((path, form))
        if url.hostname != "shop.example.com":
            html = LOGIN_PAGE.format(error="", fields=STD_FIELDS)   # some other site
        elif path in ("/account/login", "/entry") and req.method == "POST":
            if self.two_step:
                self.user_ok = form.get("u") == "jane"
                html = STEP2_PAGE
            else:
                html = self._after_password(form.get("u") == "jane" and form.get("p") == "pw")
        elif path == "/account/password" and req.method == "POST":
            html = self._after_password(self.user_ok and form.get("p") == "pw")
        elif path == "/account/mfa" and req.method == "POST" and self.pending_mfa:
            if self.mfa == "totp" and pyotp.TOTP(SEED).verify(form.get("otpCode", ""), valid_window=1):
                self.pending_mfa, self.signed_in = False, True
                html = self._dashboard()
            else:
                html = MFA_PAGE.format(prompt="Wrong code, enter the code again")
        elif path in ("/account/login", "/entry"):
            html = STEP1_PAGE if self.two_step else LOGIN_PAGE.format(error="", fields=self.fields)
        elif path == "/account/logout":
            self.signed_in = False
            html = HOME_PUBLIC.format(login_href=self.login_href, login_text=self.login_text)
        elif path == "/newsletter":
            html = "<html><body>Thanks for subscribing</body></html>"
        elif self.signed_in:
            html = self._dashboard()
        else:
            html = HOME_PUBLIC.format(login_href=self.login_href, login_text=self.login_text)
        await route.fulfill(status=200, content_type="text/html", body=html)


class FakeLlm:
    """Stands in for Extractor: answers from small functions and records calls."""

    def __init__(self, fields=None, element=None, state="unknown", not_sold=None, steps=()):
        self.fields, self.element, self.state = fields, element, state
        self.steps = list(steps)                 # next_search_step answers, in order
        self.not_sold = not_sold                 # quote to answer not_sold_quote with
        self.calls: list[str] = []

    async def pick_login_fields(self, elements):
        self.calls.append("fields")
        picked = self.fields(elements) if self.fields else {}
        return {k: picked.get(k) for k in ("username", "password", "submit", "next")}, LlmUsage(calls=1)

    async def pick_element(self, elements, what):
        self.calls.append("element")
        return (self.element(elements) if self.element else None), LlmUsage(calls=1)

    async def not_sold_quote(self, page_text, part_number):
        self.calls.append("not_sold")
        return self.not_sold, LlmUsage(calls=1)

    async def next_search_step(self, elements, page_text, part_number, tried):
        self.calls.append("step")
        if not self.steps:
            return {"action": "give_up", "index": None, "reason": "test"}, LlmUsage(calls=1)
        action, pick = self.steps.pop(0)
        return ({"action": action, "index": pick(elements) if pick else None, "reason": "test"},
                LlmUsage(calls=1))

    async def classify_login_page(self, page_text, page_url):
        self.calls.append("classify")
        return self.state, LlmUsage(calls=1)


def index_of(**match):
    return lambda els: next(e["index"] for e in els
                            if all(e.get(k) == v for k, v in match.items()))


def adapter(llm=None, selectors=None, **config) -> GenericAdapter:
    cfg = {"display_name": "Example Parts", "allowed_hosts": ["shop.example.com"],
           "login_url": f"{HOST}/account/login", **config}
    return GenericAdapter(extractor=llm, selectors=selectors, vendor_config=cfg)


def cred(password="pw", totp=None):
    return VendorCredential("example", "", "sourcing-vendor-example", "jane", password, totp)


@pytest_asyncio.fixture
async def page_for():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")

    async def make(portal: MockPortal):
        page = await browser.new_page()
        await page.route("https://*.example.com/**", portal.handle)
        return page

    yield make
    await browser.close()
    await pw.stop()


@pytest.mark.asyncio
async def test_standard_form_logs_in_without_the_llm(page_for):
    portal, llm = MockPortal(), FakeLlm()
    page = await page_for(portal)
    a = adapter(llm)
    await a.login(page, cred())
    assert portal.signed_in and llm.calls == []
    assert a.learned["username_input"] == "input[id*='username' i]"
    assert a.learned["logout_link"] and a.learned["home_url"].startswith(HOST)
    assert a.signed_in_proof.startswith(a.learned["logout_link"])
    assert await a.is_authenticated(page)
    assert await a.logout(page) and not portal.signed_in
    assert not await a.is_authenticated(page)


@pytest.mark.asyncio
async def test_unknown_username_field_is_found_by_the_llm_and_saved(page_for):
    fields = """<input id="kdnr" name="u" type="text" placeholder="Customer number">
<input id="secret" name="p" type="password"><button type="submit">Go</button>"""
    portal = MockPortal(fields=fields)
    llm = FakeLlm(fields=lambda els: {"username": index_of(id="kdnr")(els)})
    page = await page_for(portal)
    first = adapter(llm)
    await first.login(page, cred())
    assert portal.signed_in and llm.calls == ["fields"]
    assert first.learned["username_input"] == '[id="kdnr"]'

    # Next run: the saved selector is used, the LLM isn't asked again.
    portal.signed_in = False
    llm2 = FakeLlm()
    second = adapter(llm2, selectors=first.learned)
    await second.login(await page_for(portal), cred())
    assert portal.signed_in and llm2.calls == []


@pytest.mark.asyncio
async def test_llm_answer_that_is_not_a_password_field_is_refused(page_for):
    # No type=password field: the LLM points at a text box, which must not be used.
    fields = """<input id="username" name="u" type="text"><input id="pw" name="p" type="text">
<button type="submit">Sign in</button>"""
    portal = MockPortal(fields=fields)
    llm = FakeLlm(fields=lambda els: {"password": index_of(id="pw")(els)})
    with pytest.raises(LoginFailed):
        await adapter(llm).login(await page_for(portal), cred())
    # A username-only submit is how a 2-step login starts; the password is never sent.
    assert all("p" not in form for _, form in portal.posts)


@pytest.mark.asyncio
async def test_two_step_username_then_password(page_for):
    portal, llm = MockPortal(two_step=True), FakeLlm()
    a = adapter(llm)
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and llm.calls == []
    assert [p for p, _ in portal.posts] == ["/account/login", "/account/password"]
    assert a.learned["next_button"]


@pytest.mark.asyncio
async def test_home_page_login_link_is_followed_and_newsletter_box_ignored(page_for):
    portal = MockPortal()
    a = adapter(FakeLlm(), login_url=None, website=f"{HOST}/")
    await a.login(await page_for(portal), cred())
    assert portal.signed_in
    assert all(path != "/newsletter" for path, _ in portal.posts)


@pytest.mark.asyncio
async def test_login_link_without_login_words_is_found_by_the_llm(page_for):
    portal = MockPortal(login_href="/entry", login_text="Customer portal")
    llm = FakeLlm(element=index_of(href="/entry"))
    a = adapter(llm, login_url=None, website=f"{HOST}/")
    await a.login(await page_for(portal), cred())
    assert portal.signed_in and llm.calls == ["element"]
    assert a.learned["login_page_url"] == f"{HOST}/entry"


@pytest.mark.asyncio
async def test_totp_from_the_secret(page_for):
    portal = MockPortal(mfa="totp")
    a = adapter(FakeLlm())
    await a.login(await page_for(portal), cred(totp=SEED))
    assert portal.signed_in and a.learned["mfa_code_input"]


@pytest.mark.asyncio
async def test_sms_code_needs_a_person(page_for):
    with pytest.raises(MfaRequired) as exc:
        await adapter(FakeLlm()).login(await page_for(MockPortal(mfa="sms")), cred())
    assert exc.value.method == "sms"


@pytest.mark.asyncio
async def test_wrong_password(page_for):
    with pytest.raises(InvalidCredentials):
        await adapter(FakeLlm()).login(await page_for(MockPortal()), cred(password="nope"))


@pytest.mark.asyncio
async def test_captcha_is_a_retryable_failure_and_the_llm_is_not_asked(page_for):
    # For now (2026-11-03) a CAPTCHA is tried again, not a stop.
    llm = FakeLlm(state="signed_in")              # would say "fine" if it were asked
    with pytest.raises(CaptchaFailed, match="CAPTCHA") as exc:
        await adapter(llm).login(await page_for(MockPortal(captcha=True)), cred())
    assert exc.value.retryable and not isinstance(exc.value, LoginBlocked)
    assert llm.calls == []


@pytest.mark.asyncio
async def test_unclear_page_after_sign_in_is_read_by_the_llm(page_for):
    portal = MockPortal(logout_link=False)        # dashboard with no logout link
    llm = FakeLlm(state="signed_in")
    a = adapter(llm)
    page = await page_for(portal)
    await a.login(page, cred())
    # Asked once for an account menu (there is none), then to read the page.
    assert llm.calls == ["element", "classify"] and a.learned["home_url"].startswith(HOST)
    assert a.signed_in_proof is None               # the LLM's word alone: still probed
    assert await a.is_authenticated(page)          # no logout link: the LLM reads the page
    assert llm.calls == ["element", "classify", "classify"]

    portal2 = MockPortal(logout_link=False)
    with pytest.raises(InvalidCredentials):
        await adapter(FakeLlm(state="bad_credentials")).login(await page_for(portal2), cred())


@pytest.mark.asyncio
async def test_logout_in_a_profile_menu_is_found_without_the_llm(page_for):
    portal, llm = MockPortal(menu_attrs='aria-label="User profile"'), FakeLlm(state="signed_in")
    page = await page_for(portal)
    a = adapter(llm)
    await a.login(page, cred())
    assert llm.calls == [] and a.found_by["logout_link"] == "account menu"
    assert a.learned["account_menu"] == "button[aria-label*='profile' i]"
    assert await a.is_authenticated(page) and llm.calls == []
    assert await a.logout(page) and not portal.signed_in


@pytest.mark.asyncio
async def test_unnamed_avatar_is_picked_by_the_llm_and_saved(page_for):
    portal = MockPortal(menu_attrs='class="x"')            # no words the list knows
    llm = FakeLlm(element=index_of(id="me"), state="unknown")
    page = await page_for(portal)
    a = adapter(llm)
    await a.login(page, cred())
    assert llm.calls == ["element"] and a.found_by["account_menu"] == "LLM"
    assert a.learned["account_menu"] == '[id="me"]'
    saved = dict(a.learned)

    # Next run: the saved menu is opened, the LLM isn't asked.
    llm2 = FakeLlm(state="unknown")
    b = adapter(llm2, selectors=saved)
    assert await b.is_authenticated(page) and llm2.calls == []
    assert await b.logout(page) and not portal.signed_in


@pytest.mark.asyncio
async def test_menu_that_would_change_something_is_never_clicked(page_for):
    # Matches the list (aria-label*='account') and is the LLM's pick: refused both times.
    portal = MockPortal(menu_attrs='aria-label="Delete account"')
    llm = FakeLlm(element=index_of(id="me"), state="signed_in")
    page = await page_for(portal)
    a = adapter(llm)
    await a.login(page, cred())
    assert llm.calls == ["element", "classify"] and "account_menu" not in a.learned
    assert not await page.locator("#menu").is_visible()


@pytest.mark.asyncio
async def test_llm_menu_pick_without_a_logout_link_is_not_saved(page_for):
    portal = MockPortal(logout_link=False)
    llm = FakeLlm(element=index_of(text="Catalog"), state="signed_in")
    a = adapter(llm)
    await a.login(await page_for(portal), cred())
    assert "account_menu" not in a.learned and llm.calls == ["element", "classify"]


@pytest.mark.asyncio
async def test_off_site_urls_are_never_used(page_for):
    with pytest.raises(LoginFailed, match="No usable login URL"):
        await adapter(FakeLlm(), login_url="https://evil.example.com/login").login(
            await page_for(MockPortal()), cred())

    portal = MockPortal(login_href="https://evil.example.com/login")
    with pytest.raises(LoginFailed):
        await adapter(FakeLlm(), login_url=None, website=f"{HOST}/").login(
            await page_for(portal), cred())
    assert portal.posts == []                     # nothing typed anywhere


@pytest.mark.asyncio
async def test_llm_calls_are_capped(page_for):
    page = await page_for(MockPortal())
    await page.route("https://evil.example.org/**", lambda route: route.fulfill(
        status=200, content_type="text/html",
        body="<form><input type=email name=u><input type=password name=p></form>"))
    await page.goto(f"{HOST}/")
    llm = FakeLlm()
    a = adapter(llm)
    for _ in range(MAX_DISCOVERY_CALLS + 3):
        await a._llm_pick_one(page, "anything", "x")
    assert len(llm.calls) == MAX_DISCOVERY_CALLS


@pytest.mark.asyncio
async def test_signed_in_needs_positive_proof(page_for):
    page = await page_for(MockPortal())
    assert not await adapter(FakeLlm(), login_url=None).is_authenticated(page)   # no page to check
    assert not await adapter(FakeLlm(), probe_url=f"{HOST}/").is_authenticated(page)


@pytest.mark.asyncio
async def test_without_an_llm_unknown_fields_fail_clearly(page_for):
    fields = """<input id="kdnr" name="u" type="text"><input name="p" type="password">
<button type="submit">Go</button>"""
    portal = MockPortal(fields=fields)
    with pytest.raises(LoginFailed, match="No username field"):
        await adapter(None).login(await page_for(portal), cred())
    assert portal.posts == []                     # the password was not sent alone


@pytest.mark.asyncio
async def test_login_through_the_auth_manager_like_the_pipeline(page_for):
    """AuthManager.login runs adapter.login, then the signed-in check, and stores
    the account state: the path every real run takes."""
    from sourcing.auth.manager import AuthManager
    from sourcing.browser.base import BrowserHandle
    from sourcing.store import MemoryStore

    portal = MockPortal()
    page = await page_for(portal)
    store = MemoryStore()
    manager = AuthManager(store, browser=None)
    a = adapter(FakeLlm())
    probes = []
    real_probe = a.is_authenticated
    a.is_authenticated = lambda pg: probes.append(pg.url) or real_probe(pg)
    await manager.login(BrowserHandle("s1", page, None, None), a, cred())
    account = store.get_account("example__")
    assert account["state"] == "ACTIVE" and account["last_login_at"]
    # The login saw Log out: no second sign-in check (DASI, 2026-10-29), and
    # the login values still count as proven.
    assert probes == [] and "logout_link" in a.confirmed_learned()


class SilentRejectPortal(MockPortal):
    """Wrong password shows the form again with no message, or a server error page."""

    def __init__(self, *, server_error=False, **kw):
        super().__init__(**kw)
        self.server_error = server_error

    def _after_password(self, ok: bool) -> str:
        if self.server_error:
            return "<html><body><h1>500 - Internal Server Error</h1></body></html>"
        if not ok:
            return LOGIN_PAGE.format(error="", fields=self.fields)
        return super()._after_password(ok)


@pytest.mark.asyncio
async def test_login_failures_say_whether_a_retry_is_safe(page_for):
    # Back on the form with no message: maybe a rejection we can't read, so a
    # second submit could be the one that locks the account.
    with pytest.raises(LoginFailed) as exc:
        await adapter(FakeLlm(state="still_login")).login(
            await page_for(SilentRejectPortal()), cred(password="nope"))
    assert not exc.value.retryable and exc.value.credentials_sent

    # A server error page: the credentials were not judged, one more try is safe.
    llm = FakeLlm(state="signed_in")
    with pytest.raises(LoginFailed, match="Server error") as exc:
        await adapter(llm).login(await page_for(SilentRejectPortal(server_error=True)), cred())
    assert exc.value.retryable and llm.calls == []

    # Nothing was typed: the failure doesn't count towards the vendor's lockout.
    with pytest.raises(LoginFailed) as exc:
        await adapter(FakeLlm(), login_url="https://evil.example.com/login").login(
            await page_for(MockPortal()), cred())
    assert not exc.value.retryable and not exc.value.credentials_sent
    fields = '<input id="kdnr" name="u" type="text"><input name="p" type="password">'
    with pytest.raises(LoginFailed) as exc:
        await adapter(None).login(await page_for(MockPortal(fields=fields)), cred())
    assert not exc.value.credentials_sent


# -- what is saved for later runs (2026-10-22) ----------------------------------

@pytest.mark.asyncio
async def test_only_a_confirmed_sign_in_makes_its_selectors_worth_saving(page_for):
    fields = """<input id="kdnr" name="u" type="text" placeholder="Customer number">
<input id="secret" name="p" type="password"><button type="submit">Go</button>"""
    llm = FakeLlm(fields=lambda els: {"username": index_of(id="kdnr")(els)})
    failed = adapter(llm)
    with pytest.raises(InvalidCredentials):
        await failed.login(await page_for(MockPortal(fields=fields)), cred(password="nope"))
    assert failed.learned["username_input"] == '[id="kdnr"]'
    assert failed.confirmed_learned() == {}                # a failed login proves nothing

    portal = MockPortal(fields=fields)
    page = await page_for(portal)
    ok = adapter(FakeLlm(fields=lambda els: {"username": index_of(id="kdnr")(els)}))
    await ok.login(page, cred())
    # Log out seen after the submit is the sign-in check's own proof (2026-10-30).
    assert ok.signed_in_proof
    assert ok.confirmed_learned()["username_input"] == '[id="kdnr"]'
    assert "search_input" not in ok.confirmed_learned()


def test_generated_ids_are_not_saved():
    sel = GenericAdapter._selector_for
    assert sel({"index": 1, "tag": "input", "type": "email", "id": ":r4:",
                "autocomplete": "username"}) == ('[data-sourcing-el="1"]',
                                                 'input[autocomplete="username"]')
    assert sel({"index": 2, "tag": "div", "role": "button", "text": "Continue"}) == (
        '[data-sourcing-el="2"]', "[role='button']:has-text('Continue')")
    assert sel({"index": 3, "tag": "input", "type": "text", "id": "kdnr"}) == (
        '[id="kdnr"]', '[id="kdnr"]')
    assert sel({"index": 4, "tag": "input", "type": "text", "name": "a1b2c3d4e5f6"})[1] is None
    assert sel({"index": 5, "tag": "input", "type": "submit", "text": "Sign in"})[1] == (
        'input[value="Sign in"]')


@pytest.mark.asyncio
async def test_a_saved_selector_that_now_finds_the_wrong_element_is_skipped(page_for):
    # After a site change the old password selector finds the username box.
    portal = MockPortal()
    a = adapter(FakeLlm(), selectors={"password_input": '[id="username"]',
                                      "submit_button": '[id="username"]'})
    await a.login(await page_for(portal), cred())
    assert portal.signed_in
    assert a.learned.get("password_input") != '[id="username"]'
    assert a.found_by["password_input"] == "generic list"


# Monroe (2026-11-04): "Forgot Login" comes before "Sign In", and Sign In opens a
# pop-up whose form is drawn by script inside an about:blank iframe.
POPUP_HOME = """<html><body><h1>Parts</h1>
<a href="/login.php#/forgotPassword">Forgot Login</a>
<a id="signin" href="#/login" onclick="openPopup(); return false;"><div>Sign In</div></a>
<script>
function openPopup() {
  setTimeout(() => {
    const f = document.createElement('iframe');
    f.style.width = '400px'; f.style.height = '300px';
    document.body.appendChild(f);
    f.contentDocument.body.innerHTML = `<form>
      <label>Email</label><input type="email" name="u"><label>Password</label>
      <input type="password" name="p"><button type="button">Sign In</button></form>`;
    // The page's script handles the button (a form in a script-made blank frame
    // gets no submit event in Chromium; such pop-ups sign in by script).
    const form = f.contentDocument.querySelector('form');
    form.querySelector('button').addEventListener('click', () => postLogin(form.u.value, form.p.value));
    location.hash = '#/login';
  }, 800);
}
// The pop-up's script signs in for the page (as a React pop-up does).
function postLogin(u, p) {
  const form = document.createElement('form');
  form.method = 'post'; form.action = '{action}';
  for (const [k, v] of [['u', u], ['p', p]]) {
    const i = document.createElement('input'); i.name = k; i.value = v; form.appendChild(i);
  }
  document.body.appendChild(form); form.submit();
}
</script></body></html>"""


class PopupPortal(MockPortal):
    def __init__(self, action=f"{HOST}/account/login", **kw):
        super().__init__(**kw)
        self.action = action

    async def handle(self, route):
        req = route.request
        url = urlparse(req.url)
        if url.path == "/" and req.method == "GET" and not self.signed_in:
            return await route.fulfill(status=200, content_type="text/html",
                                       body=POPUP_HOME.replace("{action}", self.action))
        if url.path.startswith("/login.php"):
            # The "Forgot Login" page: no sign-in form at all.
            return await route.fulfill(status=200, content_type="text/html",
                                       body="<html><body><p>Reset your login</p></body></html>")
        return await super().handle(route)


@pytest.mark.asyncio
async def test_sign_in_form_in_a_popup_iframe_is_used_and_forgot_login_skipped(page_for):
    portal = PopupPortal()
    llm = FakeLlm()
    a = adapter(llm, login_url=None, website=f"{HOST}/")
    await a.login(await page_for(portal), cred())
    assert portal.signed_in
    assert portal.posts == [("/account/login", {"u": "jane", "p": "pw"})]
    assert llm.calls == []                       # found by the rules, inside the frame


@pytest.mark.asyncio
async def test_popup_form_posting_off_site_gets_no_password(page_for):
    # The frame is the page's own (about:blank), but its form posts elsewhere: the
    # page stays on the vendor's site, so only the submit target differs. A frame
    # with its own address off the vendor's site is never typed into.
    page = await page_for(MockPortal())
    await page.route("https://evil.example.org/**", lambda route: route.fulfill(
        status=200, content_type="text/html",
        body="<form><input type=email name=u><input type=password name=p></form>"))
    await page.goto(f"{HOST}/")
    await page.set_content("""<h1>Sign in</h1><iframe src="https://evil.example.org/login"
        style="width:400px;height:300px"></iframe>""")
    a = adapter(FakeLlm())
    assert await a._form_frame(page) is None
