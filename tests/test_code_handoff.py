"""2FA code handoff (sourcing/auth/hitl.py): a person sends the portal's login
code from Google Chat (form link or Chat-app card) and the waiting login types
it in. The mock portal signs in Notion-style: email, Continue, emailed code."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import service.api as api
import sourcing.auth.hitl as hitl
from sourcing.auth.hitl import (
    CODE_REJECTED, EXPIRED, SIGNED_IN, PersonHandoff, CodeNotGiven, accept_code, code_card,
    hitl_id, login_with_handoff,
)
from sourcing.browser.base import LiveViewUnavailable
from sourcing.config import get_settings
from sourcing.credentials.base import CredentialError, MemoryCredentialProvider, VendorCredential
from sourcing.store import FileStore, MemoryStore
from sourcing.vendors.base import LoginFailed, MfaRequired

from .test_generic_adapter import HOST, FakeLlm, adapter, page_for  # noqa: F401

CODE = "4821-77"

# Like Notion's (2026-10-22): the other sign-in buttons come first and submit the
# same form (only the "another account" filter keeps the code from clicking them),
# a decoy password box for password managers, and Continue as a <div role=button>.
EMAIL_PAGE = """<html><body><h1>Log in</h1>
<form method="post" action="/account/login">
<button type="submit" name="sso" value="google">Continue with Google</button>
<button type="submit" name="sso" value="apple">Continue with Apple</button>
<input id="email" name="u" type="email" placeholder="Enter your email address">
<input type="password" aria-hidden="true" tabindex="-1" autocomplete="current-password">
<div role="button" tabindex="0" onclick="this.closest('form').submit()"><span>Continue</span></div>
</form></body></html>"""
CODE_PAGE = """<html><body><h1>Log in</h1>
<p>We just sent you a temporary login code. Please check your inbox.</p>
<button>Continue with Google</button>
<form method="post" action="/account/code">
<input name="loginCode" placeholder="Enter login code">
<button type="submit">Continue with login code</button></form>{error}</body></html>"""
APP_PAGE = """<html><body><h1>Workspace</h1><a href="/account/logout">Log out</a></body></html>"""


class CodeLoginPortal:
    """Email, then a code sent by email; never a password."""

    def __init__(self, *, ask_password=False):
        self.ask_password = ask_password
        self.signed_in = False
        self.clicked_google = False
        self.codes: list[str] = []

    async def handle(self, route):
        req = route.request
        path = urlparse(req.url).path.lower()
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        if path == "/account/login" and req.method == "POST" and form.get("sso"):
            self.clicked_google = True
            html = "<html><body>Redirecting to accounts.google.com</body></html>"
        elif path == "/account/login" and req.method == "POST":
            html = (EMAIL_PAGE.replace('type="email"', 'type="email"><input name="p" '
                                       'type="password"') if self.ask_password
                    else CODE_PAGE.format(error=""))
        elif path == "/account/code" and req.method == "POST":
            self.codes.append(form.get("loginCode", ""))
            if form.get("loginCode") == CODE:
                self.signed_in = True
                html = APP_PAGE
            else:
                html = CODE_PAGE.format(error="<p>Invalid code, try again.</p>")
        elif self.signed_in:
            html = APP_PAGE
        else:
            html = EMAIL_PAGE
        await route.fulfill(status=200, content_type="text/html", body=html)


def code_cred():
    return VendorCredential("example", "", "sourcing-vendor-example", "jane@co.com", "",
                            passwordless=True)


class Person:
    """Reads each Chat message and answers the form with the next code."""

    def __init__(self, store, codes):
        self.store, self.codes, self.messages = store, list(codes), []

    async def send(self, text, *, to=None):
        self.messages.append((to, text))
        if self.codes:
            token = parse_qs(urlparse(text.split()[-1]).query)["t"][0]
            code = self.codes.pop(0)
            asyncio.get_running_loop().call_later(
                0.05, lambda: accept_code(self.store, token, code, via="form"))


def handoff_for(store, person, wait=5):
    return PersonHandoff(store, person, "http://localhost:8080", wait_seconds=wait,
                       poll_seconds=0.02)


# -- credentials --------------------------------------------------------------

@pytest.mark.asyncio
async def test_passwordless_login_needs_the_flag():
    secrets = MemoryCredentialProvider({"sourcing-vendor-example": {"username": "jane@co.com"}})
    with pytest.raises(CredentialError):
        await secrets.resolve("example", "")
    secrets.secrets["sourcing-vendor-example"]["passwordless"] = True
    cred = await secrets.resolve("example", "")
    assert cred.passwordless and cred.password == ""


# -- adapter ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_code_page_after_continue_is_2fa_with_its_code_box(page_for):
    portal = CodeLoginPortal()
    with pytest.raises(MfaRequired) as exc:
        await adapter(FakeLlm()).login(await page_for(portal), code_cred())
    assert exc.value.method == "email" and exc.value.input_selector
    assert not portal.clicked_google


@pytest.mark.asyncio
async def test_person_code_is_typed_in_and_a_wrong_one_is_reported(page_for):
    portal = CodeLoginPortal()
    a, page = adapter(FakeLlm()), await page_for(portal)
    with pytest.raises(MfaRequired) as first:
        await a.login(page, code_cred())
    with pytest.raises(MfaRequired) as wrong:
        await a.submit_mfa_code(page, code_cred(), "0000-00", first.value.input_selector)
    assert wrong.value.method == "code_rejected"
    await a.submit_mfa_code(page, code_cred(), CODE, wrong.value.input_selector)
    assert portal.signed_in and portal.codes == ["0000-00", CODE]


@pytest.mark.asyncio
async def test_passwordless_login_never_meets_a_password_field(page_for):
    with pytest.raises(LoginFailed, match="passwordless") as exc:
        await adapter(FakeLlm()).login(await page_for(CodeLoginPortal(ask_password=True)),
                                       code_cred())
    assert not exc.value.retryable and not exc.value.credentials_sent


# -- handoff ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_finishes_with_the_code_from_the_chat_form(page_for):
    store = MemoryStore()
    portal = CodeLoginPortal()
    person = Person(store, [CODE])
    await login_with_handoff(adapter(FakeLlm()), await page_for(portal), code_cred(),
                             handoff_for(store, person), account="example__",
                             people=("jane@co.com",))
    assert portal.signed_in
    (to, text), = person.messages
    assert to == "jane@co.com" and "login code" in text and "/hitl/code?t=" in text
    doc, = store.hitl.values()
    assert doc["state"] == SIGNED_IN and doc["code"] is None      # code not kept


@pytest.mark.asyncio
async def test_a_rejected_code_is_asked_once_more(page_for):
    store = MemoryStore()
    portal = CodeLoginPortal()
    person = Person(store, ["1111-11", CODE])
    await login_with_handoff(adapter(FakeLlm()), await page_for(portal), code_cred(),
                             handoff_for(store, person), account="example__")
    assert portal.signed_in and len(person.messages) == 2
    assert "not accepted" in person.messages[1][1] and "sent by email" in person.messages[1][1]
    assert sorted(d["state"] for d in store.hitl.values()) == [CODE_REJECTED, SIGNED_IN]


@pytest.mark.asyncio
async def test_no_code_in_time(page_for):
    store = MemoryStore()
    with pytest.raises(CodeNotGiven) as exc:
        await login_with_handoff(adapter(FakeLlm()), await page_for(CodeLoginPortal()),
                                 code_cred(), handoff_for(store, Person(store, []), wait=0.2),
                                 account="example__")
    assert exc.value.asked
    doc, = store.hitl.values()
    assert doc["state"] == EXPIRED


@pytest.mark.asyncio
async def test_without_a_handoff_the_2fa_stops_as_before(page_for):
    with pytest.raises(CodeNotGiven) as exc:
        await login_with_handoff(adapter(FakeLlm()), await page_for(CodeLoginPortal()),
                                 code_cred(), None, account="example__")
    assert not exc.value.asked and exc.value.method == "email"


def test_accept_code_checks_the_request():
    store = MemoryStore()
    store.update_hitl(hitl_id("tok"), {"state": "waiting", "expires_at": 9e12,
                                       "vendor_name": "Example"})
    assert accept_code(store, "nope", CODE, via="form")[0] is False
    assert accept_code(store, "tok", "12; DROP", via="form")[0] is False
    assert accept_code(store, "tok", " 4821 77 ", via="form")[0] is True
    assert store.get_hitl(hitl_id("tok"))["code"] == "482177"
    assert accept_code(store, "tok", CODE, via="form")[0] is False   # used once
    store.update_hitl(hitl_id("old"), {"state": "waiting", "expires_at": 1})
    assert "expired" in accept_code(store, "old", CODE, via="form")[1]


def test_file_store_requests_are_shared_between_processes(tmp_path):
    path = str(tmp_path / "store.json")
    worker, api_side = FileStore(path), FileStore(path)
    worker.update_hitl("abc", {"state": "waiting", "expires_at": 9e12})
    api_side.update_hitl("abc", {"state": "code_given", "code": CODE})
    assert worker.get_hitl("abc") == {"state": "code_given", "code": CODE, "expires_at": 9e12}


# -- API ----------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CHAT_APP_AUDIENCE", raising=False)
    get_settings.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(api, "_store", store)
    store.update_hitl(hitl_id("tok"), {"state": "waiting", "expires_at": 9e12,
                                       "vendor_name": "Notion", "method": "email"})
    yield TestClient(api.app), store
    get_settings.cache_clear()


def test_code_form_round_trip(client):
    c, store = client
    page = c.get("/hitl/code", params={"t": "tok"})
    assert page.status_code == 200 and "Notion" in page.text and 'name="code"' in page.text
    assert c.get("/hitl/code", params={"t": "bad"}).status_code == 404
    sent = c.post("/hitl/code", data={"t": "tok", "code": CODE})
    assert sent.status_code == 200 and "Typing the code" in sent.text
    assert store.get_hitl(hitl_id("tok"))["code"] == CODE
    assert 'name="code"' not in c.get("/hitl/code", params={"t": "tok"}).text


def test_chat_card_submit_reaches_the_request(client):
    c, store = client
    card = code_card("tok", vendor_name="Notion", method="email", form_url="http://x",
                     minutes=10)
    button = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"][0]
    event = {"type": "CARD_CLICKED",
             "action": {"actionMethodName": button["onClick"]["action"]["function"],
                        "parameters": button["onClick"]["action"]["parameters"]},
             "common": {"formInputs": {"code": {"stringInputs": {"value": [CODE]}}}}}
    assert "Typing the code" in c.post("/chat/events", json=event).json()["text"]
    assert store.get_hitl(hitl_id("tok"))["via"] == "chat card"


def test_chat_events_refused_on_cloud_run_without_an_audience(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("K_SERVICE", "sourcing-api")
    assert c.post("/chat/events", json={"type": "MESSAGE"}).status_code == 403


# -- Live View: the person logs in on the open page ---------------------------

class LiveViewPerson:
    """Opens the Live View link from Chat and types the code into the page."""

    def __init__(self, page, code=CODE):
        self.page, self.code, self.messages = page, code, []

    async def send(self, text, *, to=None):
        self.messages.append((to, text))
        if "live browser" in text and self.code:
            async def log_in():
                await asyncio.sleep(0.1)
                await self.page.fill("input[name='loginCode']", self.code)
                await self.page.click("button:has-text('login code')")
            asyncio.get_running_loop().create_task(log_in())


async def live_url():
    return "https://www.browserbase.com/devtools-fullscreen/inspector.html?wss=abc"


@pytest.mark.asyncio
async def test_person_logs_in_through_the_live_view_link(page_for, monkeypatch):
    monkeypatch.setattr(hitl, "LIVE_POLL_SECONDS", 0.1)
    store, portal = MemoryStore(), CodeLoginPortal()
    a, page = adapter(FakeLlm()), await page_for(portal)
    person = LiveViewPerson(page)
    await login_with_handoff(a, page, code_cred(), handoff_for(store, person),
                             account="example__", people=("jane@co.com",),
                             live_view_url=live_url)
    assert portal.signed_in and portal.codes == [CODE]
    (to, ask), (_, thanks) = person.messages
    assert to == "jane@co.com" and "devtools-fullscreen" in ask and "/hitl/code" not in ask
    assert "signed in" in thanks
    doc, = store.hitl.values()
    assert doc["state"] == SIGNED_IN and doc["mode"] == "live_view"
    assert a.learned["home_url"].startswith(HOST)


@pytest.mark.asyncio
async def test_nobody_logs_in_through_the_live_view(page_for, monkeypatch):
    monkeypatch.setattr(hitl, "LIVE_POLL_SECONDS", 0.05)
    store, page = MemoryStore(), await page_for(CodeLoginPortal())
    with pytest.raises(CodeNotGiven) as exc:
        await login_with_handoff(adapter(FakeLlm()), page, code_cred(),
                                 handoff_for(store, LiveViewPerson(page, code=None), wait=0.3),
                                 account="example__", live_view_url=live_url)
    assert exc.value.asked
    doc, = store.hitl.values()
    assert doc["state"] == EXPIRED


@pytest.mark.asyncio
async def test_without_live_view_the_code_form_is_used(page_for):
    async def no_live_view():
        raise LiveViewUnavailable("local browser")

    store, portal = MemoryStore(), CodeLoginPortal()
    person = Person(store, [CODE])
    await login_with_handoff(adapter(FakeLlm()), await page_for(portal), code_cred(),
                             handoff_for(store, person), account="example__",
                             live_view_url=no_live_view)
    assert portal.signed_in and "/hitl/code?t=" in person.messages[0][1]


@pytest.mark.asyncio
async def test_the_adapter_can_end_the_live_view_wait(monkeypatch):
    # Textron 2026-11-13: the sign-in went back to Send Code and the run waited out its
    # 10 minutes. An adapter that sees the sign-in end raises MfaRequired: the wait stops.
    from sourcing.vendors.base import MfaRequired
    monkeypatch.setattr(hitl, "LIVE_POLL_SECONDS", 0.01)

    class Ends:
        display_name = "Textron Aviation"
        accepts_person_login = True
        accepts_mfa_codes = True
        checks = 0

        async def login(self, page, cred):
            raise MfaRequired("email", input_selector="#Code")

        async def person_finished_login(self, page):
            self.checks += 1
            if self.checks == 2:
                raise MfaRequired("email", "back on the sign-in page")
            return False

    class Notes:
        def __init__(self):
            self.messages = []

        async def send(self, text, to=None):
            self.messages.append(text)

    store, notes = MemoryStore(), Notes()
    with pytest.raises(CodeNotGiven) as exc:
        await login_with_handoff(Ends(), object(), code_cred(), handoff_for(store, notes, wait=60),
                                 account="textron__", live_view_url=live_url)
    assert exc.value.asked                      # rows NEEDS_ATTENTION, account NEEDS_LOGIN
    doc, = store.hitl.values()
    assert doc["state"] == EXPIRED and "back on the sign-in page" in doc["ended"]
    assert "ended without signing in" in notes.messages[-1]
