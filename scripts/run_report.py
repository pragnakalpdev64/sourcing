"""Per-part time, tokens and cost for one run, read from the local store
(STORE_BACKEND=file, .local_store.json).

  python scripts/run_report.py                 # latest run
  python scripts/run_report.py <run_id>        # a given run (from the log's "Run ... picked up")
  python scripts/run_report.py --csv out.csv   # also save the table as CSV

A row's record is overwritten when the same sheet row is run again, so report
a run before re-running its rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLUMNS = ["row", "part_number", "status", "offers", "seconds", "calls",
           "input_tokens", "output_tokens", "cost_usd", "error"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", nargs="?", help="default: the latest run")
    parser.add_argument("--store", default=os.path.join(HERE, ".local_store.json"))
    parser.add_argument("--csv", help="also write the per-part table to this CSV file")
    args = parser.parse_args()

    with open(args.store) as f:
        data = json.load(f)
    jobs = data.get("jobs", {})
    if not jobs:
        sys.exit("No runs in the store.")
    run_id = args.run_id or max(jobs, key=lambda j: jobs[j].get("started_at", 0))
    if run_id not in jobs:
        sys.exit(f"Run {run_id} not found. Runs: {', '.join(sorted(jobs))}")

    lines = []
    for key, rec in data.get("rows", {}).items():
        if rec.get("run_id") != run_id:
            continue
        llm = rec.get("llm") or {}
        lines.append({
            "row": int(key.rsplit("__row", 1)[-1]) if "__row" in key else key,
            "part_number": rec.get("part_number", ""),
            "status": rec.get("status", ""),
            "offers": rec.get("offers", ""),
            "seconds": rec.get("seconds", ""),
            "calls": llm.get("calls", 0),
            "input_tokens": llm.get("input_tokens", 0),
            "output_tokens": llm.get("output_tokens", 0),
            # bulk runs record each row's share of the batch cost
            "cost_usd": llm.get("cost_usd", llm.get("cost_usd_share", 0.0)),
            "error": rec.get("error") or "",
        })
    lines.sort(key=lambda r: (str(type(r["row"])), r["row"]))
    if not lines:
        sys.exit(f"No row records for run {run_id} (were its rows re-run since?).")

    print(f"Run {run_id}\n")
    print(f"{'Row':>4}  {'Part number':<20} {'Status':<10} {'Offers':>6} {'Secs':>6} "
          f"{'In tok':>7} {'Out tok':>7} {'Cost $':>8}")
    for r in lines:
        print(f"{r['row']:>4}  {r['part_number']:<20.20} {r['status']:<10.10} {r['offers']!s:>6} "
              f"{r['seconds']!s:>6} {r['input_tokens']:>7} {r['output_tokens']:>7} "
              f"{r['cost_usd']:>8.4f}" + (f"  {r['error'][:60]}" if r["error"] else ""))

    n = len(lines)
    secs = [r["seconds"] for r in lines if isinstance(r["seconds"], (int, float))]
    tin = sum(r["input_tokens"] for r in lines)
    tout = sum(r["output_tokens"] for r in lines)
    cost = sum(r["cost_usd"] for r in lines)
    job = jobs[run_id]
    wall = (job.get("finished_at") or 0) - (job.get("started_at") or 0)
    print(f"\nParts: {n}   statuses: {job.get('counts', {})}")
    print(f"Search time per part: avg {sum(secs) / len(secs):.1f}s, min {min(secs):.1f}s, "
          f"max {max(secs):.1f}s" if secs else "Search time per part: n/a")
    print(f"Tokens: {tin} in + {tout} out (avg {tin // n} in + {tout // n} out per part)")
    print(f"LLM cost: ${cost:.4f} total, ${cost / n:.4f} per part")
    if wall > 0:
        print(f"Whole run (login and sheet writes included): {wall:.0f}s, "
              f"{wall / n:.1f}s per part")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(lines)
        print(f"\nSaved {args.csv}")


if __name__ == "__main__":
    main()
