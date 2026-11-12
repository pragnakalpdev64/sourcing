"""Pipeline tests against fakes (plan section 11, the parts that need no GCP)."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from sourcing.credentials.base import MemoryCredentialProvider, secret_key
from sourcing.models import AccountState, Offer, RequestRow, ResultRow, SearchOutcome
from sourcing.pipeline import Deps, Pipeline, alternate_notes, flag_for, lead_days
from sourcing.store import WORKER_LOCK, MemoryStore

from .fakes import FakeAdapter, FakeBrowser, FakeNotifier, FakeSheet, FakeSink, Portal

JANE, BOB = "jane@co.com", "bob@co.com"


def make(rows, secrets=None, portal=None, store=None):
    sheet = FakeSheet(rows)
    portal = portal or Portal()
    deps = Deps(
        sheets=sheet,
        store=store or MemoryStore(),
        creds=MemoryCredentialProvider(secrets if secrets is not None else {
            secret_key("proponent", JANE): {"username": "jane", "password": "pw"},
        }),
        browser=FakeBrowser(),
        sink=FakeSink(sheet),
        notifier=FakeNotifier(),
        adapter_factory=lambda vid, sel, cfg: FakeAdapter(portal, selectors=sel) if vid == "proponent" else None,
    )
    return deps, sheet, portal


def _results(sheet):
    """Result rows as {column heading: value}."""
    from sourcing.models import RESULTS_HEADER

    return [dict(zip(RESULTS_HEADER, r)) for r in sheet.results]


async def run(deps, run_id="r1"):
    return await Pipeline(deps, run_id).run()


@pytest.mark.asyncio
async def test_rows_are_searched_and_results_written():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE},
                                {"vendor": "Proponent", "part": "P2", "by": JANE}])
    summary = await run(deps)
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["DONE", "DONE"]
    results = _results(sheet)
    assert [r["Part Number"] for r in results] == ["P1", "P2"]
    assert results[0]["Requested Part"] == "P1" and "Qty Needed" in results[0]
    assert results[0]["Unit Price"] == "10.00" and results[0]["Requested By"] == JANE
    assert portal.logins == [JANE]          # one login for the whole group
    assert summary["counts"] == {"DONE": 2}
    assert deps.store.rows["test-sheet__row2"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_second_run_reuses_context_without_login():
    store = MemoryStore()
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    await run(deps, "r1")
    browser = deps.browser

    sheet.rows[0].status = "NEW"
    await run(deps, "r2")
    assert portal.logins == [JANE]                 # no second login
    assert browser.opened == ["ctx1", "ctx1"]       # same Context both runs
    assert store.accounts[f"proponent__{JANE}"]["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_shared_account_fallback_and_no_credentials():
    secrets = {"sourcing-vendor-proponent": {"username": "team", "password": "pw"}}
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": BOB}], secrets=secrets)
    await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert portal.logins == ["_shared"]

    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": BOB}], secrets={})
    await run(deps)
    assert sheet.status_of("P1") == "NO_CREDENTIALS"
    assert any(to == BOB and "No proponent login" in text for to, text in deps.notifier.messages)


@pytest.mark.asyncio
async def test_two_requesters_sharing_one_login_use_one_account():
    secrets = {"sourcing-vendor-proponent": {"username": "team", "password": "pw"}}
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE},
                                {"vendor": "Proponent", "part": "P2", "by": BOB}], secrets=secrets)
    await run(deps)
    assert portal.logins == ["_shared"]
    assert deps.browser.opened == ["ctx1"]


@pytest.mark.asyncio
async def test_two_users_same_vendor_get_separate_contexts():
    secrets = {secret_key("proponent", JANE): {"username": "j", "password": "pw"},
               secret_key("proponent", BOB): {"username": "b", "password": "pw"}}
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE},
                                {"vendor": "Proponent", "part": "P2", "by": BOB}], secrets=secrets)
    await run(deps)
    assert sorted(portal.logins) == sorted([JANE, BOB])
    assert sorted(deps.browser.opened) == ["ctx1", "ctx2"]
    assert sheet.status_of("P1") == sheet.status_of("P2") == "DONE"


@pytest.mark.asyncio
async def test_session_expiry_mid_run_relogs_once_and_retries():
    portal = Portal(expire_before={"P2"})
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE} for p in ("P1", "P2", "P3")],
                               portal=portal)
    await run(deps)
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE"] * 3
    assert portal.logins == [JANE, JANE]
    assert portal.searches == ["P1", "P2", "P2", "P3"]


@pytest.mark.asyncio
async def test_not_found_only_when_probe_passes():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "NOPE", "by": JANE}])
    await run(deps)
    assert sheet.status_of("NOPE") == "NOT_FOUND"


@pytest.mark.asyncio
async def test_wrong_password_stops_and_blocks_next_run():
    secrets = {secret_key("proponent", JANE): {"username": "jane", "password": "WRONG"}}
    store = MemoryStore()
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}],
                               secrets=secrets, store=store)
    await run(deps, "r1")
    assert sheet.status_of("P1") == "NO_CREDENTIALS"
    assert portal.logins == [JANE]   # wrong password is never retried
    assert store.accounts[f"proponent__{JANE}"]["state"] == AccountState.NEEDS_CREDENTIAL_UPDATE.value

    sheet.rows[0].status = "NEW"
    await run(deps, "r2")
    assert portal.logins == [JANE]   # account blocked until the password is updated
    assert sheet.status_of("P1") == "NO_CREDENTIALS"


@pytest.mark.asyncio
async def test_wrong_password_message_links_to_the_update_page():
    secrets = {secret_key("proponent", JANE): {"username": "jane", "password": "WRONG"}}
    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], secrets=secrets)
    deps.public_base_url = "https://api.example/"
    await run(deps)
    texts = [text for to, text in deps.notifier.messages if to == JANE]
    assert any("https://api.example/credentials/update?vendor=proponent&owner=jane%40co.com"
               in t for t in texts)
    assert not any("WRONG" in t for _, t in deps.notifier.messages)


@pytest.mark.asyncio
async def test_sms_2fa_marks_rows_needs_attention_and_tells_requester():
    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], portal=Portal(mfa="sms"))
    await run(deps)
    assert sheet.status_of("P1") == "NEEDS_ATTENTION"
    assert any(to == JANE and "2FA" in text for to, text in deps.notifier.messages)


@pytest.mark.asyncio
async def test_unsupported_or_empty_vendor_fails_row():
    deps, sheet, _ = make([{"vendor": "Acme", "part": "P1", "by": JANE},
                           {"vendor": "", "part": "P2", "by": JANE}])
    await run(deps)
    # an unknown vendor is set up from its secret (2026-10-16); without one: NO_CREDENTIALS
    assert sheet.status_of("P1") == "NO_CREDENTIALS"
    assert "not set up: no login secret sourcing-vendor-acme" in sheet.notes[2]
    assert sheet.status_of("P2") == "FAILED"


@pytest.mark.asyncio
async def test_rows_added_during_run_are_picked_up():
    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    added = []

    def add_once(s):
        if not added:
            added.append(True)
            s.rows.append(RequestRow(sheet_row=99, rfq_id="X", vendor_raw="Proponent",
                                     part_number="P3", requested_by=JANE, status="NEW"))
    sheet.on_read = add_once
    await run(deps)
    assert sheet.status_of("P3") == "DONE"


@pytest.mark.asyncio
async def test_rows_left_running_by_a_dead_run_are_resumed():
    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE, "status": "RUNNING"},
                           {"vendor": "Proponent", "part": "P2", "by": JANE, "status": "DONE"}])
    await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert len(sheet.results) == 1


@pytest.mark.asyncio
async def test_shared_account_2fa_notifies_each_requester():
    secrets = {"sourcing-vendor-proponent": {"username": "team", "password": "pw"}}
    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE},
                           {"vendor": "Proponent", "part": "P2", "by": BOB}],
                          secrets=secrets, portal=Portal(mfa="sms"))
    await run(deps)
    # to=None is the end-of-run summary for the space, which lists the rows too
    assert {to for to, text in deps.notifier.messages if "2FA" in text and to} == {JANE, BOB}


@pytest.mark.asyncio
async def test_busy_account_puts_rows_back_to_new():
    store = MemoryStore()
    store.acquire_lease(f"proponent__{JANE}", "someone-else", 600)
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    await run(deps)
    assert sheet.status_of("P1") == "NEW" and portal.logins == []


def test_worker_lock_is_exclusive():
    store = MemoryStore()
    assert store.acquire_lock(WORKER_LOCK, "a", 60)
    assert not store.acquire_lock(WORKER_LOCK, "b", 60)
    assert store.is_locked(WORKER_LOCK)
    store.release_lock(WORKER_LOCK, "a")
    assert store.acquire_lock(WORKER_LOCK, "b", 60)


def test_flags():
    row = RequestRow(sheet_row=2, rfq_id="", vendor_raw="Proponent", part_number="P",
                     qty_needed="10", condition_wanted="NE")
    assert flag_for(Offer("P", price="5", qty_available="3", condition="OH"), row) == "QTY_SHORT,CONDITION_DIFFERS"
    assert flag_for(Offer("P", price="5", qty_available="1,200", condition="ne"), row) == ""
    assert flag_for(Offer("P", price=None, qty_available="20"), row) == "NO_PRICE"
    assert flag_for(Offer("P", price=None, qty_available="Out of stock"), row) == "QTY_SHORT"


@pytest.mark.asyncio
async def test_a_context_that_never_logged_in_always_logs_in_even_if_the_probe_passes():
    """Regression: Proponent's public home page fooled the auth probe, so a fresh
    Context counted as signed in, the run searched logged out, and every part
    came back as a false NOT_FOUND."""

    class ProbeAlwaysPasses(FakeAdapter):
        async def is_authenticated(self, page) -> bool:
            return True

    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.adapter_factory = lambda vid, sel, cfg: ProbeAlwaysPasses(portal, selectors=sel)
    await run(deps)
    assert portal.logins == [JANE]
    assert deps.store.accounts[f"proponent__{JANE}"]["last_login_at"]


# -- bulk search and logout -------------------------------------------------------
from sourcing.models import LlmUsage, SearchOutcome  # noqa: E402
from sourcing.vendors.base import (  # noqa: E402
    BulkResultsIncomplete, BulkSearchUnavailable, SessionExpired)


class BulkAdapter(FakeAdapter):
    max_parts_per_search = 10
    default_bulk_batch_size = 2
    bulk_calls: list = []

    async def search_many(self, page, rows):
        self.bulk_calls.append([r.part_number for r in rows])
        if self.config.get("break_bulk"):
            raise BulkSearchUnavailable("form missing")
        if len(rows) > self.config.get("results_fit", 999):
            raise BulkResultsIncomplete(f"results for {len(rows)} parts too long")
        if len(self.bulk_calls) <= self.config.get("flaky_bulk", 0):
            raise BulkSearchUnavailable("dropdown stuck")
        if len(self.bulk_calls) <= self.config.get("hang_bulk", 0):
            await asyncio.sleep(30)                      # a page that stopped responding
        if not page.signed_in:
            raise SessionExpired("logged out")
        out = {}
        for r in rows:
            price = self.portal.catalog.get(r.part_number)
            if price and r.part_number not in self.config.get("bulk_blind", ()):
                out[r.row_id] = SearchOutcome(found=True, offers=[Offer(
                    part_number=r.part_number, price=price, currency="USD", qty_available="5",
                    condition="NE", lead_time="Stock", source_url="https://portal/bulk")])
        return out, LlmUsage(calls=1, cost_usd=0.01)


def _bulk_deps(parts, vendor_config=None):
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE} for p in parts])
    BulkAdapter.bulk_calls = []
    deps.adapter_factory = lambda vid, sel, cfg: BulkAdapter(
        portal, selectors=sel, vendor_config={**(cfg or {}), **(vendor_config or {})})
    return deps, sheet, portal


@pytest.mark.asyncio
async def test_parts_are_searched_in_bulk_batches():
    deps, sheet, portal = _bulk_deps(["P1", "NOPE", "P3"])
    summary = await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "NOPE"]]   # batch of 2, then 1 left -> single
    assert portal.searches == ["P3", "NOPE"]             # NOPE retried alone before logout
    assert [sheet.status_of(p) for p in ("P1", "NOPE", "P3")] == ["DONE", "NOT_FOUND", "DONE"]
    assert summary["llm"]["calls"] == 1


def _count_writes(deps, sheet):
    """Counts the sink writes and status writes a run makes (Sheets write quota)."""
    calls = {"sink": [], "status": []}
    write = deps.sink.write

    async def counted(rows):
        calls["sink"].append([r.part_number for r in rows])
        return await write(rows)

    def set_statuses(items, run_id=None):
        calls["status"].append([(r.part_number, st.value) for rows, st, _ in items for r in rows])
        for rows, st, note in items:
            sheet.set_status(rows, st, run_id, note)

    deps.sink.write, sheet.set_statuses = counted, set_statuses
    return calls


@pytest.mark.asyncio
async def test_a_bulk_batch_is_saved_in_one_results_write_and_one_status_write():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3", "NOPE"], {"bulk_batch_size": 4})
    calls = _count_writes(deps, sheet)
    await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2", "P3", "NOPE"]]
    # one write of the batch's lines; NOPE is searched again on its own afterwards
    assert calls["sink"][0] == ["P1", "P2", "P3"] and portal.searches == ["NOPE"]
    batch_status = [s for s in calls["status"] if ("P1", "DONE") in s]
    assert batch_status == [[("P1", "DONE"), ("P2", "DONE"), ("P3", "DONE")]]
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3", "NOPE")] == [
        "DONE", "DONE", "DONE", "NOT_FOUND"]
    assert [r["Part Number"] for r in _results(sheet)] == ["P1", "P2", "P3"]


@pytest.mark.asyncio
async def test_a_bulk_batch_that_cant_be_written_is_kept_and_written_next_run():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"bulk_batch_size": 3})
    deps.sink.fail_next = 1                       # the batch's one Results write fails
    await run(deps)
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["FAILED"] * 3
    assert all("written on the next run" in sheet.notes[n] for n in (2, 3, 4))
    assert sheet.results == [] and len(deps.store.rows_with_unsaved_results("test-sheet")) == 3
    await run(deps, run_id="r2")
    assert BulkAdapter.bulk_calls == [["P1", "P2", "P3"]]      # not searched again
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE"] * 3
    assert [r["Part Number"] for r in _results(sheet)] == ["P1", "P2", "P3"]
    assert deps.store.rows_with_unsaved_results("test-sheet") == []


@pytest.mark.asyncio
async def test_a_status_write_over_quota_is_retried_after_the_wait(monkeypatch):
    import sourcing.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "QUOTA_WAIT", 0.01)
    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"bulk_batch_size": 2})
    calls = _count_writes(deps, sheet)
    counted, fails = sheet.set_statuses, [2]

    def flaky(items, run_id=None):
        if fails[0] and any(st.value == "DONE" for _, st, _ in items):
            fails[0] -= 1
            raise Exception("APIError: [429]: Quota exceeded for quota metric 'Write requests'")
        counted(items, run_id)

    sheet.set_statuses = flaky
    await run(deps)
    assert fails == [0] and [sheet.status_of(p) for p in ("P1", "P2")] == ["DONE", "DONE"]
    assert len(calls["sink"]) == 1                 # Results written once, not again


@pytest.mark.asyncio
async def test_bulk_failure_falls_back_to_one_by_one():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"break_bulk": True})
    await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2"]] * 2  # tried twice, then turned off
    assert portal.searches == ["P1", "P2", "P3"]
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE", "DONE", "DONE"]


@pytest.mark.asyncio
async def test_bulk_batch_size_1_in_vendor_config_turns_bulk_off():
    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"bulk_batch_size": 1})
    await run(deps)
    assert BulkAdapter.bulk_calls == [] and portal.searches == ["P1", "P2"]


class SubstituteAdapter(FakeAdapter):
    """The part and an approved substitute for it, as Wencor / Aviall pages can show."""

    async def search(self, page, row):
        outcome = await super().search(page, row)
        outcome.offers.append(Offer(part_number=row.part_number + "-SUB", price="4.00",
                                    currency="USD", qty_available="9", condition="NE",
                                    listing="substitute", source_url="https://portal/s"))
        return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("show, parts", [(True, ["P1", "P1-SUB"]), (False, ["P1"])])
async def test_substitutes_only_for_vendors_that_show_them(show, parts):
    # Team, 2026-11-03: none for Wencor and Aviall (show_substitutes off).
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    cfg = {"show_substitutes": show}
    deps.adapter_factory = lambda vid, sel, _: SubstituteAdapter(portal, selectors=sel,
                                                                 vendor_config=cfg)
    await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert [r["Part Number"] for r in _results(sheet)] == parts


class LogoutAdapter(FakeAdapter):
    logouts: list = []

    async def logout(self, page) -> bool:
        self.logouts.append(page.context_id if hasattr(page, "context_id") else "page")
        page.set_signed_in(False)
        return True


@pytest.mark.asyncio
async def test_signed_out_at_the_end_so_the_next_run_logs_in_again():
    store = MemoryStore()
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    LogoutAdapter.logouts = []
    deps.adapter_factory = lambda vid, sel, cfg: LogoutAdapter(portal, selectors=sel,
                                                               vendor_config=cfg)
    await run(deps, "r1")
    account = store.accounts[f"proponent__{JANE}"]
    assert len(LogoutAdapter.logouts) == 1
    assert account["last_login_at"] is None and account["last_logout_at"]

    sheet.rows[0].status = "NEW"
    await run(deps, "r2")
    assert portal.logins == [JANE, JANE]


@pytest.mark.asyncio
async def test_logout_after_run_false_keeps_the_session():
    store = MemoryStore()
    store.vendors["proponent"] = {"logout_after_run": False}
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}], store=store)
    LogoutAdapter.logouts = []
    deps.adapter_factory = lambda vid, sel, cfg: LogoutAdapter(portal, selectors=sel,
                                                               vendor_config=cfg)
    await run(deps, "r1")
    assert LogoutAdapter.logouts == []
    assert store.accounts[f"proponent__{JANE}"]["last_login_at"]


@pytest.mark.asyncio
async def test_parts_the_bulk_search_missed_are_searched_again_before_logout():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"bulk_blind": ["P2"]})
    await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2"]]
    assert portal.searches == ["P3", "P2"]               # P3 left over, then the miss P2
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE"] * 3


@pytest.mark.asyncio
async def test_retry_not_found_can_be_turned_off_or_capped():
    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"bulk_blind": ["P1", "P2"],
                                                    "retry_not_found": False})
    await run(deps)
    assert portal.searches == [] and sheet.status_of("P2") == "NOT_FOUND"

    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"bulk_blind": ["P1", "P2"],
                                                    "retry_not_found_max": 1})
    await run(deps)
    assert portal.searches == ["P1"]
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["DONE", "NOT_FOUND"]


@pytest.mark.asyncio
async def test_one_bulk_failure_retries_the_batch_in_bulk():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"flaky_bulk": 1})
    await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2"], ["P1", "P2"]]
    assert portal.searches == ["P3"]                     # bulk stayed on
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE"] * 3


@pytest.mark.asyncio
async def test_a_stuck_bulk_search_is_cut_off_and_retried():
    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"hang_bulk": 1, "bulk_timeout_seconds": 0.05})
    await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2"], ["P1", "P2"]]
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["DONE", "DONE"]


@pytest.mark.asyncio
async def test_a_stuck_single_search_fails_that_row_only():
    class HangingAdapter(BulkAdapter):
        async def search(self, page, row):
            if row.part_number == "P1":
                await asyncio.sleep(30)
            return await super().search(page, row)

    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"bulk_batch_size": 1,
                                                    "search_timeout_seconds": 0.05})
    deps.adapter_factory = lambda vid, sel, cfg: HangingAdapter(
        portal, selectors=sel, vendor_config={**(cfg or {}), "bulk_batch_size": 1,
                                              "search_timeout_seconds": 0.05})
    await run(deps)
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["FAILED", "DONE"]


@pytest.mark.asyncio
async def test_a_saved_context_missing_from_browserbase_is_replaced_and_logged_in():
    """E.g. the Browserbase key was switched to another account: the saved
    Context id is unknown there (400 "Context ID not found")."""
    from sourcing.browser.browserbase import ContextNotFound

    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    aid = f"proponent__{JANE}"
    deps.store.update_account(aid, {"context_id": "old-account-ctx", "last_login_at": 1.0,
                                    "state": "ACTIVE"})
    real_open = deps.browser.open

    async def open_(context_id, **kw):
        if context_id == "old-account-ctx":
            raise ContextNotFound("Browserbase Context old-account-ctx not found")
        return await real_open(context_id, **kw)

    deps.browser.open = open_
    await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert deps.store.accounts[aid]["context_id"] == "ctx1"   # fresh Context saved
    assert portal.logins == [JANE]                            # logged in again, not trusted


def test_context_not_found_is_recognised_from_the_api_error():
    from sourcing.browser.browserbase import _is_context_not_found

    assert _is_context_not_found(Exception(
        "Error code: 400 - {'statusCode': 400, 'error': 'Bad Request', "
        "'message': 'Context ID not found'}"))
    assert not _is_context_not_found(Exception("Error code: 429 - rate limited"))


def test_out_of_stock_text_counts_as_qty_short():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                     qty_needed="2")
    offer = Offer(part_number="P1", price="5.00", currency="USD", qty_available="Out of stock",
                  condition=None, lead_time="129 days", source_url="x")
    assert flag_for(offer, row) == "QTY_SHORT"


@pytest.mark.asyncio
async def test_default_condition_fills_lines_the_portal_shows_none_for():
    deps, sheet, portal = _bulk_deps(["P1", "P2"], {"default_condition": "New"})
    await run(deps)
    rows = _results(sheet)
    assert rows and all(r["Condition"] == "NE" for r in rows)   # as its code


def test_part_number_flags():
    def flags(listed, asked="ACHC-3-13", listing="requested"):
        row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number=asked)
        return flag_for(Offer(part_number=listed, price="1.00", qty_available="5",
                              listing=listing), row)

    assert flags("ACHC-3-13") == ""
    assert flags("achc-3-13") == ""                          # letter case only
    assert flags("624018-1", "B7210", "substitute") == "SUBSTITUTE"


@pytest.mark.asyncio
async def test_look_alike_part_numbers_are_not_written():
    # client, 2026-10-23: Requested and Quoted part numbers are never different
    deps, sheet = _offer_rows([Offer(part_number="P1", price="2.00", qty_available="5"),
                               Offer(part_number="P-1", price="2.10", qty_available="5"),
                               Offer(part_number="P1-X", price="3.00", qty_available="5",
                                     listing="other")])
    await run(deps)
    assert [r["Part Number"] for r in _results(sheet)] == ["P1"]
    assert sheet.status_of("P1") == "DONE"


@pytest.mark.asyncio
async def test_only_look_alike_part_numbers_is_not_found():
    deps, sheet = _offer_rows([Offer(part_number="P-1", price="2.10", qty_available="5")])
    await run(deps)
    assert sheet.results == [] and sheet.status_of("P1") == "NOT_FOUND"
    assert "P-1" in sheet.notes[2]


@pytest.mark.asyncio
async def test_without_bulk_min_parts_every_part_is_searched_on_its_own():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"bulk_min_parts": None})
    await run(deps)
    assert BulkAdapter.bulk_calls == [] and portal.searches == ["P1", "P2", "P3"]
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE"] * 3


@pytest.mark.asyncio
async def test_bulk_only_from_bulk_min_parts():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"bulk_min_parts": 4})
    await run(deps)                                       # 3 parts < 4: one by one
    assert BulkAdapter.bulk_calls == [] and len(portal.searches) == 3

    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"bulk_min_parts": 3})
    await run(deps)                                       # 3 parts >= 3: bulk
    assert BulkAdapter.bulk_calls == [["P1", "P2"]]


def test_proponent_searches_one_part_at_a_time_until_configured():
    from sourcing.vendors.proponent import ProponentAdapter

    assert ProponentAdapter().bulk_min_parts is None
    assert ProponentAdapter(vendor_config={"bulk_min_parts": 30}).bulk_min_parts == 30


def test_lead_time_is_only_days_and_only_when_not_in_stock():
    def lead(qty, text, review=None):
        return lead_days(Offer(part_number="P1", qty_available=qty, lead_time=text,
                               review=review), "Proponent")

    assert lead("12", "In stock (59 days if out of stock)") == ""
    assert lead("Out of stock", "129 days (next due Nov 20, 2026, qty 65)") == "129"
    assert lead("0", "1,040 days") == "1040"
    assert lead("Out of stock", None) == "Not shown by Proponent"
    assert lead(None, None) == "Not shown by Proponent"
    assert lead("Out of stock", "8 weeks") == "8 weeks"                 # as the page shows it
    assert lead("Out of stock", None, "LEAD_TIME_NOT_ON_PAGE") == "Check page (see Flag)"


@pytest.mark.asyncio
async def test_delivery_is_0_in_stock_else_the_lead_time():
    # client, 2026-10-23
    deps, sheet = _offer_rows([
        Offer(part_number="P1", price="2.00", qty_available="12", condition="NE",
              lead_time="In stock (59 days if out of stock)"),
        Offer(part_number="P1", price="2.00", qty_available="Out of stock", condition="NE",
              lead_time="129 days (next due Nov 20, 2026, qty 65)")])
    await run(deps)
    assert [r.as_rfq_record()["Delivery"] for r in deps.sink.rows] == ["0", "129"]


def test_currency_comes_from_the_vendor_when_the_page_shows_none():
    from sourcing.vendors.proponent import ProponentAdapter

    assert ProponentAdapter().currency == "USD"            # Procart shows only "$"
    assert ProponentAdapter(vendor_config={"currency": "eur"}).currency == "EUR"
    assert ProponentAdapter(vendor_config={"currency": ""}).currency == ""


@pytest.mark.asyncio
async def test_too_big_bulk_results_retry_the_same_parts_in_half_the_batch():
    parts = [f"P{n}" for n in range(1, 10)]
    deps, sheet, portal = _bulk_deps(parts, {"bulk_batch_size": 8, "results_fit": 3})
    portal.catalog.update({p: "1.00" for p in parts})
    await run(deps)
    sizes = [len(c) for c in BulkAdapter.bulk_calls]
    assert sizes == [8, 4, 2, 2, 2, 2] and portal.searches == ["P9"]   # 2 kept for the run
    assert BulkAdapter.bulk_calls[1] == ["P1", "P2", "P3", "P4"]          # same parts first
    assert all(sheet.status_of(p) == "DONE" for p in parts)


@pytest.mark.asyncio
async def test_bulk_too_big_even_for_two_parts_goes_one_by_one():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "P3"], {"results_fit": 1})
    await run(deps)
    assert [len(c) for c in BulkAdapter.bulk_calls] == [2]
    assert portal.searches == ["P1", "P2", "P3"]


def _scripted(parts, script):
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE} for p in parts])
    portal.script = script
    return deps, sheet, portal


@pytest.mark.asyncio
async def test_a_failed_search_is_retried_once_before_logout():
    deps, sheet, portal = _scripted(["P1", "P2"], {"P1": ["error"]})
    await run(deps)
    assert portal.searches == ["P1", "P2", "P1"]          # P1 again at the end
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["DONE", "DONE"]


@pytest.mark.asyncio
async def test_a_search_that_fails_twice_is_failed_with_its_error():
    deps, sheet, portal = _scripted(["P1"], {"P1": ["error", "error"]})
    await run(deps)
    assert portal.searches == ["P1", "P1"] and sheet.status_of("P1") == "FAILED"
    assert "results grid did not load" in sheet.notes[2]


@pytest.mark.asyncio
async def test_an_empty_results_page_is_searched_again_before_logout():
    deps, sheet, portal = _scripted(["P1", "P2"], {"P1": ["empty"], "P2": ["empty", "empty"]})
    await run(deps)
    assert portal.searches == ["P1", "P2", "P1", "P2"]
    assert sheet.status_of("P1") == "DONE"               # in stock the second time
    assert sheet.status_of("P2") == "NOT_FOUND" and "empty" in sheet.notes[3]
    assert [r["Part Number"] for r in _results(sheet)] == ["P1"]


@pytest.mark.asyncio
async def test_a_part_the_portal_does_not_sell_is_not_sold_with_a_results_row():
    deps, sheet, portal = _scripted(["P1"], {"P1": ["not_sold"]})
    await run(deps)
    assert portal.searches == ["P1"]                      # a clear answer: not retried
    assert sheet.status_of("P1") == "NOT_SOLD"
    assert sheet.notes[2] == "Proponent: This product is restricted for sale."
    [r] = _results(sheet)
    assert r["Qty Available"] == "This product is restricted for sale."   # the portal's words
    assert r["Unit Price"] == "" and r["Flag"] == "NOT_SOLD"
    assert r["Source URL"] == "https://portal/r?id=P1"


@pytest.mark.asyncio
async def test_without_retry_not_found_a_failed_search_is_final():
    deps, sheet, portal = _scripted(["P1"], {"P1": ["error"]})
    deps.adapter_factory = lambda vid, sel, cfg: FakeAdapter(
        portal, selectors=sel, vendor_config={"retry_not_found": False})
    await run(deps)
    assert portal.searches == ["P1"] and sheet.status_of("P1") == "FAILED"


def test_not_sold_reason_reads_only_the_parts_own_record():
    from sourcing.vendors.proponent import ProponentAdapter

    page = ("Results read from the page's results grid (#jQGridDemo): 2 record(s).\n\n"
            "Record 1:\n  Cust_part: 0684-12-28\n"
            "  Item_description1: This part number is not recognized in Proponent system.\n"
            "  other_fields: This product is restricted for sale\n\n"
            "Record 2:\n  Cust_part: P2\n  Price: 5.00\n  QtyAvailable: 3")
    adapter = ProponentAdapter()
    assert adapter.not_sold_reason(page, "0684-12-28") == (
        "This part number is not recognized in Proponent system. "
        "This product is restricted for sale.")
    assert adapter.not_sold_reason(page, "P2") is None           # its own record is clean
    assert adapter.not_sold_reason(page, "P3") is None           # no record at all
    offer = Offer(part_number="0684-12-28", price="1.00", qty_available="2")
    assert adapter.not_sold_reason(page, "0684-12-28", [offer]) is None   # a real offer wins



def _offer_rows(offers, qty="1"):
    """Results rows for one part whose search returns `offers`."""
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE, "qty": qty}])

    async def search(page, row):
        return SearchOutcome(found=True, offers=offers)
    deps.adapter_factory = lambda vid, sel, cfg: _with_search(FakeAdapter(portal, selectors=sel),
                                                             search)
    return deps, sheet


def _with_search(adapter, search):
    adapter.search = search
    return adapter


@pytest.mark.asyncio
async def test_missing_values_say_who_does_not_show_them():
    deps, sheet = _offer_rows([Offer(part_number="P1", price="5.00", qty_available="3",
                                     review="WAREHOUSES_NOT_ON_PAGE")])
    await run(deps)
    [r] = _results(sheet)
    assert r["Condition"] == "Not shown by Proponent"
    assert r["Warehouses"] == "Check page (see Flag)"             # removed by verification
    assert r["Price at Qty Needed"] == "5.00"                     # qty 1: the unit price


@pytest.mark.asyncio
async def test_price_at_qty_needed_comes_from_the_adapter_or_says_check_page():
    deps, sheet = _offer_rows([Offer(part_number="P1", price="23.70", qty_available="9",
                                     qty_price="22.22"),
                               Offer(part_number="P1", price="5.48", qty_available="9")],
                              qty="300")
    await run(deps)
    rows = _results(sheet)
    assert [r["Price at Qty Needed"] for r in rows] == ["22.22", "Check page (quantity pricing)"]


@pytest.mark.asyncio
async def test_results_a_failed_write_kept_are_written_on_the_next_run():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.sink.fail_next = 1
    await run(deps)
    assert sheet.status_of("P1") == "FAILED" and sheet.results == []
    assert "written on the next run" in sheet.notes[2]
    await run(deps, run_id="r2")
    assert portal.searches == ["P1"]                    # not searched again
    assert sheet.status_of("P1") == "DONE" and len(sheet.results) == 1
    assert "written on a later run" in sheet.notes[2]
    assert deps.store.rows_with_unsaved_results("test-sheet") == []


@pytest.mark.asyncio
async def test_rfq_lines_not_written_are_kept_and_written_on_the_next_run():
    # Row 18 of 2026-10-27: Results saved, the RFQ tab over the Sheets quota.
    from sourcing.sinks.base import WriteOutcome

    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    written_rfq, rfq_fails = [], [1]

    async def write(rows):
        sheet.append_results([r.as_sheet_values() for r in rows])
        return [WriteOutcome(ok=True, rfq_error="APIError: [429]: Quota exceeded")
                for _ in rows]

    async def write_rfq(rows):
        if rfq_fails[0]:
            rfq_fails[0] -= 1
            return "APIError: [429]: Quota exceeded"
        written_rfq.extend(rows)
        return None

    deps.sink.write, deps.sink.write_rfq = write, write_rfq
    await run(deps)
    assert sheet.status_of("P1") == "DONE" and len(sheet.results) == 1
    assert "RFQ tab not written" in sheet.notes[2] and "written on the next run" in sheet.notes[2]
    await run(deps, run_id="r2")                         # still over quota: kept
    assert written_rfq == [] and deps.store.rows_with_unsaved_results("test-sheet")
    await run(deps, run_id="r3")
    assert [r.part_number for r in written_rfq] == ["P1"] and portal.searches == ["P1"]
    assert len(sheet.results) == 1                       # Results not written twice
    assert deps.store.rows_with_unsaved_results("test-sheet") == []


@pytest.mark.asyncio
async def test_kept_results_are_dropped_when_the_row_now_holds_another_part():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.sink.fail_next = 1
    await run(deps)
    sheet.rows[0].part_number, sheet.rows[0].status = "P2", "DONE"   # row 2 deleted above
    await run(deps, run_id="r2")
    assert sheet.results == [] and deps.store.rows_with_unsaved_results("test-sheet") == []


def test_older_results_of_the_same_request_are_marked_superseded():
    from sourcing.sheets import superseded_flags

    head = ["RFQ ID", "Vendor", "Requested Part", "Part Number", "Fetched At",
            "Requested By", "Flag", "Approved"]
    values = [head,
              ["R1", "Proponent", "P1", "P1", "2026-10-14 10:00:00Z", "jane", "", "yes"],
              ["R1", "Proponent", "P1", "P1-A", "2026-10-14 10:00:00Z", "jane", "SUBSTITUTE", ""],
              ["R1", "Proponent", "P2", "P2", "2026-10-14 10:00:00Z", "jane", "", ""],
              ["R1", "Proponent", "P1", "P1", "2026-10-15 09:00:00Z", "jane", "", ""]]
    new = [{"RFQ ID": "R1", "Vendor": "Proponent", "Requested Part": "P1",
            "Requested By": "jane", "Fetched At": "2026-10-15 09:00:00Z"}]
    assert superseded_flags(values, new) == [
        (2, 7, "SUPERSEDED by search of 2026-10-15 09:00:00Z"),
        (3, 7, "SUPERSEDED by search of 2026-10-15 09:00:00Z; SUBSTITUTE")]
    values[1][6] = "SUPERSEDED by search of 2026-10-14 12:00:00Z"
    assert [m[0] for m in superseded_flags(values, new)] == [3]       # marked once only
    # Rows the same run wrote are never marked (2026-10-28): the same part asked
    # twice in one sheet keeps both sets current.
    same_run = frozenset({(("R1", "Proponent", "P1", "jane"), "2026-10-14 10:00:00Z")})
    assert superseded_flags(values, new, same_run) == []


@pytest.mark.asyncio
async def test_sheet_sink_marks_older_rows_after_a_write():
    from sourcing.sinks.sheet import SheetSink

    class Sheets:
        appended, superseded = [], []

        def append_results(self, records):
            self.appended.extend(records)

        def supersede_results(self, records):
            self.superseded.extend(records)
            raise RuntimeError("quota")          # never fails the write

    sheets = Sheets()
    out = await SheetSink(sheets).write([ResultRow(rfq_id="R1", vendor="Proponent",
                                                   part_number="P1")])
    assert out[0].ok and len(sheets.appended) == 1 and len(sheets.superseded) == 1


def _alt_lines(*offers):
    """(Offer, ResultRow) pairs as _record builds them, for alternate_notes."""
    return [(o, ResultRow(rfq_id="R", vendor="Proponent", requested_part="A1",
                          part_number=o.part_number, price=o.price or "",
                          fetched_at="t", requested_by=JANE)) for o in offers]


def test_alternate_notes_when_requested_part_is_quoted():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    out = alternate_notes(row, _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested"),
        Offer(part_number="B2", price="4.00", qty_available="9", listing="substitute")))
    assert [(r.requested_part, r.part_number, r.internal_comments) for r in out] == [
        ("A1", "A1", "• APPROVED SUBSTITUTE ALSO OFFERED: PN B2"),
        ("B2", "B2", "• APPROVED SUBSTITUTE FOR PN A1")]   # never A1 / B2 (client, 2026-10-23)


def test_alternate_notes_add_a_pointer_row_when_only_the_alternate_is_listed():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    out = alternate_notes(row, _alt_lines(
        Offer(part_number="B2", price="4.00", qty_available="9", listing="substitute"),
        Offer(part_number="B2", price="3.00", qty_available="2", listing="substitute"),
        Offer(part_number="C3", price="6.00", qty_available="1", listing="substitute")))
    pointer = out[0]
    assert (pointer.part_number, pointer.requested_part, pointer.price) == ("A1", "A1", "0.01")
    assert pointer.internal_comments == "• QUOTING PN B2, PN C3"
    rfq = pointer.as_rfq_record()
    assert (rfq["Part Number"], rfq["PN Quoted"], rfq["Qty Quoted"], rfq["Unit Price"],
            rfq["Condition Quoted"], rfq["Type Quoted"], rfq["Delivery"]) == (
        "A1", "A1", "0", "0.01", "", "", "")
    assert len(out) == 4 and all(r.internal_comments == "• APPROVED SUBSTITUTE FOR PN A1"
                                 and r.requested_part == r.part_number for r in out[1:])


def test_alternate_notes_out_of_stock_requested_part_points_to_the_alternate():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    out = alternate_notes(row, _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="Out of stock", listing="requested"),
        Offer(part_number="B2", price="4.00", qty_available="9", listing="substitute")))
    assert len(out) == 2   # no extra row: the requested part has its own line
    assert out[0].internal_comments == "• QUOTING PN B2"


def test_out_of_stock_requested_part_with_a_lead_time_still_offers_the_substitute():
    # 2026-10-28: A is quoted on its lead time, so B is "also offered", with B's
    # lead time when B has no stock either.
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    lines = _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="Out of stock", listing="requested"),
        Offer(part_number="B2", price="4.00", qty_available="Out of stock", listing="substitute"),
        Offer(part_number="C3", price="6.00", qty_available="2", listing="substitute"))
    lines[0][1].qty_available, lines[0][1].lead_time = "Out of stock", "60"
    lines[1][1].lead_time = "154"
    out = alternate_notes(row, lines)
    assert out[0].internal_comments == (
        "• APPROVED SUBSTITUTE ALSO OFFERED: PN C3\n"
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH 154 DAY LEAD TIME: PN B2")


def test_listed_alternates_go_only_into_internal_comments():
    # Wencor / Aviall (team, 2026-11-04): no entry for the alternate, one line.
    from sourcing.pipeline import add_listed_alternates
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Wencor", part_number="A1")
    out = alternate_notes(row, _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested")))
    out[0].internal_comments = "• MOQ 10"
    add_listed_alternates(["B2", "C3", "B2", ""], out)
    assert len(out) == 1 and out[0].part_number == "A1"
    assert out[0].internal_comments == "• MOQ 10\n• ALTERNATE PN LISTED: PN B2, PN C3"
    rfq = out[0].as_rfq_record()
    assert rfq["PN Quoted"] == "A1" and "PN B2, PN C3" in rfq["Internal Comments"]
    none = alternate_notes(row, _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested")))
    add_listed_alternates([], none)
    assert none[0].internal_comments == ""


@pytest.mark.asyncio
async def test_a_run_writes_the_adapters_alternates_into_internal_comments():
    class WithAlternates(FakeAdapter):
        async def search(self, page, row):
            outcome = await super().search(page, row)
            outcome.alternates = ["ALT-1", "ALT-2"]
            return outcome

    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.adapter_factory = lambda vid, sel, cfg: WithAlternates(portal, selectors=sel)
    await run(deps)
    results = _results(sheet)
    assert len(results) == 1 and results[0]["Part Number"] == "P1"
    assert "ALTERNATE PN LISTED: PN ALT-1, PN ALT-2" in results[0]["Internal Comments"]


def test_alternate_notes_leave_look_alike_numbers_alone():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    out = alternate_notes(row, _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested"),
        Offer(part_number="A1X", price="4.00", qty_available="9", listing="other")))
    assert [r.internal_comments for r in out] == ["", ""]


def test_price_break_note_lists_the_whole_table_in_the_clients_words():
    from sourcing.pipeline import price_break_note

    o = Offer(part_number="A1", price="23.70", qty_available="5",
              price_breaks=[("1", "249", "23.70"), ("250", "499", "22.22"), ("500", "", "20.92")])
    assert price_break_note(o, "USD") == (
        "• PRICE BREAKS:\n    ◦ 1-249 $23.70\n    ◦ 250-499 $22.22\n    ◦ 500+ $20.92")
    assert price_break_note(o, "EUR").splitlines()[1] == "    ◦ 1-249 EUR 23.70"
    assert price_break_note(o, "").splitlines()[1] == "    ◦ 1-249 23.70"
    assert price_break_note(Offer(part_number="A1", price="5.00"), "USD") == ""


def test_alternate_notes_go_below_the_price_break_table():
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    lines = _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested"),
        Offer(part_number="B2", price="4.00", qty_available="9", listing="substitute"))
    for _, r in lines:
        r.internal_comments = "• PRICE BREAKS:\n    ◦ 1+ $5.00"
    out = alternate_notes(row, lines)
    assert out[0].internal_comments == ("• PRICE BREAKS:\n    ◦ 1+ $5.00\n"
                                        "• APPROVED SUBSTITUTE ALSO OFFERED: PN B2")
    assert out[1].internal_comments.endswith("\n• APPROVED SUBSTITUTE FOR PN A1")


def test_below_moq_is_flagged():
    from sourcing.pipeline import flag_for

    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1",
                     qty_needed="10")
    o = Offer(part_number="A1", price="1.02", qty_available="500", moq="50")
    assert flag_for(o, row) == "BELOW_MOQ"
    row.qty_needed = "50"
    assert flag_for(o, row) == ""
    assert flag_for(Offer(part_number="A1", price="1.02", qty_available="500"), row) == ""


def test_stock_note_names_every_warehouse_with_its_stock_below():
    from sourcing.pipeline import stock_note

    cell = ("Warehouse: Brea, CA\nQty on hand: 45\nNext day in: May 20, 2027\nQty due in: 200\n"
            "Next day in: Jun 1, 2027\n\nWarehouse: Singapore\nQty on hand: 5\n\n"
            "Qty due in: 30")

    def note(stocked, warehouses=cell, moq=None):
        return stock_note(Offer(part_number="P1", warehouses=warehouses, moq=moq,
                                qty_available="50" if stocked else "Out of stock"),
                          "10", priced=True, stocked=stocked)

    # in stock: its incoming stock listed too, as out of stock (team, 2026-11-06)
    assert note(True) == note(False)
    # 416F25-4 (2026-11-06): 5 on hand; Nov 25 10, Nov 27 1 and 6 -> two entries
    filt = ("Warehouse: Netherlands\nQty on hand: 5\nNext day in: Nov 25, 2026\nQty due in: 10\n"
            "Next day in: Nov 27, 2026\nQty due in: 1\nNext day in: Nov 27, 2026\nQty due in: 6")
    assert note(True, filt) == ("• STK NETHERLANDS\n    ◦ NETHERLANDS: 5 ON HAND, "
                                "NEXT IN NOV 25, 2026: 10, NEXT IN NOV 27, 2026: 7")
    assert note(False) == (
        "• STK BREA, CA / SINGAPORE / WAREHOUSE NOT NAMED\n"
        "    ◦ BREA, CA: 45 ON HAND, NEXT IN MAY 20, 2027: 200, NEXT IN JUN 1, 2027\n"
        "    ◦ SINGAPORE: 5 ON HAND\n"
        "    ◦ WAREHOUSE NOT NAMED: NEXT IN DATE NOT SHOWN: 30")
    assert note(True, None) == "" and note(True, None, moq="5") == "• MOQ 5"
    limits = "Warehouse: Brea, CA\nQty on hand: 198323\nMin order qty: 10\nMax qty available: 198323"
    assert note(True, limits) == "• STK BREA, CA\n    ◦ BREA, CA: 198323 ON HAND"
    # A warehouse with 0 on hand is not named after STK (2026-10-23).
    two = "Warehouse: Brea, CA\nQty on hand: 0\n\nWarehouse: Singapore\nQty on hand: 5"
    assert note(True, two) == ("• STK SINGAPORE\n    ◦ BREA, CA: 0 ON HAND\n"
                               "    ◦ SINGAPORE: 5 ON HAND")
    # 418-09104 (2026-10-26): three deliveries on the same date -> one entry, added up.
    same_day = ("Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Dec 6, 2026\nQty due in: 1\n"
                "Next day in: Dec 6, 2026\nQty due in: 3\nNext day in: Dec 6, 2026\nQty due in: 3\n"
                "Next day in: Jan 9, 2027\nQty due in: 2")
    assert note(False, same_day) == (
        "• OUT OF STOCK\n    ◦ BREA, CA: 0 ON HAND, NEXT IN DEC 6, 2026: 7, NEXT IN JAN 9, 2027: 2")
    # RA71CM012AT99NG (2026-10-23): out of stock in Brea, less than the minimum asked.
    assert note(False, "Warehouse: Brea, CA\nQty on hand: 0", moq="50") == (
        "• MOQ 50, PRICED AT 50 (ASKED 10), OUT OF STOCK\n    ◦ BREA, CA: 0 ON HAND")
    # Out of stock, nothing due in, a lead time: the client's line, MOQ below
    # (2026-10-27, 406Q80-1); every warehouse after FOB.
    def lead_note(warehouses, moq=None, lead="336 days"):
        return stock_note(Offer(part_number="P1", warehouses=warehouses, moq=moq, lead_time=lead,
                                qty_available="Out of stock"), "10", priced=True, stocked=False)

    assert lead_note("Warehouse: Netherlands\nQty on hand: 0") == (
        "• 336 DAY LEAD TIME, FOB: NETHERLANDS")
    assert lead_note("Warehouse: Brea, CA\nQty on hand: 0\n\nWarehouse: UK\nQty on hand: 0",
                     moq="50", lead="3-5 weeks") == (
        "• 35 DAY LEAD TIME, FOB: BREA, CA / UK\n• MOQ 50, PRICED AT 50 (ASKED 10)")
    # Stock due in, or no lead time: the stock lines as before.
    assert lead_note(same_day).startswith("• OUT OF STOCK\n")
    assert lead_note("Warehouse: Brea, CA\nQty on hand: 0", lead="") == (
        "• OUT OF STOCK\n    ◦ BREA, CA: 0 ON HAND")


def test_no_due_date_flag():
    from sourcing.pipeline import flag_for

    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                     qty_needed="1")
    undated = Offer(part_number="P1", qty_available="Out of stock",
                    warehouses="Warehouse: Brea, CA\nQty on hand: 0\nQty due in: 7")
    assert "NO_DUE_DATE" in flag_for(undated, row)
    undated.warehouses = "Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Dec 7, 2026\nQty due in: 7"
    assert "NO_DUE_DATE" not in flag_for(undated, row)


def test_moq_note():
    from sourcing.pipeline import moq_note

    # ASNA2397-10 (seen 2026-10-19): "Minimum order quantity for this part is 50 EA".
    def offer(moq):
        return Offer(part_number="ASNA2397-10", moq=moq, uom="EA", qty_available="500")

    assert moq_note(offer("50"), "500") == "MOQ 50"
    assert moq_note(offer(None)) == "" and moq_note(offer("Check page (see Flag)"), "1") == ""
    # Rows 28 and 30 of the test sheet (2026-10-21): 1 asked, MOQ 50, so 50 is quoted.
    assert moq_note(offer("50"), "1") == "MOQ 50, PRICED AT 50 (ASKED 1)"
    assert moq_note(offer("50"), "") == "MOQ 50, PRICED AT 50 (ASKED 1)"


# The client's example (2026-10-23): AS3491-01 at Proponent, stock in Brea, CA.
AS3491_BREAKS = [("1", "4", "15.64"), ("5", "10", "6.26"), ("11", "21", "3.45"),
                 ("22", "172", "2.15"), ("173", "", "1.06")]


@pytest.mark.asyncio
async def test_clients_price_break_example():
    deps, sheet = _offer_rows([Offer(
        part_number="P1", price="15.64", currency="USD", qty_available="7915", condition="NE",
        lead_time="In stock", price_breaks=AS3491_BREAKS,
        warehouses="Warehouse: Brea, CA\nQty on hand: 7915")], qty="50")
    await run(deps)
    [row] = deps.sink.rows
    assert row.internal_comments == (
        "• MOQ 22 FOR THIS PRICE BREAK, STK BREA, CA\n    ◦ BREA, CA: 7915 ON HAND\n"
        "• PRICE BREAKS:\n"
        "    ◦ 1-4 $15.64\n    ◦ 5-10 $6.26\n    ◦ 11-21 $3.45\n    ◦ 22-172 $2.15\n"
        "    ◦ 173+ $1.06")
    rfq = row.as_rfq_record()
    # the client's line: 7915 / 7915 at the price for the 50 asked
    assert (rfq["Quantity Requested"], rfq["Qty Quoted"], rfq["Unit Price"], rfq["Delivery"]) == (
        "7915", "7915", "2.15", "0")
    assert _results(sheet)[0]["Unit Price"] == "15.64"       # Results keeps one piece's price


@pytest.mark.asyncio
async def test_short_stock_is_quoted_at_what_the_site_has():
    deps, sheet = _offer_rows([Offer(
        part_number="P1", price="15.64", currency="USD", qty_available="8", condition="NE",
        price_breaks=AS3491_BREAKS)], qty="50")
    await run(deps)
    [row] = deps.sink.rows
    rfq = row.as_rfq_record()
    # client, 2026-10-23: the whole stock in both quantities, priced at the stock (8)
    assert (rfq["Quantity Requested"], rfq["Qty Quoted"], rfq["Unit Price"]) == ("8", "8", "6.26")
    assert row.internal_comments.startswith("• MOQ 5 FOR THIS PRICE BREAK\n")


@pytest.mark.asyncio
async def test_out_of_stock_line_keeps_price_and_condition_for_comparison():
    deps, sheet = _offer_rows([Offer(
        part_number="P1", price="15.64", currency="USD", qty_available="Out of stock",
        condition="NE", lead_time="59 days", price_breaks=AS3491_BREAKS)], qty="50")
    await run(deps)
    [row] = deps.sink.rows
    rfq = row.as_rfq_record()
    # Nothing due in: the customer's quantity in both (client, 2026-10-27).
    assert (rfq["Condition Quoted"], rfq["Type Quoted"], rfq["Quantity Requested"],
            rfq["Qty Quoted"], rfq["Unit Price"], rfq["Delivery"]) == (
        "NE", "Outright", "50", "50", "2.15", "59")
    assert _results(sheet)[0]["Unit Price"] == "15.64"


@pytest.mark.asyncio
async def test_clients_out_of_stock_examples_end_to_end():
    # 406Q80-1 (client, 2026-10-27): out of stock in the Netherlands, nothing due
    # in, 336 days -> 1 / 1, 387.04, Delivery 336, "336 DAY LEAD TIME, FOB: NETHERLANDS".
    deps, sheet = _offer_rows([Offer(
        part_number="P1", price="387.04", currency="USD", qty_available="Out of stock",
        condition="FN", lead_time="336 days",
        warehouses="Warehouse: Netherlands\nQty on hand: 0")], qty="1")
    await run(deps)
    [row] = deps.sink.rows
    rfq = row.as_rfq_record()
    assert (rfq["Quantity Requested"], rfq["Qty Quoted"], rfq["Unit Price"], rfq["Delivery"],
            rfq["Type Quoted"]) == ("1", "1", "387.04", "336", "Outright")
    assert row.internal_comments == "• 336 DAY LEAD TIME, FOB: NETHERLANDS"

    # 4140073-1: 75 due Dec 7, 2026 and 200 later -> 75 / 75, Delivery = days to Dec 7.
    deps, sheet = _offer_rows([Offer(
        part_number="P1", price="105.74", currency="USD", qty_available="Out of stock",
        condition="FN", lead_time="264 days",
        warehouses="Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Dec 7, 2026\n"
                   "Qty due in: 75\nNext day in: Jul 26, 2027\nQty due in: 200")], qty="1")
    await run(deps)
    [row] = deps.sink.rows
    rfq = row.as_rfq_record()
    days = (date(2026, 12, 7) - date.fromisoformat(row.fetched_at[:10])).days
    assert (rfq["Quantity Requested"], rfq["Qty Quoted"], rfq["Delivery"]) == (
        "75", "75", str(days) if days >= 0 else "264")
    assert "NO_DUE_DATE" not in row.flag


@pytest.mark.asyncio
async def test_rfq_tab_gets_the_same_rows_only_with_the_flag():
    from sourcing.sinks.sheet import SheetSink

    class Sheets:
        def __init__(self):
            self.results, self.rfq = [], []

        def append_results(self, records):
            self.results.extend(records)

        def append_rfq(self, records):
            self.rfq.extend(records)

    rows = [ResultRow(rfq_id="R1", vendor="Proponent", part_number="P1", requested_part="P1")]
    sheets = Sheets()
    await SheetSink(sheets).write(rows)
    assert len(sheets.results) == 1 and sheets.rfq == []
    await SheetSink(sheets, rfq=True).write(rows)
    assert len(sheets.results) == 2 and sheets.rfq[0]["PN Quoted"] == "P1"

    class Broken(Sheets):
        def append_rfq(self, records):
            raise RuntimeError("quota")
    out = await SheetSink(Broken(), rfq=True).write(rows)
    assert out[0].ok                          # Results saved: an RFQ failure doesn't fail it


@pytest.mark.asyncio
async def test_internal_comments_hold_stock_price_breaks_and_substitute_note():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "A1", "by": JANE, "qty": "300",
                                 "cond": "NE"}])
    sheet.rows[0].uom = "EA"
    written = []

    class Adapter(FakeAdapter):
        async def search(self, page, row):
            return SearchOutcome(found=True, offers=[
                Offer(part_number="A1", price="23.70", currency="USD", qty_available="45",
                      listing="requested", description="SEAL", uom="PC",
                      price_breaks=[("1", "249", "23.70"), ("250", "", "22.22")],
                      warehouses="Warehouse: Brea, CA\nQty on hand: 45\n"
                                 "Next day in: May 20, 2027\nQty due in: 200"),
                Offer(part_number="B2", qty_available="Out of stock", listing="substitute",
                      warehouses="Warehouse: UK\nQty on hand: 0")])

    class Sink(FakeSink):
        async def write(self, rows):
            written.extend(rows)
            return await super().write(rows)

    deps.adapter_factory = lambda vid, sel, cfg: Adapter(portal, selectors=sel)
    deps.sink = Sink(sheet)
    await run(deps)
    a1, b2 = written
    assert a1.internal_comments == (
        "• STK BREA, CA\n    ◦ BREA, CA: 45 ON HAND, NEXT IN MAY 20, 2027: 200\n"  # 45 quoted: 1-249 row
        "• PRICE BREAKS:\n    ◦ 1-249 $23.70\n    ◦ 250+ $22.22\n"
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH LEAD TIME: PN B2")   # B2 has no stock
    assert b2.internal_comments == "• OUT OF STOCK\n    ◦ UK: 0 ON HAND\n• APPROVED SUBSTITUTE FOR PN A1"
    assert _results(sheet)[0]["Warehouses"].startswith("Warehouse: Brea, CA")   # column kept
    # RFQ details: the team member's UOM wins, the vendor's description fills the gap
    assert (a1.uom, a1.part_description, a1.condition_wanted) == ("EA", "SEAL", "NE")


# Price breaks that start above 1 with no MOQ stated (client, 2026-10-21): the
# first row's start is the least the vendor sells, and that is quoted.
BREAKS_FROM_50 = [("50", "649", "0.27"), ("650", "1249", "0.15"), ("1250", "", "0.11")]


def test_price_breaks_starting_above_one_are_the_minimum():
    from sourcing.pipeline import flag_for, min_order, moq_note, qty_price

    o = Offer(part_number="P1", price="0.27", qty_available="138649", uom="EA",
              price_breaks=BREAKS_FROM_50)
    assert min_order(o) == (50, "breaks")
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                     qty_needed="10")
    assert moq_note(o, "10") == "MOQ 50, PRICED AT 50 (ASKED 10)"
    assert qty_price(o, row, "0.27") == "0.27"      # not "check page": 10 is in no row
    assert flag_for(o, row) == "BELOW_MOQ"
    # Asked within the table: the row's start is the MOQ for that price.
    assert moq_note(o, "700") == "MOQ 650 FOR THIS PRICE BREAK"
    assert min_order(Offer(part_number="P1")) == (None, "")


def test_stated_moq_wins_and_stock_under_the_minimum_is_short():
    from sourcing.pipeline import flag_for, min_order

    o = Offer(part_number="P1", price="0.27", qty_available="30", moq="50",
              price_breaks=BREAKS_FROM_50)
    assert min_order(o) == (50, "moq")
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                     qty_needed="1")
    assert flag_for(o, row) == "QTY_SHORT,BELOW_MOQ"


def test_stated_moq_below_the_first_price_break_is_the_minimum():
    # Monroe 2026-11-04: "Minimum order is 100", bulk rates from 500 -> MOQ 100,
    # priced at 100 with the unit price (no break row holds 100), not MOQ 500.
    from sourcing.pipeline import min_order, moq_note, quote_price
    o = Offer(part_number="MS24693-C274", price="0.27", qty_available="44350", moq="100",
              price_breaks=[("500", "999", "0.26"), ("1000", "2499", "0.25")])
    assert min_order(o) == (100, "moq")
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Monroe", part_number="MS24693-C274",
                     qty_needed="1")
    assert moq_note(o, "1") == "MOQ 100, PRICED AT 100 (ASKED 1)"
    assert quote_price(o, row, "0.27") == "0.27"
    assert quote_price(o, RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Monroe",
                                     part_number="MS24693-C274", qty_needed="600"),
                       "0.27") == "0.26"


@pytest.mark.asyncio
async def test_in_stock_line_without_a_condition_is_flagged():
    # client, 2026-10-23: an in-stock line must show its condition
    deps, sheet = _offer_rows([Offer(part_number="P1", price="2.00", qty_available="5"),
                               Offer(part_number="P1", price="2.00", qty_available="5",
                                     condition="FN"),
                               Offer(part_number="P1", price="2.00",
                                     qty_available="Out of stock")])
    await run(deps)
    assert [r["Flag"] for r in _results(sheet)] == ["NO_CONDITION", "", "QTY_SHORT"]


@pytest.mark.asyncio
async def test_substitute_without_stock_is_offered_with_lead_time():
    # 426EN10-8 / 426EN9-8 (client, 2026-10-27): A in stock, its substitute B out of
    # stock with a price and a lead time, no condition shown -> "WITH LEAD TIME", and
    # B is quoted (not No Quote), Condition left for a person (NO_CONDITION).
    deps, sheet = _offer_rows([
        Offer(part_number="P1", price="3,014.37", qty_available="10", condition="FN",
              listing="requested", warehouses="Warehouse: Brea, CA\nQty on hand: 10"),
        Offer(part_number="P9", price="3,650.89", qty_available="Out of stock",
              listing="substitute", lead_time="154 days",
              warehouses="Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Feb 26, 2027\n"
                         "Qty due in: 10"),
        Offer(part_number="P8", price="9.00", qty_available="4", condition="FN",
              listing="substitute")])
    await run(deps)
    p1, p9, p8 = deps.sink.rows
    assert p1.internal_comments.endswith(
        "• APPROVED SUBSTITUTE ALSO OFFERED: PN P8\n"
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH 154 DAY LEAD TIME: PN P9")
    rfq = p9.as_rfq_record()
    assert (rfq["Type Quoted"], rfq["Condition Quoted"], rfq["Condition Code"], rfq["Unit Price"],
            rfq["Qty Quoted"]) == ("Outright", "", "", "3650.89", "10")
    assert "NO_CONDITION" in p9.flag


def test_substitutes_without_stock_show_their_lead_time_days():
    # 2026-10-28: the days in the note; substitutes with the same days share a point,
    # one without a lead time keeps "WITH LEAD TIME".
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="A1")
    lines = _alt_lines(
        Offer(part_number="A1", price="5.00", qty_available="3", listing="requested"),
        Offer(part_number="B2", price="4.00", qty_available="Out of stock", listing="substitute"),
        Offer(part_number="C3", qty_available="Out of stock", listing="substitute"),
        Offer(part_number="D4", qty_available="Out of stock", listing="substitute"),
        Offer(part_number="E5", qty_available="Out of stock", listing="substitute"))
    for (o, r), lead in zip(lines[1:], ("146", "146", "21", "Not shown by Proponent")):
        r.lead_time = lead
    out = alternate_notes(row, lines)
    assert out[0].internal_comments == (
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH 146 DAY LEAD TIME: PN B2, PN C3\n"
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH 21 DAY LEAD TIME: PN D4\n"
        "• APPROVED SUBSTITUTE ALSO OFFERED WITH LEAD TIME: PN E5")


@pytest.mark.asyncio
async def test_unit_price_without_price_breaks_is_the_grid_price():
    deps, sheet = _offer_rows([Offer(part_number="P1", price="3,014.37", qty_available="10",
                                     condition="FN", qty_price="2,900.00")], qty="5")
    await run(deps)
    rfq = deps.sink.rows[0].as_rfq_record()
    assert (rfq["Quantity Requested"], rfq["Qty Quoted"], rfq["Unit Price"]) == ("10", "10", "3014.37")


# -- the same part asked on several rows (2026-10-30) ----------------------
# One search for the part, every row recorded from it, their lines together.

def _statuses(sheet):
    return [r.status for r in sheet.rows]


@pytest.mark.asyncio
async def test_a_part_asked_twice_is_bulk_searched_once_and_its_lines_written_together():
    deps, sheet, portal = _bulk_deps(["P1", "P2", "p1 ", "P3"], {"bulk_batch_size": 4})
    summary = await run(deps)
    assert BulkAdapter.bulk_calls == [["P1", "P2", "P3"]]
    assert [r.requested_part for r in deps.sink.rows] == ["P1", "p1 ", "P2", "P3"]
    assert _statuses(sheet) == ["DONE"] * 4
    assert summary["llm"]["calls"] == 1
    assert deps.store.rows["test-sheet__row4"]["same_search_as_row"] == 2


@pytest.mark.asyncio
async def test_a_part_asked_twice_is_searched_once_one_by_one():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P2", "P1")])
    await run(deps)
    assert portal.searches == ["P1", "P2"]
    assert [r.requested_part for r in deps.sink.rows] == ["P1", "P1", "P2"]
    assert _statuses(sheet) == ["DONE"] * 3


@pytest.mark.asyncio
async def test_each_row_of_a_shared_search_keeps_its_own_unit():
    # MS20470AD3-4 asked in EA and in LB: the EA lines go to the EA row only.
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P2", "P1")])
    sheet.rows[2].uom = "LB"
    await run(deps)
    assert portal.searches == ["P1", "P2"]
    assert [r.requested_part for r in deps.sink.rows] == ["P1", "P2"]
    assert _statuses(sheet) == ["DONE", "DONE", "NOT_FOUND"]
    assert "No LB lines" in sheet.notes[4]


@pytest.mark.asyncio
async def test_a_failed_search_fails_every_row_of_the_part():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P2", "P1")])
    portal.script["P1"] = ["error", "error"]     # the search and its retry
    await run(deps)
    assert portal.searches == ["P1", "P2", "P1"]
    assert _statuses(sheet) == ["FAILED", "DONE", "FAILED"]


@pytest.mark.asyncio
async def test_a_login_problem_reaches_every_row_of_the_part():
    secrets = {secret_key("proponent", JANE): {"username": "jane", "password": "WRONG"}}
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P1")], secrets=secrets)
    await run(deps)
    assert _statuses(sheet) == ["NO_CREDENTIALS"] * 2


def test_a_twin_gets_its_own_offers_priced_for_its_quantity():
    from sourcing.pipeline import twin_outcome

    searched = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                          qty_needed="1")
    twin = RequestRow(sheet_row=4, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                      qty_needed="50")
    outcome = SearchOutcome(found=True, offers=[Offer(
        part_number="P1", price="15.64", qty_available="7915", price_breaks=AS3491_BREAKS,
        qty_price="15.64")], usage=LlmUsage(calls=1, cost_usd=0.02))
    copy = twin_outcome(outcome, searched, twin)
    assert copy.offers[0].qty_price == "2.15" and outcome.offers[0].qty_price == "15.64"
    assert copy.usage.calls == 0 and copy.offers[0] is not outcome.offers[0]


def test_warehouses_holding_less_than_the_total_are_flagged():
    # Proponent 4066-0014, 2026-11-04: 22 in total, only Brea, CA's 17 read.
    from sourcing.pipeline import flag_for
    row = RequestRow(sheet_row=2, rfq_id="R", vendor_raw="Proponent", part_number="P1",
                     qty_needed="1")
    short = Offer(part_number="P1", price="1.00", qty_available="22", condition="NE",
                  warehouses="Warehouse: Brea, CA\nQty on hand: 17")
    assert "WAREHOUSES_SHORT" in flag_for(short, row)
    whole = Offer(part_number="P1", price="1.00", qty_available="22", condition="NE",
                  warehouses="Warehouse: Brea, CA\nQty on hand: 17\n\n"
                             "Warehouse: Singapore\nQty on hand: 5")
    assert "WAREHOUSES_SHORT" not in flag_for(whole, row)


@pytest.mark.asyncio
async def test_delivery_is_the_vendors_shipping_days_when_it_has_them():
    # Incora (client, 2026-11-06): in stock -> 7; No Quote -> none; out of stock keeps
    # its own rule (no lead time -> No Quote, not quoted at 7 days).
    deps, sheet = _offer_rows([
        Offer(part_number="P1", price="2.00", qty_available="12", condition="NE"),
        Offer(part_number="P1", price=None, qty_available="40", condition="NE"),
        Offer(part_number="P1", price="2.00", qty_available="Out of stock", condition="NE")])
    portal_factory = deps.adapter_factory

    def factory(vid, sel, cfg):
        adapter = portal_factory(vid, sel, cfg)
        adapter.config["delivery_days"] = "7"
        return adapter
    deps.adapter_factory = factory
    await run(deps)
    rfq = [r.as_rfq_record() for r in deps.sink.rows]
    assert [(r["Type Quoted"], r["Delivery"]) for r in rfq] == [
        ("Outright", "7"), ("No Quote", ""), ("No Quote", "")]


def test_incora_ships_in_7_days_by_default():
    from sourcing.vendors.incora import IncoraAdapter
    from sourcing.vendors.proponent import ProponentAdapter
    assert IncoraAdapter().delivery_days == "7"
    assert IncoraAdapter(vendor_config={"delivery_days": 5}).delivery_days == "5"
    assert ProponentAdapter().delivery_days == ""
