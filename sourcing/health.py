"""Detect when a vendor's pages change shape, so a person can re-check and
redesign the adapter before wrong or missing data piles up.

The pipeline already produces every signal we need: whether the structured
results grid could be read or we fell back to page text, whether the extractor
recognised the page, how many offer values failed verification (`Check page`),
how many parts came back empty, and how many rows needed attention. This module
turns one run's per-vendor counters into rates, compares them with that vendor's
own recent runs (kept in the store), and returns plain-language warnings when a
run deviates far enough to suggest the pages changed.

It only warns. Nothing here changes extraction, login or which vendors run
(prototype stage; the client's rule is to stop and tell a person, never to work
around a change on our own). The fix a warning points to is to re-run
`scripts/onboard_vendor.py <vendor> --fresh` and compare the offers with the
portal, then update the adapter if the page really moved.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

# A run is compared only against a vendor with at least this many prior runs, and
# only when it searched at least this many parts: too little history or too few
# parts makes a rate swing on noise, not a page change.
MIN_HISTORY = 3
MIN_PARTS = 3

# The extractor's page_kind values that mean "I couldn't read a normal results page".
BAD_PAGE_KINDS = ("error", "login_page", "other")

# Fields a run contributes to its vendor's health history. Counters, not rates:
# rates are derived here so the thresholds live in one place.
METRIC_FIELDS = (
    "parts", "searched", "grid_reads", "grid_fallback", "page_reads", "bad_page_kind",
    "offer_total", "check_page_offers", "not_found", "attention",
)


def _safe(n: float, d: float) -> Optional[float]:
    """n / d, or None when the denominator is 0 (the rate is unknown, not 0)."""
    return (n / d) if d else None


def rates(metrics: dict) -> dict:
    """The five health rates for one run's counters. A rate is None when nothing
    fed it (e.g. grid_fallback_rate when the vendor reads no grid)."""
    return {
        # Structural: the results grid was expected but could not be read.
        "grid_fallback_rate": _safe(metrics.get("grid_fallback", 0), metrics.get("grid_reads", 0)),
        # Structural: the extractor didn't recognise the results page.
        "bad_page_kind_rate": _safe(metrics.get("bad_page_kind", 0), metrics.get("page_reads", 0)),
        # Offer values the extractor gave but verification couldn't find on the page.
        "check_page_rate": _safe(metrics.get("check_page_offers", 0), metrics.get("offer_total", 0)),
        # Parts that came back with nothing (NOT_FOUND / NOT_SOLD) among those searched.
        "not_found_rate": _safe(metrics.get("not_found", 0), metrics.get("searched", 0)),
        # Rows that needed a person (a broken login or flow shows up here).
        "attention_rate": _safe(metrics.get("attention", 0), metrics.get("parts", 0)),
    }


def _baseline(history: list[dict], rate_name: str) -> Optional[float]:
    """Median of a rate over prior runs that had a value for it, or None."""
    values = [r[rate_name] for h in history if (r := rates(h))[rate_name] is not None]
    return median(values) if values else None


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def evaluate(current: dict, history: list[dict]) -> list[str]:
    """Warnings that one run's pages may have changed, judged against the vendor's
    recent runs. Empty when the run looks normal, or when there isn't enough
    history or the run was too small to judge.

    `current` and each item of `history` are counter dicts (METRIC_FIELDS);
    `history` is the vendor's prior runs, this run excluded."""
    if current.get("parts", 0) < MIN_PARTS or len(history) < MIN_HISTORY:
        return []

    now = rates(current)
    base = {name: _baseline(history, name) for name in now}
    alarms: list[str] = []

    # Results grid lost: it used to read almost always, now it rarely does.
    gf, gf_base = now["grid_fallback_rate"], base["grid_fallback_rate"]
    if gf is not None and gf_base is not None and gf_base < 0.2 and gf >= 0.5:
        alarms.append(f"results grid not read on {_pct(gf)} of searches "
                      f"(usually {_pct(gf_base)}) — the results page may have changed")

    # Extractor no longer recognises the results page.
    bp = now["bad_page_kind_rate"]
    if bp is not None and bp >= 0.5:
        alarms.append(f"the extractor didn't recognise {_pct(bp)} of result pages")

    # Far more parts come back empty than usual.
    nf, nf_base = now["not_found_rate"], base["not_found_rate"]
    if nf is not None and nf_base is not None and nf - nf_base > 0.4:
        alarms.append(f"{_pct(nf)} of parts returned nothing (usually {_pct(nf_base)}) — "
                      f"search or results reading may have broken")

    # Offer values increasingly fail verification against the page.
    cp, cp_base = now["check_page_rate"], base["check_page_rate"]
    if cp is not None and cp > 0.5 and (cp_base is None or cp - cp_base > 0.3):
        alarms.append(f"{_pct(cp)} of offers had values that couldn't be verified against the "
                      f"page (usually {_pct(cp_base)}) — the results layout may have moved")

    # Many more rows need a person than usual (a login or flow change).
    at, at_base = now["attention_rate"], base["attention_rate"]
    if at is not None and at_base is not None and at - at_base > 0.4:
        alarms.append(f"{_pct(at)} of rows needed attention (usually {_pct(at_base)}) — "
                      f"the login or search flow may have changed")

    return alarms
