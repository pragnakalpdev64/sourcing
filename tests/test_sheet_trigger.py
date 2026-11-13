"""The sheet's Apps Script calls /trigger when rows are added (apps_script/).
Rows added while a worker runs are read by that worker before it ends; local
mode (TRIGGER_RUN_LOCAL, for testing through ngrok) runs the worker here."""
from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

import service.api as api
import service.job_main as job_main
from sourcing.config import get_settings
from sourcing.store import RERUN_FLAG, RERUN_HOLDER, TRIGGER_LOCK, WORKER_LOCK, MemoryStore

H = {"X-Trigger-Secret": "s3"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRIGGER_SECRET", "s3")
    monkeypatch.delenv("TRIGGER_RUN_LOCAL", raising=False)
    get_settings.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(api, "_store", store)
    started = []
    monkeypatch.setattr(api, "_start_job", lambda: started.append(1) or "exec-1")
    yield TestClient(api.app), store, started
    get_settings.cache_clear()


def test_rows_added_while_the_worker_runs_ask_it_to_read_again(client):
    c, store, started = client
    store.acquire_lock(WORKER_LOCK, "run-1", 600)
    resp = c.post("/trigger", headers=H, json={"sheet_id": "s1"}).json()
    assert resp["started"] is False and resp["rerun"] is True
    assert store.is_locked(RERUN_FLAG) and started == []


def test_a_call_seconds_after_a_start_asks_for_a_second_read(client):
    c, store, started = client
    assert c.post("/trigger", headers=H).json()["started"] is True
    assert not store.is_locked(RERUN_FLAG)
    resp = c.post("/trigger", headers=H).json()
    assert resp["rerun"] is True and store.is_locked(RERUN_FLAG) and started == [1]


# -- worker passes -------------------------------------------------------------

@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "memory")
    get_settings.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(job_main, "make_store", lambda *a, **k: store)
    passes = []
    yield store, passes, monkeypatch
    get_settings.cache_clear()


def test_worker_reads_the_sheet_again_when_rows_came_in(worker):
    store, passes, mp = worker

    async def one_pass(settings, st, run_id, *a):
        passes.append(run_id)
        assert st.is_locked(WORKER_LOCK)
        if len(passes) == 1:   # /trigger during the first pass
            st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, 600)

    mp.setattr(job_main, "_one_pass", one_pass)
    assert asyncio.run(job_main.run(dummy=True)) == 0
    assert len(passes) == 2 and not store.is_locked(RERUN_FLAG)
    assert not store.is_locked(WORKER_LOCK)


def test_a_request_from_before_the_run_is_covered_by_it(worker):
    store, passes, mp = worker
    store.acquire_lock(RERUN_FLAG, RERUN_HOLDER, 600)

    async def one_pass(*a):
        passes.append(1)

    mp.setattr(job_main, "_one_pass", one_pass)
    asyncio.run(job_main.run(dummy=True))
    assert passes == [1]


def test_rows_that_keep_coming_stop_after_the_last_pass(worker):
    store, passes, mp = worker

    async def one_pass(settings, st, *a):
        passes.append(1)
        st.acquire_lock(RERUN_FLAG, RERUN_HOLDER, 600)

    mp.setattr(job_main, "_one_pass", one_pass)
    asyncio.run(job_main.run(dummy=True))
    assert len(passes) == job_main.MAX_PASSES


def test_a_failed_pass_still_frees_the_lock(worker):
    store, _, mp = worker

    async def one_pass(*a):
        raise RuntimeError("sheet down")

    mp.setattr(job_main, "_one_pass", one_pass)
    with pytest.raises(RuntimeError):
        asyncio.run(job_main.run(dummy=True))
    assert not store.is_locked(WORKER_LOCK)


# -- local mode (ngrok testing) ------------------------------------------------

class FakeProc:
    count = 0

    def __init__(self, cmd, cwd=None):
        FakeProc.count += 1
        self.pid = 1000 + FakeProc.count
        self.cmd = cmd
        self.done = threading.Event()
        self.code = None

    def poll(self):
        return self.code

    def wait(self):
        self.done.wait(5)
        return self.code

    def finish(self):
        self.code = 0
        self.done.set()


@pytest.fixture
def local(monkeypatch, client):
    c, store, started = client
    monkeypatch.setenv("TRIGGER_RUN_LOCAL", "1")
    monkeypatch.setenv("TRIGGER_LOCAL_ARGS", "--dummy")
    get_settings.cache_clear()
    procs = []

    def popen(cmd, cwd=None):
        procs.append(FakeProc(cmd, cwd))
        return procs[-1]

    monkeypatch.setattr(api.subprocess, "Popen", popen)
    monkeypatch.setattr(api, "_local", {"proc": None, "again": False, "sheet_id": None,
                                      "start_at": 0.0, "waiting": False})
    yield c, store, started, procs
    for p in procs:
        p.finish()


def test_local_mode_runs_the_worker_here_on_the_sheet_that_called(local):
    c, store, started, procs = local
    resp = c.post("/trigger", headers=H, json={"sheet_id": "sheetA"}).json()
    assert resp["started"] is True and resp["registered"] is False
    assert started == []                       # not the Cloud Run job
    assert procs[0].cmd[1:] == ["-m", "service.job_main", "--dummy", "--sheet", "sheetA"]
    assert store.list_enabled_sheets() == []   # the worker's file store isn't touched


def test_local_mode_runs_again_once_when_rows_came_in(local):
    c, _, _, procs = local
    c.post("/trigger", headers=H, json={"sheet_id": "sheetA"})
    for _ in range(3):
        assert c.post("/trigger", headers=H, json={"sheet_id": "sheetA"}).json()["rerun"] is True
    assert len(procs) == 1
    procs[0].finish()
    for _ in range(50):
        if len(procs) == 2:
            break
        threading.Event().wait(0.05)
    assert len(procs) == 2 and procs[1].cmd[-1] == "sheetA"
    procs[1].finish()
    threading.Event().wait(0.2)
    assert len(procs) == 2                      # nothing new came in: no third run


def test_local_mode_still_needs_the_secret(local):
    c, _, _, procs = local
    assert c.post("/trigger", headers={"X-Trigger-Secret": "x"}).status_code == 401
    assert procs == []


# -- the Sheets add-on (apps_script/addon): user's token, wait on our side ------

ADDON = "123-abc.apps.googleusercontent.com"
BEARER = {"Authorization": "Bearer ya29.token"}


@pytest.fixture
def addon(monkeypatch, client):
    c, store, started = client
    monkeypatch.setenv("ADDON_OAUTH_CLIENT_IDS", ADDON)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "skylink.com")
    get_settings.cache_clear()
    info = {"azp": ADDON, "aud": ADDON, "email": "Ann@skylink.com", "email_verified": "true"}

    async def token_info(token):
        assert token == "ya29.token"
        return info

    monkeypatch.setattr(api, "_token_info", token_info)
    yield c, store, started, info


def test_addon_token_starts_a_run_and_notes_the_edit(addon):
    c, store, started, _ = addon
    resp = c.post("/trigger", headers=BEARER,
                  json={"sheet_id": "s1", "label": "Ann's parts", "edited": True}).json()
    assert resp["started"] is True and started == [1]
    sheet = store.list_enabled_sheets()[0]
    assert sheet["sheet_id"] == "s1" and sheet["last_called_by"] == "ann@skylink.com"
    assert sheet["last_edit_at"] > 0


def test_addon_search_now_clears_the_wait_for_its_sheet(addon):
    c, store, _, _ = addon
    c.post("/trigger", headers=BEARER, json={"sheet_id": "s1", "edited": True})
    c.post("/trigger", headers=BEARER, json={"sheet_id": "s1", "edited": False})
    assert store.list_enabled_sheets()[0]["last_edit_at"] == 0


def test_older_script_leaves_the_edit_time_alone(addon):
    c, store, _, _ = addon
    c.post("/trigger", headers=H, json={"sheet_id": "s1"})
    assert "last_edit_at" not in store.list_enabled_sheets()[0]


@pytest.mark.parametrize("change, why", [
    ({"azp": "other.apps.googleusercontent.com", "aud": "other"}, "not from the SourcingAI add-on"),
    ({"email": "ann@gmail.com"}, "not a Skylink Google account"),
    ({"email_verified": "false"}, "did not confirm"),
])
def test_addon_token_refused(addon, change, why):
    c, store, started, info = addon
    info.update(change)
    resp = c.post("/trigger", headers=BEARER, json={"sheet_id": "s1"})
    assert resp.status_code == 401 and why in resp.json()["detail"]
    assert started == [] and store.list_enabled_sheets() == []


def test_addon_token_refused_when_google_rejects_it(addon, monkeypatch):
    c, _, started, _ = addon

    async def rejected(token):
        return None

    monkeypatch.setattr(api, "_token_info", rejected)
    assert c.post("/trigger", headers=BEARER).status_code == 401 and started == []


def test_addon_token_refused_until_the_client_id_is_set(addon, monkeypatch):
    c, _, started, _ = addon
    monkeypatch.delenv("ADDON_OAUTH_CLIENT_IDS")
    get_settings.cache_clear()
    resp = c.post("/trigger", headers=BEARER)
    assert resp.status_code == 401 and "ADDON_OAUTH_CLIENT_IDS" in resp.json()["detail"]


def test_no_secret_and_no_token_is_refused(client):
    c, _, started = client
    assert c.post("/trigger").status_code == 401 and started == []


def test_worker_waits_until_the_sheets_are_quiet():
    store = MemoryStore()
    store.register_sheet("s1", {"last_edit_at": 100.0})
    store.register_sheet("s2", {"last_edit_at": 0})
    now = [150.0]
    naps = []

    async def sleep(s):
        naps.append(s)
        now[0] += s
        if now[0] >= 160 and not store.list_enabled_sheets()[0].get("edited_again"):
            store.register_sheet("s1", {"last_edit_at": 160.0, "edited_again": True})

    waited = asyncio.run(job_main.wait_until_quiet(store, 90, clock=lambda: now[0], sleep=sleep))
    assert now[0] >= 250 and waited == now[0] - 150   # 90 s after the edit at 160
    assert all(n <= 5 for n in naps)


def test_worker_does_not_wait_for_ever():
    store = MemoryStore()
    now = [1000.0]

    async def sleep(s):
        now[0] += s
        store.register_sheet("s1", {"last_edit_at": now[0]})   # edited non-stop

    store.register_sheet("s1", {"last_edit_at": now[0]})
    waited = asyncio.run(job_main.wait_until_quiet(store, 90, clock=lambda: now[0], sleep=sleep))
    assert waited >= job_main.MAX_QUIET_WAIT_SECONDS and waited < job_main.MAX_QUIET_WAIT_SECONDS + 10


def test_cloud_run_job_waits_before_reading_and_a_quiet_one_does_not(worker):
    store, passes, mp = worker
    waits = []

    async def wait(st, quiet):
        waits.append(quiet)
        assert st.is_locked(WORKER_LOCK)
        return 0

    async def one_pass(*a):
        passes.append(1)

    mp.setattr(job_main, "wait_until_quiet", wait)
    mp.setattr(job_main, "_one_pass", one_pass)
    asyncio.run(job_main.run(dummy=True, all_sheets=True))
    asyncio.run(job_main.run(dummy=True))          # local --sheet run: no wait
    assert waits == [90] and passes == [1, 1]


def test_local_mode_waits_for_a_quiet_sheet(local, monkeypatch):
    c, _, _, procs = local
    monkeypatch.setenv("TRIGGER_QUIET_SECONDS", "1")
    get_settings.cache_clear()
    for _ in range(3):   # three edits: one run, after the last one
        resp = c.post("/trigger", headers=H, json={"sheet_id": "sheetA", "edited": True}).json()
        assert resp["waiting"] is True
    assert procs == []
    for _ in range(60):
        if procs:
            break
        threading.Event().wait(0.05)
    assert len(procs) == 1 and procs[0].cmd[-1] == "sheetA"
