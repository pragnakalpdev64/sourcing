"""The worker's heartbeat for the add-on's sidebar (sourcing/progress.py)."""
from __future__ import annotations

import asyncio

import pytest

import service.job_main as job_main
import sourcing.progress as progress_mod
from sourcing.config import get_settings
from sourcing.models import RowStatus
from sourcing.progress import STALE_SECONDS, SheetProgress, describe
from sourcing.store import MemoryStore

from .test_pipeline import JANE, make, run


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_steps_rows_and_finish_are_saved():
    store, clock = MemoryStore(), Clock()
    p = SheetProgress(store, "s", "run-1", clock=clock)
    p.start()
    p.rows_picked(3)
    p.step("aar___shared", "AAR: signing in")
    p.step("proponent___shared", "Proponent: searching, 2 part(s) to go")
    p.rows_finished(["r2", "r3"])
    p.rows_finished(["r2"])                    # counted once
    p.step("aar___shared", None)
    doc = store.get_sheet_progress("s")
    assert doc["steps"] == ["Proponent: searching, 2 part(s) to go"]
    assert (doc["rows_done"], doc["rows_total"], doc["state"]) == (2, 3, "running")
    clock.now += 60
    p.finish("done", {"DONE": 3})
    shown = describe(store.get_sheet_progress("s"), clock.now + 5)
    assert shown["state"] == "done" and shown["elapsed_seconds"] == 60
    assert shown["counts"] == {"DONE": 3} and shown["finished_seconds_ago"] == 5


def test_a_silent_worker_shows_as_stopped():
    doc = {"state": "running", "started_at": 0, "alive_at": 100}
    assert describe(doc, 100 + STALE_SECONDS - 1)["state"] == "running"
    assert describe(doc, 100 + STALE_SECONDS + 1)["state"] == "stopped"
    assert describe(None, 0) is None


def test_a_store_error_never_stops_the_search():
    class Broken(MemoryStore):
        def set_sheet_progress(self, sheet_id, fields):
            raise RuntimeError("firestore down")

    p = SheetProgress(Broken(), "s", "run-1")
    p.start()
    p.step("a", "x")
    p.finish("done")


def test_the_heartbeat_keeps_beating_during_a_long_step(monkeypatch):
    monkeypatch.setattr(progress_mod, "HEARTBEAT_SECONDS", 0.01)
    store = MemoryStore()
    beats = []
    real = store.set_sheet_progress
    store.set_sheet_progress = lambda sid, f: (beats.append(f), real(sid, f))

    async def long_step():
        with SheetProgress(store, "s", "run-1"):
            await asyncio.sleep(0.08)

    asyncio.run(long_step())
    assert sum(1 for f in beats if set(f) == {"alive_at"}) >= 3


@pytest.mark.asyncio
async def test_the_pipeline_reports_its_steps_and_finished_rows():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE},
                                {"vendor": "Proponent", "part": "P2", "by": JANE}])
    seen = []

    class Spy(SheetProgress):
        def _save(self, fields):
            seen.append(dict(fields))
            super()._save(fields)

    deps.progress = Spy(deps.store, "test-sheet", "r1")
    await run(deps)
    steps = [f["steps"][0] for f in seen if f.get("steps")]
    assert any("signing in" in s for s in steps) and any("searching" in s for s in steps)
    assert any("signing out" in s for s in steps)
    doc = deps.store.get_sheet_progress("test-sheet")
    assert (doc["rows_done"], doc["rows_total"]) == (2, 2)
    assert doc["steps"] == ["Working"]          # every login's part is over


def test_the_worker_saves_progress_for_a_relay_sheet(monkeypatch):
    monkeypatch.setenv("SHEET_ACCESS", "relay")
    get_settings.cache_clear()
    store = MemoryStore()
    store.put_sheet_snapshot("sheet-1", {"Requests": [["Part Number", "Vendor", "Status"],
                                                     ["P1", "Proponent", ""]]})

    class FakePipeline:
        def __init__(self, deps, run_id):
            self.deps = deps

        async def run(self):
            assert store.get_sheet_progress("sheet-1")["state"] == "running"
            rows = self.deps.sheets.read_new_rows()
            self.deps.progress.rows_picked(len(rows))
            self.deps.sheets.set_status(rows, RowStatus.DONE)
            return {"rows_total": len(rows), "counts": {"DONE": 1}}

    monkeypatch.setattr(job_main, "Pipeline", FakePipeline)
    shared = job_main.Deps(sheets=None, store=store, creds=None, browser=None, sink=None,
                           notifier=None, adapter_factory=None)
    asyncio.run(job_main.run_one_sheet(shared, {"sheet_id": "sheet-1"}, "run-1"))
    get_settings.cache_clear()
    doc = store.get_sheet_progress("sheet-1")
    assert doc["state"] == "done" and doc["counts"] == {"DONE": 1} and doc["rows_total"] == 1
