"""BROWSER_BACKEND: picking the client, the local Chromium client, and switching
between backends. Local-browser tests are skipped if no Chromium is installed."""
import pytest

import service.job_main as job_main
from sourcing.browser.base import ContextNotFound, LiveViewUnavailable
from sourcing.browser.browserbase import BrowserbaseClient
from sourcing.browser.local import LOCAL_CONTEXT_PREFIX, LocalBrowserClient
from sourcing.vendors.proponent import ProponentAdapter
from tests.test_proponent_adapter import FakeExtractor, MockProcart, cred, row


def settings(**kw):
    base = job_main.get_settings()
    return job_main.replace(base, browserbase_api_key="bb-key", browserbase_project_id="bb-proj",
                            **kw)


def test_backend_is_picked_from_settings():
    assert isinstance(job_main.build_browser(settings(browser_backend="local")), LocalBrowserClient)
    assert isinstance(job_main.build_browser(settings(browser_backend="browserbase")),
                      BrowserbaseClient)
    with pytest.raises(ValueError, match="BROWSER_BACKEND"):
        job_main.build_browser(settings(browser_backend="chrome"))


def test_browser_flags_set_env():
    parser = job_main.build_parser()
    env = job_main.env_overrides(parser.parse_args(["--browser", "local", "--headed"]))
    assert env == {"BROWSER_BACKEND": "local", "LOCAL_BROWSER_HEADLESS": "0"}


@pytest.mark.asyncio
async def test_browserbase_refuses_a_local_context_label_without_calling_the_api():
    client = BrowserbaseClient("bb-key", "bb-proj")

    def no_api(*a, **k):
        raise AssertionError("Browserbase API should not be called")

    client._create_session = no_api
    with pytest.raises(ContextNotFound):
        await client.open(f"{LOCAL_CONTEXT_PREFIX}abc")


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor, config, stealth", [
    ("aar", {"browser_stealth": True}, True),   # off by default since 2026-11-04 (402)
    ("aar", {}, False), ("proponent", {}, False)])
async def test_stealth_sessions_only_for_vendors_that_ask(vendor, config, stealth):
    # AAR, testing 2026-11-03: proxy only (advanced stealth is Enterprise-only); CAPTCHA solving stays off.
    from sourcing.vendors import adapter_class
    client = BrowserbaseClient("bb-key", "bb-proj")
    calls = []
    client._bb.sessions.create = lambda **kw: calls.append(kw) or (_ for _ in ()).throw(
        RuntimeError("stop after create"))
    adapter = adapter_class(vendor)(vendor_config=config)
    with pytest.raises(RuntimeError, match="stop after create"):
        await client.open("ctx-1", **adapter.browser_options)
    settings = calls[0]["browser_settings"]
    assert settings["solve_captchas"] is False
    assert "advanced_stealth" not in settings
    assert calls[0].get("proxies", False) is stealth
    assert adapter_class(vendor)(vendor_config={"browser_stealth": False}).browser_options == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("env, solve", [("", False), ("1", True)])
async def test_captcha_solving_only_with_the_dev_switch(monkeypatch, env, solve):
    # REMOVE-AFTER-CAPTCHA-TRIAL: rule paused for development 2026-11-05.
    monkeypatch.setenv("DEV_ALLOW_CAPTCHA_SOLVING", env)
    client = BrowserbaseClient("bb-key", "bb-proj")
    calls = []
    client._bb.sessions.create = lambda **kw: calls.append(kw) or (_ for _ in ()).throw(
        RuntimeError("stop after create"))
    with pytest.raises(RuntimeError, match="stop after create"):
        await client.open("ctx-1")
    assert calls[0]["browser_settings"]["solve_captchas"] is solve


async def open_local_or_skip(client: LocalBrowserClient):
    try:
        return await client.open(await client.create_context())
    except Exception as exc:
        pytest.skip(f"No local Chromium: {exc}")


@pytest.mark.asyncio
async def test_local_client_opens_renders_and_closes():
    client = LocalBrowserClient(headless=True)
    assert (await client.create_context()).startswith(LOCAL_CONTEXT_PREFIX)
    handle = await open_local_or_skip(client)
    try:
        await handle.page.set_content("<p>hello</p>")
        assert await handle.page.inner_text("p") == "hello"
        with pytest.raises(LiveViewUnavailable):
            await client.live_view_url(handle.session_id)
    finally:
        await client.close(handle)
    assert not handle.browser.is_connected()


@pytest.mark.asyncio
async def test_local_live_view_links_to_devtools_on_this_machine():
    import httpx

    client = LocalBrowserClient(headless=True, live_view=True)
    handle = await open_local_or_skip(client)
    try:
        await handle.page.set_content("<p>hello</p>")
        url = await client.live_view_url(handle.session_id)
        assert url.startswith("http://127.0.0.1:") and "/devtools/inspector.html?ws=" in url
        async with httpx.AsyncClient(timeout=5) as http:
            assert (await http.get(url.split("?")[0])).status_code == 200
    finally:
        await client.close(handle)
    with pytest.raises(LiveViewUnavailable):          # session gone
        await client.live_view_url(handle.session_id)


@pytest.mark.asyncio
async def test_proponent_login_and_search_through_the_local_client():
    client = LocalBrowserClient(headless=True)
    handle = await open_local_or_skip(client)
    try:
        mock = MockProcart()
        await handle.page.route("https://procart.proponent.com/**", mock.handle)
        adapter = ProponentAdapter(extractor=FakeExtractor())
        await adapter.login(handle.page, cred())
        assert await adapter.is_authenticated(handle.page)
        outcome = await adapter.search(handle.page, row("35010-38"))
        assert outcome.found and outcome.offers[0].price == "42.50"
    finally:
        await client.close(handle)
