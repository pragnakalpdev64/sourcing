"""The program's own passkey (sourcing/auth/passkey.py): created in one browser,
saved, and used to sign in from a fresh one, on a mock site (no network)."""
from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from sourcing.auth.passkey import VirtualPasskeys

SITE = "https://passkey.test/"
# What a site does: "Add a passkey" (create) and "Sign in with a passkey" (get,
# no user name: the passkey says who it is).
PAGE = """<html><body><script>
const b64 = s => Uint8Array.from(s, c => c.charCodeAt(0));
async function addPasskey() {
  const c = await navigator.credentials.create({publicKey: {
    challenge: b64('register-challenge'), rp: {id: 'passkey.test', name: 'Mock'},
    user: {id: b64('user-42'), name: 'buyer@x.com', displayName: 'Buyer'},
    pubKeyCredParams: [{type: 'public-key', alg: -7}],
    authenticatorSelection: {residentKey: 'required', userVerification: 'required'}}});
  return c.id;
}
async function signIn() {
  const a = await navigator.credentials.get({publicKey: {
    challenge: b64('login-challenge'), rpId: 'passkey.test', userVerification: 'required'}});
  return {id: a.id, user: new TextDecoder().decode(a.response.userHandle)};
}
</script></body></html>"""


async def _page(pw):
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.route("https://passkey.test/**", lambda r: r.fulfill(
        status=200, content_type="text/html", body=PAGE))
    await page.goto(SITE)
    return browser, page


@pytest.mark.asyncio
async def test_a_saved_passkey_signs_in_from_a_fresh_browser():
    pw = await async_playwright().start()
    try:
        try:
            browser, page = await _page(pw)
        except Exception as exc:
            pytest.skip(f"No local Chromium: {exc}")
        keys = await VirtualPasskeys.attach(page)
        made = await page.evaluate("addPasskey()")
        saved = await keys.credentials()
        assert keys.added == [made] and len(saved) == 1
        assert saved[0]["rpId"] == "passkey.test" and saved[0]["privateKey"]
        await browser.close()

        # Next run: a new browser knows nothing until the saved passkey is loaded.
        browser, page = await _page(pw)
        keys = await VirtualPasskeys.attach(page, saved)
        signed = await page.evaluate("signIn()")
        assert signed == {"id": made, "user": "user-42"}
        assert keys.asserted == [made]
        after = await keys.credentials()
        assert after[0]["signCount"] > saved[0]["signCount"]   # saved back after a run
        await browser.close()
    finally:
        await pw.stop()


@pytest.mark.asyncio
async def test_without_the_saved_passkey_the_request_fails():
    pw = await async_playwright().start()
    try:
        try:
            browser, page = await _page(pw)
        except Exception as exc:
            pytest.skip(f"No local Chromium: {exc}")
        await VirtualPasskeys.attach(page)        # an empty authenticator
        with pytest.raises(Exception, match="NotAllowedError|not allowed|timed out"):
            await page.evaluate("Promise.race([signIn(), new Promise((_, no) => "
                                "setTimeout(() => no(new Error('timed out')), 5000))])")
        await browser.close()
    finally:
        await pw.stop()


@pytest.mark.asyncio
async def test_passkeys_live_in_the_login_secret_and_never_in_its_repr():
    from sourcing.auth.passkey import merge
    from sourcing.credentials.base import MemoryCredentialProvider
    from sourcing.models import SHARED

    old = {"credentialId": "A", "rpId": "notion.com", "privateKey": "SECRET-KEY-A", "signCount": 1}
    creds = MemoryCredentialProvider({"sourcing-vendor-notion": {
        "username": "dev@x.com", "password": "pw", "passkeys": [old]}})
    cred = await creds.resolve("notion", "")
    assert cred.passkeys == (old,) and "SECRET-KEY" not in repr(cred)

    used = dict(old, signCount=2)                         # after a run
    new = {"credentialId": "B", "rpId": "notion.com", "privateKey": "SECRET-KEY-B", "signCount": 0}
    await creds.update("notion", SHARED, {"passkeys": merge(list(cred.passkeys), [used, new])},
                       must_exist=True)
    cred = await creds.resolve("notion", "")
    assert {k["credentialId"]: k["signCount"] for k in cred.passkeys} == {"A": 2, "B": 0}
    assert cred.password == "pw"                          # the rest of the login kept


@pytest.mark.asyncio
async def test_two_passkeys_of_one_account_load_the_newest():
    """2026-11-18: a local- and a Browserbase-made passkey for the same Notion user;
    the authenticator takes one per account, so the newest is loaded."""
    pw = await async_playwright().start()
    try:
        made = []
        for _ in range(2):
            try:
                browser, page = await _page(pw)
            except Exception as exc:
                pytest.skip(f"No local Chromium: {exc}")
            keys = await VirtualPasskeys.attach(page)
            await page.evaluate("addPasskey()")
            made += await keys.credentials()
            await browser.close()
        browser, page = await _page(pw)
        keys = await VirtualPasskeys.attach(page, made)           # no error
        signed = await page.evaluate("signIn()")
        from sourcing.auth.passkey import web_id
        assert signed["id"] == web_id(made[1]["credentialId"])
        await browser.close()
    finally:
        await pw.stop()
