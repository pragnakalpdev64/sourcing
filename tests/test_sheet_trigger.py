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
    monkeypatch.setattr(api, "_local", {"proc": None, "again": False, "sheet_id": None})
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
