"""Command-line flags of service.job_main: env overrides and per-run vendor config."""
import pytest

import service.job_main as job_main
from sourcing.store import MemoryStore


def parse(*argv):
    parser = job_main.build_parser()
    args = parser.parse_args(list(argv))
    return job_main.env_overrides(args), job_main.vendor_overrides(args, parser)


def test_no_flags_searches_in_bulk_with_env_settings():
    # bulk from 2 parts even if the stored vendor config says otherwise
    assert parse() == ({}, {"*": {"bulk_min_parts": 2}})


def test_bulk_flag():
    assert parse("--bulk")[1] == {"*": {"bulk_min_parts": 2}}
    assert parse("--bulk", "5")[1] == {"*": {"bulk_min_parts": 5}}


def test_one_by_one_flag():
    # forced off even if the stored vendor config turns bulk on
    assert parse("--one-by-one")[1] == {"*": {"bulk_min_parts": None}}
    with pytest.raises(SystemExit):
        parse("--one-by-one", "--bulk")


def test_env_flags():
    env, _ = parse("--sheet", "test.xlsx", "--dummy", "--record", "--dump-pages")
    assert env == {"SHEET_ID": "test.xlsx", "DUMMY_MODE": "1",
                   "BROWSERBASE_RECORD_SESSIONS": "1", "DEBUG_DUMP_PAGES": "1"}


def test_results_tab_only_with_its_flag_and_rfq_flag_is_harmless():
    assert parse("--results")[0] == {"WRITE_RESULTS_TAB": "1"}
    assert parse()[0] == {} and parse("--rfq")[0] == {}      # RFQ tab is always written


def test_set_and_merge_into_stored_config():
    _, vendor = parse("--set", "proponent:logout_after_run=false", "--set", "probe_url=https://x/a")
    seen = {}
    factory = job_main.with_vendor_overrides(
        lambda vid, sel, cfg: seen.update({vid: cfg}), vendor)
    stored = {"bulk_min_parts": 3, "retry_not_found": True}
    factory("proponent", {}, stored)
    factory("other", {}, {})
    assert seen["proponent"] == {"bulk_min_parts": 2, "retry_not_found": True,
                                 "probe_url": "https://x/a", "logout_after_run": False}
    assert seen["other"] == {"bulk_min_parts": 2, "probe_url": "https://x/a"}
    assert stored == {"bulk_min_parts": 3, "retry_not_found": True}  # not mutated


@pytest.mark.asyncio
@pytest.mark.parametrize("all_sheets, expected", [
    (False, ["env-sheet"]),
    (True, ["registered", "env-sheet"]),
])
async def test_which_sheets_run(monkeypatch, all_sheets, expected):
    store = MemoryStore()
    store.register_sheet("registered", {})
    settings = job_main.get_settings()
    monkeypatch.setattr(job_main, "get_settings",
                        lambda: job_main.replace(settings, sheet_id="env-sheet"))
    monkeypatch.setattr(job_main, "make_store", lambda *a: store)
    monkeypatch.setattr(job_main, "build_shared_deps", lambda *a, **k: None)
    ran = []

    async def fake_run_one(shared, sheet, run_id):
        ran.append(sheet["sheet_id"])
        return {"sheet_id": sheet["sheet_id"]}

    monkeypatch.setattr(job_main, "run_one_sheet", fake_run_one)
    await job_main.run(dummy=True, all_sheets=all_sheets)
    assert sorted(ran) == sorted(expected)
