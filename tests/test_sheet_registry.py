"""Multi-sheet registry: a sheet joins just by sending its ID once, and the
worker processes every registered sheet independently."""
from __future__ import annotations

import asyncio

import pytest

import service.job_main as job_main
from sourcing.credentials.base import MemoryCredentialProvider, secret_key
from sourcing.pipeline import Deps
from sourcing.store import MemoryStore

from .fakes import FakeAdapter, FakeBrowser, FakeNotifier, FakeSheet, FakeSink, Portal

JANE, BOB = "jane@co.com", "bob@co.com"


def test_registering_a_sheet_twice_keeps_the_first_registered_at():
    store = MemoryStore()
    store.register_sheet("s1", {"label": "Team A"})
    first = store.list_enabled_sheets()[0]["registered_at"]
    store.register_sheet("s1", {"label": "Team A (renamed)"})
    sheets = store.list_enabled_sheets()
    assert len(sheets) == 1
    assert sheets[0]["label"] == "Team A (renamed)"
    assert sheets[0]["registered_at"] == first  # not reset on re-registration


def test_disabled_sheets_are_excluded_unless_asked_for():
    store = MemoryStore()
    store.register_sheet("s1", {})
    store.register_sheet("s2", {})
    store.set_sheet_enabled("s2", False)
    assert [s["sheet_id"] for s in store.list_enabled_sheets()] == ["s1"]
    assert sorted(s["sheet_id"] for s in store.list_enabled_sheets(enabled_only=False)) == ["s1", "s2"]


@pytest.mark.asyncio
async def test_worker_processes_every_registered_sheet_independently(monkeypatch):
    store = MemoryStore()
    store.register_sheet("sheetA", {"label": "Team A"})
    store.register_sheet("sheetB", {"label": "Team B"})

    # Different requesters -> different accounts, so both sheets complete in
    # the same run with no contention (see the lease test below for the
    # same-account case).
    by_id = {
        "sheetA": FakeSheet([{"vendor": "Proponent", "part": "P1", "by": JANE}], sheet_id="sheetA"),
        "sheetB": FakeSheet([{"vendor": "Proponent", "part": "P2", "by": BOB}], sheet_id="sheetB"),
    }
    monkeypatch.setattr(job_main, "open_sheet", lambda sid, req, res: by_id[sid])
    monkeypatch.setattr(job_main, "SheetSink", lambda sheets, **_: FakeSink(sheets))

    portal = Portal()
    shared = Deps(
        sheets=None, store=store,
        creds=MemoryCredentialProvider({
            secret_key("proponent", JANE): {"username": "jane", "password": "pw"},
            secret_key("proponent", BOB): {"username": "bob", "password": "pw"},
        }),
        browser=FakeBrowser(), sink=None, notifier=FakeNotifier(),
        adapter_factory=lambda vid, sel, cfg: FakeAdapter(portal, selectors=sel) if vid == "proponent" else None,
    )

    results = await asyncio.gather(
        *(job_main.run_one_sheet(shared, s, "run1") for s in store.list_enabled_sheets())
    )
    assert sorted(r["sheet_id"] for r in results) == ["sheetA", "sheetB"]
    assert by_id["sheetA"].status_of("P1") == "DONE"
    assert by_id["sheetB"].status_of("P2") == "DONE"
    # Two different sheets, same row number -> must not collide in the row store.
    assert store.rows["sheetA__row2"]["status"] == "DONE"
    assert store.rows["sheetB__row2"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_sheets_sharing_one_login_dont_race_but_one_waits_for_the_next_run(monkeypatch):
    """Two sheets whose rows resolve to the SAME vendor account (e.g. both use
    the shared Proponent login) must not log in twice at once. One sheet's row
    goes through now; the other's goes back to NEW for the next run/sweep,
    rather than being silently dropped or double-logged-in."""
    store = MemoryStore()
    store.register_sheet("sheetA", {})
    store.register_sheet("sheetB", {})

    by_id = {
        "sheetA": FakeSheet([{"vendor": "Proponent", "part": "P1", "by": JANE}], sheet_id="sheetA"),
        "sheetB": FakeSheet([{"vendor": "Proponent", "part": "P2", "by": JANE}], sheet_id="sheetB"),
    }
    monkeypatch.setattr(job_main, "open_sheet", lambda sid, req, res: by_id[sid])
    monkeypatch.setattr(job_main, "SheetSink", lambda sheets, **_: FakeSink(sheets))

    portal = Portal()
    shared = Deps(
        sheets=None, store=store,
        creds=MemoryCredentialProvider({secret_key("proponent", JANE): {"username": "jane", "password": "pw"}}),
        browser=FakeBrowser(), sink=None, notifier=FakeNotifier(),
        adapter_factory=lambda vid, sel, cfg: FakeAdapter(portal, selectors=sel) if vid == "proponent" else None,
    )

    await asyncio.gather(*(job_main.run_one_sheet(shared, s, "run1") for s in store.list_enabled_sheets()))
    # Depending on scheduling, either both complete in this run (one lease
    # acquired and released before the other asks) or one gets "busy" and is
    # requeued to NEW — both are fine; FAILED or a lost row would not be.
    after_run1 = {by_id["sheetA"].status_of("P1"), by_id["sheetB"].status_of("P2")}
    assert after_run1 <= {"DONE", "NEW"}

    # Whichever row didn't finish is picked up by the next run (the sweep).
    await asyncio.gather(*(job_main.run_one_sheet(shared, s, "run2") for s in store.list_enabled_sheets()))
    assert by_id["sheetA"].status_of("P1") == "DONE" and by_id["sheetB"].status_of("P2") == "DONE"


@pytest.mark.asyncio
async def test_one_sheet_failing_to_open_does_not_stop_the_others(monkeypatch):
    store = MemoryStore()
    store.register_sheet("broken", {})
    store.register_sheet("ok", {})
    ok_sheet = FakeSheet([{"vendor": "Proponent", "part": "P1", "by": JANE}], sheet_id="ok")

    def fake_client(sid, req, res):
        if sid == "broken":
            raise RuntimeError("not shared with the service account")
        return ok_sheet

    monkeypatch.setattr(job_main, "open_sheet", fake_client)
    monkeypatch.setattr(job_main, "SheetSink", lambda sheets, **_: FakeSink(sheets))

    portal = Portal()
    shared = Deps(
        sheets=None, store=store,
        creds=MemoryCredentialProvider({secret_key("proponent", JANE): {"username": "jane", "password": "pw"}}),
        browser=FakeBrowser(), sink=None, notifier=FakeNotifier(),
        adapter_factory=lambda vid, sel, cfg: FakeAdapter(portal, selectors=sel) if vid == "proponent" else None,
    )

    results = await asyncio.gather(
        *(job_main.run_one_sheet(shared, s, "run1") for s in store.list_enabled_sheets())
    )
    by_id = {r["sheet_id"]: r for r in results}
    assert "error" in by_id["broken"]
    assert "error" not in by_id["ok"]
    assert ok_sheet.status_of("P1") == "DONE"
