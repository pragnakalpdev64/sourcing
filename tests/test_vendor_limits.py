"""Per-vendor request limits (sourcing/limits.py) and how the pipeline uses them."""
from __future__ import annotations

import time

import pytest

from sourcing.limits import (
    DEFAULT_LIMITS, LimitReached, VendorLimiter, day_key, effective_limits, in_allowed_hours,
    validate,
)
from sourcing.store import MemoryStore

from .fakes import FakeAdapter
from .test_pipeline import JANE, make, run


class _Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def _limits(**values):
    return effective_limits({"limits": values})[0]


# --- the dictionary ----------------------------------------------------------

def test_limits_layer_defaults_adapter_record_and_run_override():
    limits, set_for_vendor = effective_limits(
        {"limits": {"max_searches_per_day": 100}, "limits.max_searches_per_day": "20"},
        {"min_seconds_between_requests": 0})
    assert set_for_vendor
    assert limits["max_searches_per_day"] == 20                 # this run's --set wins
    assert limits["min_seconds_between_requests"] == 0          # the adapter's own default
    assert limits["vendor_lockout_after"] == DEFAULT_LIMITS["vendor_lockout_after"]
    assert effective_limits({})[1] is False                     # no record: defaults only


def test_a_bad_stored_value_falls_back_to_the_default():
    limits, _ = effective_limits({"limits": {"max_searches_per_day": "lots", "bogus": 1}})
    assert limits["max_searches_per_day"] == DEFAULT_LIMITS["max_searches_per_day"]


@pytest.mark.parametrize("key, value", [
    ("max_logins_per_day", "-1"), ("vendor_lockout_after", 0), ("count_bulk_as", "pages"),
    ("allowed_hours", "6pm-8am"), ("timezone", "Mars/Base"), ("max_login_per_day", 3),
])
def test_validate_rejects_typos_and_bad_values(key, value):
    with pytest.raises(ValueError):
        validate(key, value)


def test_allowed_hours_over_midnight():
    limits = _limits(allowed_hours="18:00-08:00", timezone="UTC")
    at = lambda hhmm: time.mktime(time.strptime(f"2026-10-21 {hhmm}", "%Y-%m-%d %H:%M")) \
        - time.timezone                                         # as UTC
    assert in_allowed_hours(at("19:30"), limits) and in_allowed_hours(at("07:59"), limits)
    assert not in_allowed_hours(at("12:00"), limits)


# --- the limiter ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_searches_wait_for_the_gap_and_stop_at_the_run_cap():
    clock = _Clock()
    limiter = VendorLimiter(MemoryStore(), clock=clock, sleep=clock.sleep)
    limits = _limits(min_seconds_between_requests=5, max_searches_per_run=2)
    await limiter.before_request("v", limits)
    clock.now += 2                                              # the search took 2s
    await limiter.before_request("v", limits)
    assert clock.slept == [3.0]                                 # 5s from the last start
    with pytest.raises(LimitReached, match="one run"):
        await limiter.before_request("v", limits)


@pytest.mark.asyncio
async def test_the_daily_cap_survives_a_new_run_and_resets_the_next_day():
    store, clock = MemoryStore(), _Clock()
    limits = _limits(min_seconds_between_requests=0, max_searches_per_day=2)
    for _ in range(2):
        await VendorLimiter(store, clock=clock, sleep=clock.sleep).before_request("v", limits)
    later_run = VendorLimiter(store, clock=clock, sleep=clock.sleep)
    assert "daily search limit" in later_run.block_reason("v", limits)
    clock.now += 86_400
    assert later_run.block_reason("v", limits) is None


@pytest.mark.asyncio
async def test_a_bulk_search_counts_once_or_per_part():
    store, clock = MemoryStore(), _Clock()
    for how, counted in (("search", 1), ("parts", 25)):
        limiter = VendorLimiter(store, clock=clock, sleep=clock.sleep)
        limits = _limits(min_seconds_between_requests=0, count_bulk_as=how)
        before = limiter.searches_today(how, limits)
        await limiter.before_request(how, limits, parts=25)
        assert limiter.searches_today(how, limits) - before == counted


def test_back_off_pauses_the_vendor():
    clock = _Clock()
    limiter = VendorLimiter(MemoryStore(), clock=clock, sleep=clock.sleep)
    limits = _limits(backoff_minutes=30)
    limiter.back_off("v", limits)
    assert "slow down" in limiter.block_reason("v", limits)
    clock.now += 31 * 60
    assert limiter.block_reason("v", limits) is None


# --- in the pipeline -------------------------------------------------------------

def _with_vendor_config(deps, portal):
    """Like make(), but the fake adapter gets the stored vendor record."""
    deps.adapter_factory = (lambda vid, sel, cfg: FakeAdapter(portal, selectors=sel,
                                                              vendor_config=cfg)
                            if vid == "proponent" else None)
    return deps


@pytest.mark.asyncio
async def test_parts_over_the_run_cap_wait_as_new_with_a_note():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P2", "P3")])
    deps.store.update_vendor("proponent", {"limits": {"max_searches_per_run": 2,
                                                      "min_seconds_between_requests": 0}})
    await run(_with_vendor_config(deps, portal))
    assert [sheet.status_of(p) for p in ("P1", "P2", "P3")] == ["DONE", "DONE", "NEW"]
    assert "search limit for one run reached" in sheet.notes[4]
    assert any("waiting" in text for _, text in deps.notifier.messages)


@pytest.mark.asyncio
async def test_a_vendor_over_its_daily_cap_is_not_even_logged_in():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    limits = {"max_searches_per_day": 5, "min_seconds_between_requests": 0}
    deps.store.update_vendor("proponent", {"limits": limits})
    deps.store.update_vendor_usage("proponent", {
        "day": day_key(time.time(), _limits(**limits)), "searches_today": 5})
    await run(_with_vendor_config(deps, portal))
    assert sheet.status_of("P1") == "NEW" and "daily search limit" in sheet.notes[2]
    assert portal.logins == [] and portal.searches == []
    # Only the once-a-day limit message, no run summary for a run that did nothing.
    assert not [t for _, t in deps.notifier.messages if "run finished" in t]


@pytest.mark.asyncio
async def test_a_rate_limit_page_pauses_the_vendor_and_keeps_the_rest_waiting():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": p, "by": JANE}
                                for p in ("P1", "P2")])
    deps.store.update_vendor("proponent", {"limits": {"min_seconds_between_requests": 0}})
    portal.script = {"P1": ["rate_limited"]}
    await run(_with_vendor_config(deps, portal))
    assert [sheet.status_of(p) for p in ("P1", "P2")] == ["NEW", "NEW"]
    assert "too many requests" in sheet.notes[2]
    assert portal.searches == ["P1"]                            # nothing after the warning
    assert float(deps.store.get_vendor_usage("proponent")["backoff_until"]) > time.time()


@pytest.mark.asyncio
async def test_a_new_vendor_runs_on_the_defaults_and_they_are_saved_once():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    await run(_with_vendor_config(deps, portal))
    assert sheet.status_of("P1") == "DONE"
    record = deps.store.get_vendor("proponent")
    # Empty and unconfirmed: it keeps following the defaults until a person sets its own.
    assert record["limits"] == {} and record["limits_confirmed"] is False
    asked = [t for _, t in deps.notifier.messages if "no login/search limits set" in t]
    assert len(asked) == 1

    # Next run: the saved limits are used and nobody is asked again.
    deps2, sheet2, portal2 = make([{"vendor": "Proponent", "part": "P2", "by": JANE}],
                                  store=deps.store)
    await run(_with_vendor_config(deps2, portal2), run_id="r2")
    assert not [t for _, t in deps2.notifier.messages if "no login/search limits set" in t]


@pytest.mark.asyncio
async def test_a_dry_run_adapter_never_writes_the_vendors_limits():
    from sourcing.vendors.dummy import DummyAdapter

    deps, sheet, _ = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.adapter_factory = lambda vid, sel, cfg: DummyAdapter(selectors=sel, vendor_config=cfg)
    await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert deps.store.get_vendor("proponent") is None
    assert not [t for _, t in deps.notifier.messages if "no login/search limits set" in t]
