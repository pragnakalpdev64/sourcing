"""The service never opens the request sheet (client, 2026-11-13): the add-on sends
a copy and writes the results itself (sourcing/sheet_relay.py, apps_script/addon)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import service.api as api
import service.job_main as job_main
from sourcing.config import get_settings
from sourcing.models import RowStatus
from sourcing.sheet_relay import SheetNotSent, open_relay_sheet
from sourcing.sheets import RFQ_TAB
from sourcing.store import WORKER_LOCK, MemoryStore

HEADER = ["Part Number", "Qty", "Condition", "Vendor", "Requested By", "Status", "Notes"]
SNAPSHOT = {
    "Requests": [HEADER,
                 ["35010-38", "2", "NE", "Proponent", "ann@skylinkintl.com", "", ""],
                 ["MS20470AD4-4", "5", "", "Proponent", "", "DONE", ""],
                 ["620-1-200", "1", "OH", "Proponent", "", "NEW", ""]],
    "RFQ": [["Vendor Reference", "PN Quoted", "Qty Quoted", "Unit Price"]],
}


def sheet(store):
    return open_relay_sheet(store, "sheet-1", "Requests", "Results")


def test_rows_are_read_from_the_copy_the_add_on_sent():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    rows = sheet(store).read_new_rows()
    assert [(r.sheet_row, r.part_number) for r in rows] == [(2, "35010-38"), (4, "620-1-200")]


def test_no_copy_yet_is_a_clear_error():
    with pytest.raises(SheetNotSent):
        sheet(MemoryStore())


def test_status_writes_wait_for_the_add_on_with_the_part_they_belong_to():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    s = sheet(store)
    rows = s.read_new_rows()
    s.set_status(rows, RowStatus.DONE, note="2 offers")
    [item] = store.sheet_ops("sheet-1")
    op = item["op"]
    assert op["kind"] == "cells" and op["tab"] == "Requests"
    assert {"range": "F2", "values": [["DONE"]]} in op["updates"]
    assert {"range": "G4", "values": [["2 offers"]]} in op["updates"]
    # The add-on checks the part is still in that row before writing (rows may move).
    # ... and that its Status is still the one the worker saw (a person's edit wins).
    assert op["guard"] == {"col": 1, "rows": {"2": "35010-38", "4": "620-1-200"},
                           "status_col": 6, "status": {"2": "", "4": "NEW"}}


def test_rfq_lines_follow_the_sheets_own_columns():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    sheet(store).append_rfq([{"PN Quoted": "35010-38", "Vendor Reference": "Proponent",
                              "Unit Price": "12.5", "Qty Quoted": "2"}])
    ops = [o["op"] for o in store.sheet_ops("sheet-1")]
    append = [o for o in ops if o["kind"] == "append"][-1]
    assert append["tab"] == RFQ_TAB
    assert append["rows"][0][:4] == ["Proponent", "35010-38", "2", "12.5"]


def test_rfq_tab_missing_is_created_by_the_add_on():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", {"Requests": SNAPSHOT["Requests"]})
    sheet(store).append_rfq([{"PN Quoted": "35010-38", "Vendor Reference": "Proponent"}])
    kinds = [o["op"]["kind"] for o in store.sheet_ops("sheet-1")]
    assert kinds[0] == "add_sheet" and "append" in kinds


def test_writes_not_yet_confirmed_count_on_the_next_read():
    # Pass 2 of a run (or a run started by the next edit) must not search rows
    # the add-on hasn't marked yet.
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    s = sheet(store)
    s.set_status(s.read_new_rows(), RowStatus.RUNNING)
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)      # the add-on's copy, sent before writing
    assert sheet(store).read_new_rows() == []
    store.ack_sheet_ops("sheet-1", [o["id"] for o in store.sheet_ops("sheet-1")])
    assert len(sheet(store).read_new_rows()) == 2       # acked but the copy is old: as sent


def test_writes_keep_their_order():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    s = sheet(store)
    rows = s.read_new_rows()
    s.set_status(rows, RowStatus.RUNNING)
    s.set_status(rows, RowStatus.DONE)
    values = [o["op"]["updates"][0]["values"][0][0] for o in store.sheet_ops("sheet-1")]
    assert values == ["RUNNING", "DONE"]


def _with_status(status_by_row: dict) -> dict:
    rows = [list(r) for r in SNAPSHOT["Requests"]]
    for r, status in status_by_row.items():
        rows[r - 1][5] = status
    return {**SNAPSHOT, "Requests": rows}


def test_a_person_resetting_a_row_wins_over_an_unconfirmed_write():
    # 2026-11-16: a run marked rows NEEDS_ATTENTION; before the add-on wrote that,
    # the person had cleared the Status to search again. The old write must not
    # put NEEDS_ATTENTION back on the new copy.
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    s = sheet(store)
    rows = s.read_new_rows()
    s.set_status(rows, RowStatus.RUNNING)
    s.set_status(rows, RowStatus.NEEDS_ATTENTION, note="site refused the browser")
    # The add-on wrote RUNNING and NEEDS_ATTENTION is still waiting; the person
    # clears row 2 and sends a new copy.
    ops = store.sheet_ops("sheet-1")
    store.ack_sheet_ops("sheet-1", [ops[0]["id"]])
    store.put_sheet_snapshot("sheet-1", _with_status({2: "", 4: "RUNNING"}))
    again = sheet(store).read_new_rows()
    assert [r.sheet_row for r in again] == [2]           # row 4 untouched: NEEDS_ATTENTION
    assert again[0].status in ("", "NEW")


def test_waiting_writes_still_apply_when_nobody_changed_the_row():
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    s = sheet(store)
    rows = s.read_new_rows()
    s.set_status(rows, RowStatus.RUNNING)
    s.set_status(rows, RowStatus.DONE)
    # Nothing confirmed yet, copy as first sent: both writes count, in order.
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    assert sheet(store).read_new_rows() == []
    # Written twice by the add-on (ack lost): the repeat is skipped, not an error.
    store.put_sheet_snapshot("sheet-1", _with_status({2: "DONE", 4: "DONE"}))
    assert sheet(store).read_new_rows() == []


# -- API ----------------------------------------------------------------------------

H = {"X-Trigger-Secret": "s3"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRIGGER_SECRET", "s3")
    monkeypatch.setenv("SHEET_ACCESS", "relay")
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.delenv("TRIGGER_RUN_LOCAL", raising=False)
    get_settings.cache_clear()
    store = MemoryStore()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_start_job", lambda: "exec-1")
    yield TestClient(api.app), store
    get_settings.cache_clear()


def test_trigger_keeps_the_copy_the_add_on_sent(client):
    c, store = client
    resp = c.post("/trigger", headers=H, json={"sheet_id": "sheet-1", "snapshot": SNAPSHOT})
    assert resp.status_code == 200
    assert store.get_sheet_snapshot("sheet-1")["tabs"] == SNAPSHOT


def test_a_malformed_copy_is_refused(client):
    c, _ = client
    resp = c.post("/trigger", headers=H, json={"sheet_id": "s", "snapshot": {"Requests": "x"}})
    assert resp.status_code == 400


def test_pull_gives_the_waiting_writes_and_ack_removes_them(client):
    c, store = client
    store.add_sheet_ops("sheet-1", [{"kind": "append", "tab": "RFQ", "rows": [["a"]]},
                                    {"kind": "add_sheet", "tab": "X"}])
    store.add_sheet_ops("other", [{"kind": "add_sheet", "tab": "Y"}])
    body = c.post("/sheet/pull", headers=H, json={"sheet_id": "sheet-1"}).json()
    assert [o["kind"] for o in body["ops"]] == ["append", "add_sheet"]
    assert body["ops"][0]["rows"] == [["a"]] and body["busy"] is False
    ids = [o["id"] for o in body["ops"]][:1]
    assert c.post("/sheet/ack", headers=H, json={"sheet_id": "sheet-1", "ids": ids}).json() == {"acked": 1}
    left = c.post("/sheet/pull", headers=H, json={"sheet_id": "sheet-1"}).json()["ops"]
    assert [o["kind"] for o in left] == ["add_sheet"]
    assert len(store.sheet_ops("other")) == 1


def test_pull_says_busy_while_a_search_runs(client):
    c, store = client
    store.acquire_lock(WORKER_LOCK, "run-1", 600)
    assert c.post("/sheet/pull", headers=H, json={"sheet_id": "s"}).json()["busy"] is True


def test_pull_needs_a_caller_and_a_sheet(client):
    c, _ = client
    assert c.post("/sheet/pull", json={"sheet_id": "s"}).status_code == 401
    assert c.post("/sheet/pull", headers=H, json={}).status_code == 400


def test_relay_refuses_a_file_store(client, monkeypatch):
    c, _ = client
    monkeypatch.setenv("STORE_BACKEND", "file")
    get_settings.cache_clear()
    resp = c.post("/sheet/pull", headers=H, json={"sheet_id": "s"})
    assert resp.status_code == 400 and "firestore" in resp.json()["detail"]


# -- worker -------------------------------------------------------------------------

def test_worker_uses_the_add_on_copy_in_relay_mode(monkeypatch):
    monkeypatch.setenv("SHEET_ACCESS", "relay")
    get_settings.cache_clear()
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", SNAPSHOT)
    seen = {}

    class FakePipeline:
        def __init__(self, deps, run_id):
            self.deps = deps

        async def run(self):
            rows = self.deps.sheets.read_new_rows()
            self.deps.sheets.set_status(rows, RowStatus.DONE)
            seen["rows"] = [r.part_number for r in rows]
            return {"rows_total": len(rows)}

    def no_sheets_api(*a, **k):
        raise AssertionError("the worker opened the sheet itself")

    monkeypatch.setattr(job_main, "Pipeline", FakePipeline)
    monkeypatch.setattr(job_main, "open_sheet", no_sheets_api)
    shared = job_main.Deps(sheets=None, store=store, creds=None, browser=None, sink=None,
                           notifier=None, adapter_factory=None)
    result = asyncio.run(job_main.run_one_sheet(shared, {"sheet_id": "sheet-1"}, "run-1"))
    get_settings.cache_clear()
    assert result["rows_total"] == 2 and seen["rows"] == ["35010-38", "620-1-200"]
    assert store.sheet_ops("sheet-1")[-1]["op"]["updates"][0]["values"] == [["DONE"]]
