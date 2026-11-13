from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import service.api as api
from sourcing.config import get_settings
from sourcing.store import WORKER_LOCK, MemoryStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRIGGER_SECRET", "s3")
    get_settings.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(api, "_store", store)
    started = []
    monkeypatch.setattr(api, "_start_job", lambda: started.append(1) or "exec-1")
    yield TestClient(api.app), store, started
    get_settings.cache_clear()


def test_trigger_requires_secret(client):
    c, _, started = client
    assert c.post("/trigger").status_code == 401
    assert c.post("/trigger", headers={"X-Trigger-Secret": "nope"}).status_code == 401
    assert started == []


def test_trigger_starts_once_then_debounces(client):
    c, _, started = client
    h = {"X-Trigger-Secret": "s3"}
    assert c.post("/trigger", headers=h).json() == {"started": True, "registered": False, "execution": "exec-1"}
    assert c.post("/trigger", headers=h).json()["started"] is False
    assert started == [1]


def test_trigger_with_sheet_id_registers_it(client):
    c, store, _ = client
    h = {"X-Trigger-Secret": "s3"}
    resp = c.post("/trigger", headers=h, json={"sheet_id": "sheet1", "label": "Team A"})
    assert resp.json() == {"started": True, "registered": True, "execution": "exec-1"}
    sheets = store.list_enabled_sheets()
    assert len(sheets) == 1 and sheets[0]["sheet_id"] == "sheet1" and sheets[0]["label"] == "Team A"

    # A later call from the same sheet still registers (counts as "registered"
    # even though it's already known) but doesn't duplicate the entry.
    c.post("/trigger", headers=h, json={"sheet_id": "sheet1", "label": "Team A (renamed)"})
    sheets = store.list_enabled_sheets()
    assert len(sheets) == 1 and sheets[0]["label"] == "Team A (renamed)"


def test_trigger_skips_while_worker_runs(client):
    c, store, started = client
    store.acquire_lock(WORKER_LOCK, "run-1", 600)
    assert c.post("/trigger", headers={"X-Trigger-Secret": "s3"}).json()["reason"] == "worker already running"
    assert started == []


def test_unbuilt_endpoints_say_so(client):
    c, _, _ = client
    assert c.get("/hitl/view").status_code == 501
    # Step 6 is built: signed-out visitors get the Google sign-in page.
    resp = c.get("/credentials/update", follow_redirects=False)
    assert resp.status_code == 401 and "Sign in with Google" in resp.text
    assert c.get("/healthz").json() == {"ok": True}


def test_trigger_says_why_when_the_job_cannot_start(client, monkeypatch):
    # Seen 2026-11-12: a local API without TRIGGER_RUN_LOCAL, job not deployed (403).
    from google.api_core import exceptions as gexc
    c, _, _ = client

    def refused():
        raise gexc.PermissionDenied("Permission 'run.jobs.run' denied")
    monkeypatch.setattr(api, "_start_job", refused)
    resp = c.post("/trigger", headers={"X-Trigger-Secret": "s3"})
    assert resp.status_code == 503 and "TRIGGER_RUN_LOCAL=1" in resp.json()["detail"]
    # Not debounced: the next call tries again.
    monkeypatch.setattr(api, "_start_job", lambda: "exec-2")
    assert c.post("/trigger", headers={"X-Trigger-Secret": "s3"}).json()["started"] is True
