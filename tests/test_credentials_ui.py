"""Login update page (service/credentials_ui.py) and Google sign-in (service/web_auth.py)."""
from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

import service.api as api
import service.credentials_ui as ui
import service.web_auth as web_auth
from sourcing.config import get_settings
from sourcing.credentials.access import Login, can_edit
from sourcing.credentials.base import MemoryCredentialProvider, secret_key
from sourcing.models import SHARED, AccountState
from sourcing.store import MemoryStore

JANE, BOB, ADMIN = "jane@skylink.com", "bob@skylink.com", "boss@skylink.com"
SEED = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("CREDENTIAL_ADMINS", ADMIN)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "skylink.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    # Independent of a developer's .env (its OAuth client, overrides).
    for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "DEV_SIGNIN_EMAIL",
                 "VENDOR_SECRET_OVERRIDES"):
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()
    store = MemoryStore()
    store.update_account(f"proponent__{SHARED}", {"vendor_id": "proponent", "user_email": SHARED,
                                                  "editors": [BOB]})
    store.update_account(f"proponent__{JANE}", {
        "vendor_id": "proponent", "user_email": JANE,
        "state": AccountState.NEEDS_CREDENTIAL_UPDATE.value, "last_error": "wrong password"})
    creds = MemoryCredentialProvider({
        secret_key("proponent", SHARED): {"username": "team", "password": "old", "url": "https://x"},
        secret_key("proponent", JANE): {"username": "jane", "password": "old", "totp_secret": SEED},
    })
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(ui, "_creds", creds)
    yield monkeypatch, store, creds
    get_settings.cache_clear()


def signed_in(monkeypatch, email: str) -> TestClient:
    monkeypatch.setenv("DEV_SIGNIN_EMAIL", email)
    get_settings.cache_clear()
    client = TestClient(api.app)
    assert client.get("/auth/login").status_code == 200   # follows to /credentials
    monkeypatch.delenv("DEV_SIGNIN_EMAIL")
    get_settings.cache_clear()
    return client


def csrf(page_html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', page_html).group(1)


def save(client, owner, password="new-pw", password2=None, token=None, **extra):
    form = client.get("/credentials/update", params={"vendor": "proponent", "owner": owner})
    data = {"csrf": token if token is not None else csrf(form.text), "vendor": "proponent",
            "owner": owner, "password": password,
            "password2": password if password2 is None else password2, **extra}
    return client.post("/credentials/update", data=data)


def test_signed_out_goes_to_sign_in(env):
    resp = TestClient(api.app).get("/credentials?vendor=proponent", follow_redirects=False)
    assert resp.status_code == 401 and "Sign in with Google" in resp.text
    assert "Skylink accounts only." in resp.text and "@skylink.com Google" in resp.text
    assert 'href="/auth/login?next=%2Fcredentials%3Fvendor%3Dproponent"' in resp.text


def test_each_person_sees_only_logins_they_may_change(env):
    monkeypatch, _, _ = env
    jane = signed_in(monkeypatch, JANE).get("/credentials?vendor=proponent").text
    assert "Your own login" in jane and "Shared team login" not in jane
    assert "password rejected" in jane

    bob = signed_in(monkeypatch, BOB).get("/credentials?vendor=proponent").text
    assert "Shared team login" in bob and "jane@skylink.com" not in bob

    admin = signed_in(monkeypatch, ADMIN).get("/credentials?vendor=proponent").text
    assert "Shared team login" in admin and "jane@skylink.com&#x27;s login" in admin

    other = signed_in(monkeypatch, "ann@skylink.com").get("/credentials").text
    assert "no vendor logins you can change" in other


def test_vendor_picker_lists_vendors(env):
    monkeypatch, _, _ = env
    page = signed_in(monkeypatch, BOB).get("/credentials").text
    assert '<option value="proponent">Proponent</option>' in page


def test_credentials_page_polish(env):
    monkeypatch, store, _ = env
    account = store.accounts[f"proponent__{JANE}"]
    account["password_changed_at"] = 1700000000.0
    account["password_changed_by"] = "jane@skylink.com"

    jane_page = signed_in(monkeypatch, JANE).get("/credentials?vendor=proponent").text

    # 1. Subtitle matches real actions (no "2FA secrets")
    assert "Manage login credentials for Proponent." in jane_page
    assert "2FA secrets" not in jane_page

    # 2. Only ONE "Other vendor" back link (top breadcrumb, no bottom button)
    assert '<div class="breadcrumb"><a href="/credentials">← Other vendor</a></div>' in jane_page
    assert 'class="action-footer"' not in jane_page
    assert jane_page.count("Other vendor") == 1

    # 3. Status label clarified with tooltip
    assert 'title="The worker failed to log in with this password. Please update it."' in jane_page

    # 4. Action column right-aligned, smaller button
    assert '<th class="col-action">Action</th>' in jane_page
    assert '<td class="col-action">' in jane_page
    assert 'class="btn btn-primary btn-sm"' in jane_page

    # 5. Timestamp and "by <email>" on separate lines
    assert '<div class="timestamp">' in jane_page
    assert '<div class="author">by jane@skylink.com</div>' in jane_page


def test_password_change_adds_a_version_and_unblocks_the_account(env):
    monkeypatch, store, creds = env
    resp = save(signed_in(monkeypatch, JANE), JANE)
    assert resp.status_code == 200 and "Saved" in resp.text and "back to NEW" in resp.text
    secret = creds.secrets[secret_key("proponent", JANE)]
    assert secret["password"] == "new-pw" and secret["username"] == "jane"
    assert secret["totp_secret"] == SEED and secret["updated_by"] == JANE   # 2FA key kept
    account = store.accounts[f"proponent__{JANE}"]
    assert account["state"] == AccountState.NEEDS_LOGIN.value and account["last_error"] is None
    assert account["password_changed_by"] == JANE
    event = store.audit_log[-1]
    assert event["event"] == "credential_updated" and event["fields"] == ["password"]
    assert "new-pw" not in str(store.audit_log) and "new-pw" not in str(store.accounts)
    assert "new-pw" not in resp.text


def test_shared_login_editor_can_change_it_and_others_cannot(env):
    monkeypatch, _, creds = env
    jane = signed_in(monkeypatch, JANE)
    assert jane.get("/credentials/update",
                    params={"vendor": "proponent", "owner": SHARED}).status_code == 403
    # Posting directly, without the form, is refused too.
    form = jane.get("/credentials/update", params={"vendor": "proponent", "owner": JANE})
    resp = jane.post("/credentials/update", data={
        "csrf": csrf(form.text), "vendor": "proponent", "owner": SHARED,
        "password": "x1", "password2": "x1"})
    assert resp.status_code == 403
    assert creds.secrets[secret_key("proponent", SHARED)]["password"] == "old"

    resp = save(signed_in(monkeypatch, BOB), SHARED, username="team2")
    assert resp.status_code == 200
    secret = creds.secrets[secret_key("proponent", SHARED)]
    assert (secret["password"], secret["username"], secret["url"]) == ("new-pw", "team2", "https://x")


def test_form_checks(env):
    monkeypatch, _, creds = env
    jane = signed_in(monkeypatch, JANE)
    assert save(jane, JANE, token="forged").status_code == 400
    assert "not the same" in save(jane, JANE, password2="other").text
    assert "spaces" in save(jane, JANE, password=" pw ").text
    assert "2FA key is not valid" in save(jane, JANE, totp_secret="not-base32!").text
    assert creds.secrets[secret_key("proponent", JANE)]["password"] == "old"


def test_missing_secret_is_not_created(env):
    monkeypatch, store, creds = env
    store.update_account(f"proponent__{BOB}", {"vendor_id": "proponent", "user_email": BOB})
    resp = save(signed_in(monkeypatch, BOB), BOB)
    assert resp.status_code == 409 and "no secret" in resp.text
    assert secret_key("proponent", BOB) not in creds.secrets


def test_csrf_token_belongs_to_one_session(env):
    monkeypatch, _, creds = env
    first = signed_in(monkeypatch, JANE)
    token = csrf(first.get("/credentials/update",
                           params={"vendor": "proponent", "owner": JANE}).text)
    second = signed_in(monkeypatch, JANE)
    assert save(second, JANE, token=token).status_code == 400


def test_dev_sign_in_is_refused_on_cloud_run(env):
    monkeypatch, _, _ = env
    monkeypatch.setenv("K_SERVICE", "sourcing-api")
    monkeypatch.setenv("DEV_SIGNIN_EMAIL", JANE)
    get_settings.cache_clear()
    resp = TestClient(api.app).get("/auth/login", follow_redirects=False)
    assert resp.status_code == 503      # falls through to Google, which isn't set up here


# -- Google sign-in ---------------------------------------------------------------

def _claims(**over):
    return {"iss": "https://accounts.google.com", "email": JANE, "email_verified": True,
            "hd": "skylink.com", "name": "Jane", **over}


@pytest.fixture
def google(env, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    replies = {}

    async def fake_exchange(code, redirect_uri, with_token=False):
        assert redirect_uri == "http://testserver/auth/callback"
        if with_token:
            return replies["claims"], replies.get("token", {
                "access_token": "ya29.admin-token", "scope": f"openid email {web_auth.GCP_SCOPE}"})
        return replies["claims"]

    monkeypatch.setattr(web_auth, "exchange_code", fake_exchange)
    return replies


def _start(client):
    resp = client.get("/auth/login?next=/credentials%3Fvendor%3Dproponent", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith(web_auth.GOOGLE_AUTH_URL)
    query = dict(p.split("=", 1) for p in resp.headers["location"].split("?", 1)[1].split("&"))
    assert query["hd"] == "skylink.com" and "openid" in query["scope"]
    return query["state"], web_auth.unsign(client.cookies[web_auth.STATE_COOKIE],
                                           "test-session-secret")["nonce"]


def test_google_sign_in(google):
    client = TestClient(api.app)
    state, nonce = _start(client)
    google["claims"] = _claims(nonce=nonce)
    resp = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/credentials?vendor=proponent"
    assert "Your own login" in client.get("/credentials?vendor=proponent").text


@pytest.mark.parametrize("claims, why", [
    (_claims(email="eve@gmail.com", hd=None), "not a Skylink Google account"),
    (_claims(hd=None), "not a Skylink Google account"),      # personal account, company email
    (_claims(email_verified=False), "did not confirm"),
    (_claims(nonce="other"), "does not match"),
])
def test_google_sign_in_refused(google, claims, why):
    client = TestClient(api.app)
    state, nonce = _start(client)
    google["claims"] = {"nonce": nonce, **claims}
    resp = client.get(f"/auth/callback?code=c&state={state}")
    assert resp.status_code == 403 and why in resp.text
    assert "Access denied" in resp.text and "Use your @skylink.com Google Workspace account" in resp.text
    assert 'href="/auth/login"' in resp.text and "Sign in with a different account" in resp.text
    assert web_auth.SESSION_COOKIE not in client.cookies


def test_any_google_account_only_locally(google, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "*")
    get_settings.cache_clear()
    gmail = _claims(email="dev@gmail.com", hd=None)
    assert web_auth.email_allowed(gmail) is None
    assert web_auth.email_allowed(_claims(email_verified=False)) is not None
    monkeypatch.setenv("K_SERVICE", "sourcing-api")
    assert "local testing only" in web_auth.email_allowed(gmail)


def test_google_callback_needs_the_matching_state(google):
    client = TestClient(api.app)
    _, nonce = _start(client)
    google["claims"] = _claims(nonce=nonce)
    assert client.get("/auth/callback?code=c&state=forged").status_code == 400
    assert TestClient(api.app).get("/auth/callback?code=c&state=x").status_code == 400


def test_signed_values():
    token = web_auth.sign({"email": JANE}, "k", 60)
    assert web_auth.unsign(token, "k")["email"] == JANE
    assert web_auth.unsign(token, "other") is None
    body, _, mac = token.partition(".")
    assert web_auth.unsign(body[:-2] + "xx." + mac, "k") is None
    assert web_auth.unsign(web_auth.sign({"email": JANE}, "k", -1), "k") is None


def test_next_stays_on_this_site():
    assert web_auth._safe_next("/credentials?vendor=x") == "/credentials?vendor=x"
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "evil"):
        assert web_auth._safe_next(bad) == "/credentials"


def test_can_edit_rules():
    shared = Login("proponent", SHARED, editors=(BOB,))
    own = Login("proponent", JANE)
    assert can_edit(BOB, shared) and not can_edit(JANE, shared)
    assert can_edit("Jane@Skylink.com", own) and not can_edit(BOB, own)
    assert can_edit(ADMIN, shared, admins=[ADMIN]) and can_edit(ADMIN, own, admins=[ADMIN])
    assert not can_edit("", shared)


# -- admins: add a login, editors --------------------------------------------------

from google.api_core import exceptions as gexc  # noqa: E402

import service.credentials_ui as ui_mod  # noqa: E402


class FakeSecretManager:
    """create_secret / list_secret_versions / add_secret_version, by secret id."""

    def __init__(self, existing=None, deny=False):
        self.versions = {k: list(v) for k, v in (existing or {}).items()}
        self.deny = deny

    def create_secret(self, parent, secret_id, secret):
        assert parent == "projects/test-proj" and secret["labels"] == {"app": "sourcing"}
        if self.deny:
            raise gexc.PermissionDenied("no secrets.create")
        if secret_id in self.versions:
            raise gexc.AlreadyExists("exists")
        self.versions[secret_id] = []

    def list_secret_versions(self, request=None, *, parent=None):
        # The real client's shape: the filter only fits in `request`.
        assert request and request.get("filter") == "state:ENABLED" and parent is None
        return iter(self.versions[request["parent"].rsplit("/", 1)[1]])

    def add_secret_version(self, parent, payload):
        self.versions[parent.rsplit("/", 1)[1]].append(payload["data"])


@pytest.fixture
def admin_env(google, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    get_settings.cache_clear()
    fake = FakeSecretManager()
    monkeypatch.setattr(ui_mod, "_secret_client", fake)
    return google, fake


def granted(google, monkeypatch, email=ADMIN, **claims):
    """Signed in, then Google Cloud access given through /auth/grant."""
    client = signed_in(monkeypatch, email)
    resp = client.get("/auth/grant?next=/credentials/new", follow_redirects=False)
    assert resp.status_code == 303
    query = dict(p.split("=", 1) for p in resp.headers["location"].split("?", 1)[1].split("&"))
    assert "cloud-platform" in query["scope"] and query["access_type"] == "online"
    saved = web_auth.unsign(client.cookies[web_auth.STATE_COOKIE], "test-session-secret")
    google["claims"] = _claims(email=email, nonce=saved["nonce"], **claims)
    resp = client.get(f"/auth/callback?code=c&state={query['state']}", follow_redirects=False)
    return client, resp


def new_login(client, **over):
    form = client.get("/credentials/new")
    data = {"csrf": csrf(form.text), "vendor": "ILS", "kind": "shared", "owner_email": "",
            "username": "skyuser", "password": "s3cret!", "password2": "s3cret!",
            "totp_secret": "", "url": "", "editors": BOB, **over}
    return client.post("/credentials/new", data=data)


def test_only_admins_add_logins_or_change_editors(admin_env, monkeypatch):
    _, fake = admin_env
    bob = signed_in(monkeypatch, BOB)
    assert "Add a login" not in bob.get("/credentials").text
    assert "Who can change it" not in bob.get("/credentials?vendor=proponent").text
    assert bob.get("/credentials/new").status_code == 403
    token = csrf(bob.get("/credentials/update", params={"vendor": "proponent",
                                                       "owner": SHARED}).text)
    assert bob.post("/credentials/new", data={"csrf": token, "vendor": "ils"}).status_code == 403
    assert bob.get("/credentials/editors", params={"vendor": "proponent",
                                                   "owner": SHARED}).status_code == 403
    assert bob.post("/credentials/editors", data={"csrf": token, "vendor": "proponent",
                                                  "owner": SHARED, "editors": BOB + "\n" + JANE}
                    ).status_code == 403
    assert fake.versions == {}

    admin = signed_in(monkeypatch, ADMIN)
    assert "Add a login" in admin.get("/credentials").text
    assert "Continue with Google" in admin.get("/credentials/new").text   # no Cloud access yet


def test_admin_adds_a_shared_login_with_their_own_google_access(admin_env, monkeypatch):
    google, fake = admin_env
    client, resp = granted(google, monkeypatch)
    assert resp.status_code == 303 and resp.headers["location"] == "/credentials/new"
    cookie = resp.headers["set-cookie"]
    assert "sai_gcp=" in cookie and "Path=/credentials/new" in cookie and "HttpOnly" in cookie
    assert "ya29.admin-token" not in cookie     # encrypted, not just signed

    resp = new_login(client)
    assert resp.status_code == 200 and "Created sourcing-vendor-ils" in resp.text
    secret = __import__("json").loads(fake.versions["sourcing-vendor-ils"][0])
    assert secret["username"] == "skyuser" and secret["password"] == "s3cret!"
    assert secret["updated_by"] == ADMIN and "totp_secret" not in secret

    store = api._store
    account = store.accounts[f"ils__{SHARED}"]
    assert account["editors"] == [BOB] and account["secret_key"] == "sourcing-vendor-ils"
    assert store.audit_log[-1]["event"] == "credential_created"
    for text in (resp.text, str(store.accounts), str(store.audit_log)):
        assert "s3cret!" not in text
    # The editor sees the new login; another team member doesn't.
    assert "Shared team login" in signed_in(monkeypatch, BOB).get("/credentials?vendor=ils").text
    assert signed_in(monkeypatch, JANE).get("/credentials?vendor=ils").status_code == 404


def test_monroe_is_listed_and_its_login_is_created_under_its_own_name(admin_env, monkeypatch):
    # Monroe Aerospace (2026-11-04): the add form offers it, and the name a person
    # types ("Monroe Aerospace") becomes the vendor id "monroe" -> sourcing-vendor-monroe.
    google, fake = admin_env
    client, _ = granted(google, monkeypatch)
    assert '<option value="monroe">Monroe Aerospace</option>' in client.get("/credentials/new").text
    resp = new_login(client, vendor="Monroe Aerospace")
    assert "Created sourcing-vendor-monroe" in resp.text
    assert api._store.accounts[f"monroe__{SHARED}"]["secret_key"] == "sourcing-vendor-monroe"


def test_existing_secret_is_left_alone_and_empty_one_is_filled(admin_env, monkeypatch):
    google, fake = admin_env
    fake.versions = {"sourcing-vendor-ils": [b'{"username": "x", "password": "keep"}'],
                     "sourcing-vendor-avsight": []}
    client, _ = granted(google, monkeypatch)
    resp = new_login(client)
    assert "already exists" in resp.text and len(fake.versions["sourcing-vendor-ils"]) == 1
    assert f"ils__{SHARED}" in api._store.accounts            # listed, so it can be changed
    assert "Created sourcing-vendor-avsight" in new_login(client, vendor="avsight").text
    assert len(fake.versions["sourcing-vendor-avsight"]) == 1


def test_google_decides_who_may_create_secrets(admin_env, monkeypatch):
    google, fake = admin_env
    fake.deny = True
    client, _ = granted(google, monkeypatch)
    resp = new_login(client)
    assert resp.status_code == 403 and "may not create secrets in test-proj" in resp.text
    assert f"ils__{SHARED}" not in api._store.accounts


@pytest.mark.parametrize("claims, token", [
    ({"email": JANE}, None),                                     # another Google account
    ({}, {"access_token": "t", "scope": "openid email"}),        # Cloud access unticked
])
def test_cloud_access_refused(admin_env, monkeypatch, claims, token):
    google, _ = admin_env
    if token:
        google["token"] = token
    client = signed_in(monkeypatch, ADMIN)
    resp = client.get("/auth/grant", follow_redirects=False)
    query = dict(p.split("=", 1) for p in resp.headers["location"].split("?", 1)[1].split("&"))
    saved = web_auth.unsign(client.cookies[web_auth.STATE_COOKIE], "test-session-secret")
    google["claims"] = _claims(**{"email": ADMIN, "nonce": saved["nonce"], **claims})
    resp = client.get(f"/auth/callback?code=c&state={query['state']}", follow_redirects=False)
    assert resp.status_code == 403 and "sai_gcp" not in resp.headers.get("set-cookie", "")


def test_grant_belongs_to_one_session(admin_env, monkeypatch):
    google, fake = admin_env
    first, _ = granted(google, monkeypatch)
    second = signed_in(monkeypatch, ADMIN)
    second.cookies.set(web_auth.GRANT_COOKIE, first.cookies[web_auth.GRANT_COOKIE],
                       path="/credentials/new")
    assert "Continue with Google" in second.get("/credentials/new").text
    assert fake.versions == {}


def test_new_login_form_checks(admin_env, monkeypatch):
    google, fake = admin_env
    client, _ = granted(google, monkeypatch)
    assert "not a Skylink account" in new_login(client, editors="eve@gmail.com").text
    assert "Only a shared login" in new_login(client, kind="personal",
                                               owner_email=JANE).text
    assert "not a Skylink account" in new_login(client, kind="personal", editors="",
                                                 owner_email="eve@gmail.com").text
    assert "not the same" in new_login(client, password2="other").text
    assert "https://" in new_login(client, url="http://ils.example").text
    assert new_login(client, csrf="forged").status_code == 400
    assert fake.versions == {}
    resp = new_login(client, kind="personal", owner_email=JANE, editors="")
    assert "Created sourcing-vendor-ils__jane-skylink-com" in resp.text


def test_admin_changes_who_can_change_a_shared_login(admin_env, monkeypatch):
    admin = signed_in(monkeypatch, ADMIN)
    page_html = admin.get("/credentials?vendor=proponent").text
    assert page_html.count("Who can change it") == 1             # shared row only
    form = admin.get("/credentials/editors", params={"vendor": "proponent", "owner": SHARED})
    assert BOB in form.text
    data = {"csrf": csrf(form.text), "vendor": "proponent", "owner": SHARED}
    resp = admin.post("/credentials/editors", data={**data, "editors": "x@gmail.com"})
    assert resp.status_code == 400 and "not a Skylink account" in resp.text
    resp = admin.post("/credentials/editors", data={**data, "editors": JANE})
    assert resp.status_code == 200 and "Saved" in resp.text
    assert api._store.accounts[f"proponent__{SHARED}"]["editors"] == [JANE]
    event = api._store.audit_log[-1]
    assert (event["added"], event["removed"]) == ([JANE], [BOB])
    assert "Shared team login" in signed_in(monkeypatch, JANE).get("/credentials?vendor=proponent").text
    assert "Shared team login" not in signed_in(monkeypatch, BOB).get("/credentials?vendor=proponent").text


# -- find a login made with gcloud -------------------------------------------------

def find(client, vendor="ILS", **extra):
    token = csrf(client.get("/credentials/find").text)
    return client.post("/credentials/find", data={"csrf": token, "vendor": vendor, **extra})


def test_login_made_with_gcloud_is_found_and_can_be_changed(env):
    monkeypatch, store, creds = env
    creds.secrets[secret_key("ils", SHARED)] = {"username": "team", "password": "team-pw-91"}
    creds.secrets[secret_key("ils", JANE)] = {"username": "jane", "password": "p2"}
    creds.secrets[secret_key("ils", BOB)] = {"username": "bob", "password": "p3"}
    jane = signed_in(monkeypatch, JANE)
    assert "Find it" in jane.get("/credentials").text

    # Another person's email is ignored for a team member: only shared + their own.
    resp = find(jane, owner_email=BOB)
    assert resp.status_code == 200
    assert "Your own login (sourcing-vendor-ils__jane-skylink-com): you can change it now" in resp.text
    assert "Shared team login (sourcing-vendor-ils): listed; ask an admin" in resp.text
    assert f"ils__{BOB}" not in store.accounts and "team-pw-91" not in resp.text
    assert store.audit_log[-1]["event"] == "login_found"

    form = {"vendor": "ils", "owner": JANE}
    assert jane.get("/credentials/update", params=form).status_code == 200
    token = csrf(jane.get("/credentials/update", params=form).text)
    assert jane.post("/credentials/update", data={**form, "csrf": token, "password": "new",
                                                  "password2": "new"}).status_code == 200
    assert creds.secrets[secret_key("ils", JANE)]["password"] == "new"
    assert jane.get("/credentials/update", params={"vendor": "ils",
                                                   "owner": SHARED}).status_code == 403

    admin = signed_in(monkeypatch, ADMIN)
    assert "you can change it now" in find(admin, owner_email=BOB).text
    assert f"ils__{BOB}" in store.accounts


def test_find_reports_a_missing_login(env):
    monkeypatch, store, _ = env
    resp = find(signed_in(monkeypatch, JANE), vendor="Acme Parts")
    assert resp.status_code == 404
    assert "sourcing-vendor-acme-parts, sourcing-vendor-acme-parts__jane-skylink-com" in resp.text
    assert not any(k.startswith("acme") for k in store.accounts)
    assert find(signed_in(monkeypatch, JANE), vendor="").status_code in (400, 422)
