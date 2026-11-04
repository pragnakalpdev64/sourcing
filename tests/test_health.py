"""Page-change detector (sourcing/health.py): the rates, the alarm rules, and
the per-vendor history the store keeps for it."""
from __future__ import annotations

from sourcing import health
from sourcing.store import MemoryStore


def metrics(*, parts, grid_reads=0, grid_fallback=0, page_reads=0, bad_page_kind=0,
            offer_total=0, check_page_offers=0, not_found=0, attention=0):
    """One run's counters; `searched` is the parts that reached a results read."""
    return {
        "parts": parts, "searched": parts, "grid_reads": grid_reads,
        "grid_fallback": grid_fallback, "page_reads": page_reads, "bad_page_kind": bad_page_kind,
        "offer_total": offer_total, "check_page_offers": check_page_offers,
        "not_found": not_found, "attention": attention,
    }


def healthy(parts=10):
    """A normal run: grid read every time, pages recognised, few empties."""
    return metrics(parts=parts, grid_reads=parts, page_reads=parts, offer_total=parts,
                   not_found=1)


# --- rates -----------------------------------------------------------------

def test_rate_is_none_when_nothing_feeds_it():
    r = health.rates(metrics(parts=5))
    assert r["grid_fallback_rate"] is None       # no grid reads
    assert r["check_page_rate"] is None           # no offers


def test_rates_are_fractions():
    r = health.rates(metrics(parts=10, grid_reads=10, grid_fallback=5,
                             offer_total=4, check_page_offers=2, not_found=3))
    assert r["grid_fallback_rate"] == 0.5
    assert r["check_page_rate"] == 0.5
    assert r["not_found_rate"] == 0.3


# --- guards ----------------------------------------------------------------

def test_silent_without_enough_history():
    current = metrics(parts=10, grid_reads=10, grid_fallback=10)
    assert health.evaluate(current, [healthy(), healthy()]) == []   # only 2 prior runs


def test_silent_on_a_tiny_run():
    current = metrics(parts=2, grid_reads=2, grid_fallback=2)
    assert health.evaluate(current, [healthy()] * 5) == []          # < MIN_PARTS


def test_healthy_run_raises_nothing():
    assert health.evaluate(healthy(), [healthy()] * 5) == []


# --- each alarm ------------------------------------------------------------

def test_grid_lost():
    current = metrics(parts=10, grid_reads=10, grid_fallback=8, page_reads=10, offer_total=10)
    lines = health.evaluate(current, [healthy()] * 5)
    assert any("grid" in line for line in lines)


def test_bad_page_kind():
    current = metrics(parts=10, grid_reads=10, page_reads=10, bad_page_kind=6)
    lines = health.evaluate(current, [healthy()] * 5)
    assert any("recognise" in line for line in lines)


def test_not_found_spike():
    current = metrics(parts=10, grid_reads=10, page_reads=10, offer_total=2, not_found=9)
    lines = health.evaluate(current, [healthy()] * 5)
    assert any("nothing" in line for line in lines)


def test_check_page_spike():
    current = metrics(parts=10, grid_reads=10, page_reads=10, offer_total=10, check_page_offers=7)
    lines = health.evaluate(current, [healthy()] * 5)
    assert any("verified" in line for line in lines)


def test_attention_spike():
    current = metrics(parts=10, grid_reads=10, page_reads=10, offer_total=10, attention=6)
    lines = health.evaluate(current, [healthy()] * 5)
    assert any("attention" in line for line in lines)


def test_grid_fallback_does_not_alarm_a_page_text_vendor():
    # A vendor that never reads a grid: fallback is the norm, so no alarm.
    base = metrics(parts=10, grid_reads=10, grid_fallback=10, page_reads=10, offer_total=10,
                   not_found=1)
    current = metrics(parts=10, grid_reads=10, grid_fallback=10, page_reads=10, offer_total=10,
                      not_found=1)
    assert health.evaluate(current, [base] * 5) == []


# --- store -----------------------------------------------------------------

def test_store_keeps_capped_history_newest_last():
    store = MemoryStore()
    for i in range(5):
        store.append_vendor_health("proponent", {"run_id": f"r{i}", "parts": i}, keep=3)
    runs = store.get_vendor_health("proponent")
    assert [r["run_id"] for r in runs] == ["r2", "r3", "r4"]
    assert store.get_vendor_health("unknown") == []
