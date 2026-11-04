"""Per-vendor page-health history and whether the latest run looks like the
vendor's pages changed (sourcing/health.py), read from the local store
(STORE_BACKEND=file, .local_store.json).

  python scripts/check_page_health.py                 # every vendor
  python scripts/check_page_health.py proponent       # one vendor

Each row is one run's five health rates; the last row is the newest run. A
`⚠` line below a vendor means the newest run deviated far enough from its own
recent runs to suggest the pages moved. This reads the stored history only; the
same check runs automatically at the end of every pipeline run and warns in the
Chat message.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from sourcing import health      # noqa: E402

RATES = ["grid_fallback_rate", "bad_page_kind_rate", "check_page_rate",
         "not_found_rate", "attention_rate"]
HEADS = ["grid fb", "bad kind", "check pg", "not fnd", "attn"]


def _pct(value) -> str:
    return "   -" if value is None else f"{round(value * 100):>3}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("vendor", nargs="?", help="default: every vendor with history")
    parser.add_argument("--store", default=os.path.join(HERE, ".local_store.json"))
    args = parser.parse_args()

    with open(args.store) as f:
        data = json.load(f)
    all_health = data.get("vendor_health", {})
    if not all_health:
        sys.exit("No vendor health recorded yet (run the pipeline against a file store first).")
    vendors = [args.vendor] if args.vendor else sorted(all_health)

    for vendor_id in vendors:
        runs = all_health.get(vendor_id) or []
        if not runs:
            print(f"{vendor_id}: no history\n")
            continue
        print(f"{vendor_id}  ({len(runs)} run(s))")
        print(f"  {'run':<16} {'parts':>5}  " + " ".join(f"{h:>8}" for h in HEADS))
        for rec in runs:
            r = health.rates(rec)
            print(f"  {str(rec.get('run_id', ''))[:16]:<16} {rec.get('parts', 0):>5}  "
                  + " ".join(f"{_pct(r[name]):>8}" for name in RATES))
        # The newest run judged against the ones before it (as the pipeline does).
        warnings = health.evaluate(runs[-1], runs[:-1])
        if warnings:
            print("  ⚠ possible page change:")
            for line in warnings:
                print(f"    - {line}")
        print()


if __name__ == "__main__":
    main()
