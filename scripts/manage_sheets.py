"""Add, list or disable sheets in the registry (sourcing.store sheets collection).

The worker processes every enabled sheet here (plus SHEET_ID), so this is how a
new sheet joins, or one is turned off, without a redeploy.

  python scripts/manage_sheets.py add <sheet_id> [--label "Team A"]
  python scripts/manage_sheets.py list
  python scripts/manage_sheets.py disable <sheet_id>
  python scripts/manage_sheets.py enable <sheet_id>
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing.config import get_settings  # noqa: E402
from sourcing.store import make_store  # noqa: E402


def _ts(value) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if value else "-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["add", "list", "enable", "disable"])
    parser.add_argument("sheet_id", nargs="?")
    parser.add_argument("--label", help="add: a name to recognise the sheet by")
    parser.add_argument("--requests-tab", help="add: default Requests")
    parser.add_argument("--results-tab", help="add: default Results")
    args = parser.parse_args()

    s = get_settings()
    store = make_store(s.store_backend, s.store_project, s.store_file)

    if args.action == "list":
        sheets = store.list_enabled_sheets(enabled_only=False)
        if not sheets:
            print("No sheets registered.")
            return 0
        for sheet in sheets:
            print(f"{sheet['sheet_id']}  {sheet.get('label', ''):30s}  "
                 f"tabs={sheet.get('requests_tab')}/{sheet.get('results_tab')}  "
                 f"last_seen={_ts(sheet.get('last_seen_at'))}")
        return 0

    if not args.sheet_id:
        parser.error(f"{args.action} needs a sheet_id (see: list)")
    if args.action == "add":
        store.register_sheet(args.sheet_id, {"label": args.label,
                                             "requests_tab": args.requests_tab,
                                             "results_tab": args.results_tab})
        print(f"{args.sheet_id}: registered (share it with the runtime service account)")
        return 0
    store.set_sheet_enabled(args.sheet_id, args.action == "enable")
    print(f"{args.sheet_id}: enabled={args.action == 'enable'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
