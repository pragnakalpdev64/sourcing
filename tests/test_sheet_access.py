"""Which sheet a caller may name (service/sheet_access.py): the id's format always,
and with SHEET_ACCESS_CHECK on, that the caller's own Google token can open it."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import service.api as api
import service.job_main as job_main
import service.sheet_access as sheet_access
from sourcing.config import Settings, get_settings
from sourcing.store import MemoryStore

H = {"X-Trigger-Secret": "s3"}
ADDON = "123-abc.apps.googleusercontent.com"
BEARER = {"Authorization": "Bearer ya29.ann"}
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-abcd"
SNAPSHOT = {"Requests": [["Part Number", "Vendor", "Status"], ["P1", "Proponent", ""]]}


@pytest.mark.parametrize("bad", [123, None, "", "x.xlsx", "../app/x.xlsx", "a/b", "a b",
                                 "s" * 129, ["s"]])
def test_only_google_sheet_ids_pass(bad):
    with pytest.raises(HTTPException) as exc:
        sheet_access.checked_sheet_id(bad)
    assert exc.value.status_code == 400


def test_a_google_sheet_id_passes():
    assert sheet_access.checked_sheet_id(SHEET) == SHEET


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRIGGER_SECRET", "s3")
    monkeypatch.setenv("SHEET_ACCESS", "relay")
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("ADDON_OAUTH_CLIENT_IDS", ADDON)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "skylinkintl.com")
    monkeypatch.delenv("TRIGGER_RUN_LOCAL", raising=False)
    monkeypatch.delenv("SHEET_ACCESS_CHECK", raising=False)
    get_settings.cache_clear()
    sheet_access.clear_cache()
    store = MemoryStore()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_start_job", lambda: "exec-1")

    async def token_info(token):
        return {"azp": ADDON, "email": "ann@skylinkintl.com", "email_verified": "true"}

    monkeypatch.setattr(api, "_token_info", token_info)
    yield TestClient(api.app), store, monkeypatch
    get_settings.cache_clear()
    sheet_access.clear_cache()


@pytest.mark.parametrize("path", ["/trigger", "/sheet/pull", "/sheet/ack"])
@pytest.mark.parametrize("bad", [123, "x.xlsx", "../x", {"a": 1}])
def test_a_bad_sheet_id_is_refused_before_anything_is_saved(client, path, bad):
    c, store, _ = client
    resp = c.post(path, headers=H, json={"sheet_id": bad, "snapshot": SNAPSHOT})
    assert resp.status_code == 400
    assert store.list_enabled_sheets(enabled_only=False) == []


def test_the_check_is_off_by_default(client):
    # Off: everything works as before (the Sheets API isn't on in the add-on's project yet).
    c, _, _ = client
    assert get_settings().sheet_access_check is False
    assert c.post("/sheet/pull", headers=H, json={"sheet_id": SHEET}).status_code == 200
    assert c.post("/trigger", headers=H, json={"sheet_id": SHEET}).json()["started"] is True


def _google(monkeypatch, answers: dict, asked: list):
    """Fake Google: answers[(token, sheet)] = True / False / an HTTPException."""
    async def says(token, sheet_id):
        asked.append((token, sheet_id))
        answer = answers[(token, sheet_id)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(sheet_access, "_google_says", says)


def test_with_the_check_on_a_person_who_can_open_the_sheet_gets_through(client):
    c, store, mp = client
    mp.setenv("SHEET_ACCESS_CHECK", "1")
    get_settings.cache_clear()
    asked = []
    _google(mp, {("ya29.ann", SHEET): True}, asked)
    assert c.post("/trigger", headers=BEARER,
                  json={"sheet_id": SHEET, "snapshot": SNAPSHOT}).json()["started"] is True
    assert c.post("/sheet/pull", headers=BEARER, json={"sheet_id": SHEET}).status_code == 200
    assert c.post("/sheet/ack", headers=BEARER, json={"sheet_id": SHEET, "ids": []}).status_code == 200
    assert asked == [("ya29.ann", SHEET)]          # Google asked once, then remembered


def test_with_the_check_on_someone_elses_sheet_is_refused_everywhere(client):
    c, store, mp = client
    mp.setenv("SHEET_ACCESS_CHECK", "1")
    get_settings.cache_clear()
    _google(mp, {("ya29.ann", SHEET): False}, [])
    resp = c.post("/trigger", headers=BEARER, json={"sheet_id": SHEET, "snapshot": SNAPSHOT})
    assert resp.status_code == 403
    assert store.get_sheet_snapshot(SHEET) is None              # nothing saved
    assert store.list_enabled_sheets(enabled_only=False) == []  # not registered either
    assert c.post("/sheet/pull", headers=BEARER, json={"sheet_id": SHEET}).status_code == 403
    assert c.post("/sheet/ack", headers=BEARER,
                  json={"sheet_id": SHEET, "ids": ["x"]}).status_code == 403


def test_with_the_check_on_the_trigger_secret_may_not_name_a_sheet(client):
    # Cloud Scheduler's sweep sends no sheet; a secret caller naming one is refused.
    c, _, mp = client
    mp.setenv("SHEET_ACCESS_CHECK", "1")
    get_settings.cache_clear()
    _google(mp, {}, [])
    assert c.post("/trigger", headers=H, json={"sheet_id": SHEET}).status_code == 403
    assert c.post("/sheet/pull", headers=H, json={"sheet_id": SHEET}).status_code == 403
    assert c.post("/trigger", headers=H).json()["started"] is True     # the sweep still runs


def test_google_not_answering_is_never_let_through(client):
    c, _, mp = client
    mp.setenv("SHEET_ACCESS_CHECK", "1")
    get_settings.cache_clear()
    _google(mp, {("ya29.ann", SHEET): HTTPException(503, "could not ask Google")}, [])
    assert c.post("/sheet/pull", headers=BEARER, json={"sheet_id": SHEET}).status_code == 503


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


@pytest.mark.asyncio
@pytest.mark.parametrize("status, text, expected", [
    (200, "", True), (404, "", False), (403, "PERMISSION_DENIED", False),
    (403, '{"reason": "SERVICE_DISABLED"}', 503), (500, "", 503),
])
async def test_googles_answers(monkeypatch, status, text, expected):
    seen = {}

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, headers=None):
            seen.update(url=url, auth=headers["Authorization"])
            return _Resp(status, text)

    monkeypatch.setattr(sheet_access.httpx, "AsyncClient", FakeClient)
    if expected == 503:
        with pytest.raises(HTTPException) as exc:
            await sheet_access._google_says("tok", SHEET)
        assert exc.value.status_code == 503
    else:
        assert await sheet_access._google_says("tok", SHEET) is expected
    assert seen == {"url": f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}",
                    "auth": "Bearer tok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["CLOUD_RUN_JOB", "K_SERVICE"])
async def test_the_worker_refuses_a_local_file_on_cloud_run(monkeypatch, env):
    monkeypatch.setenv(env, "sourcing-worker")
    result = await job_main.run_one_sheet(None, {"sheet_id": "/app/x.xlsx"}, "run-1")
    assert "refused on Cloud Run" in result["error"]


def test_keys_are_not_in_the_printed_settings(monkeypatch):
    for name, value in {"ANTHROPIC_API_KEY": "KEY-A", "BROWSERBASE_API_KEY": "KEY-B",
                        "CHAT_WEBHOOK_URL": "https://chat/KEY-C", "TRIGGER_SECRET": "KEY-D"}.items():
        monkeypatch.setenv(name, value)
    text = repr(Settings.from_env())
    assert "KEY-" not in text and "anthropic_api_key" not in text
    assert Settings.from_env().anthropic_api_key == "KEY-A"
