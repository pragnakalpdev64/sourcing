"""Add, show or change vendors in the vendor registry (store `vendors` records).

A vendor needs a record here plus its login secret (sourcing-vendor-<id>, made
by the client). Buyers then type its name, or an alias, in the sheet's Vendor
column. New vendors start disabled: enable one after its onboarding run has
been checked (docs/GENERIC_VENDOR_PLAN.md).

  python scripts/manage_vendors.py add wencor --name Wencor \\
      --login-url https://shop.wencor.com/login --hosts shop.wencor.com \\
      [--website https://www.wencor.com/] [--aliases "wencor group,wencor inc"]
  python scripts/manage_vendors.py list
  python scripts/manage_vendors.py show wencor
  python scripts/manage_vendors.py enable wencor        # or: disable wencor
  python scripts/manage_vendors.py set wencor currency=USD not_sold_phrases='["not sold"]'
  python scripts/manage_vendors.py export vendors.json  # records to a file (checked in)
  python scripts/manage_vendors.py import vendors.json  # seed a fresh store (e.g. Firestore
                                                        # on Cloud Run); existing records kept

Works on the store in .env (STORE_BACKEND): Firestore on Cloud Run, or
.local_store.json locally.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing.config import get_settings  # noqa: E402
from sourcing.credentials.base import SHARED, secret_key  # noqa: E402
from sourcing.models import slugify  # noqa: E402
from sourcing.store import make_store  # noqa: E402
from sourcing.vendors import ADAPTER_TYPES, ADAPTERS  # noqa: E402
from sourcing.vendors.base import check_login_url  # noqa: E402
from sourcing.vendors.registry import VendorRegistry  # noqa: E402


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def check_record(vendor_id: str, record: dict) -> list[str]:
    """Problems that would stop this record from working (empty = fine). The login
    URL and website must be https on one of the vendor's own hosts: credentials
    are only typed there."""
    built_in = ADAPTERS.get(vendor_id)
    hosts = record.get("allowed_hosts") or (built_in.allowed_hosts if built_in else ())
    problems = []
    if not hosts:
        problems.append("allowed_hosts is empty (--hosts): the sites where its login may be typed")
    for key in ("login_url", "website"):
        if record.get(key):
            problem = check_login_url(record[key], hosts)
            if problem:
                problems.append(f"{key}: {problem}")
    if not built_in and not (record.get("login_url") or record.get("website")):
        problems.append("no login_url or website: nowhere to start the login")
    adapter = record.get("adapter")
    if adapter and adapter not in ADAPTER_TYPES:
        problems.append(f"adapter '{adapter}' is not available yet "
                        f"(available: {', '.join(sorted(ADAPTER_TYPES))})")
    return problems


def _set_value(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# Run-time keys left out of an export: when a record was added is per store.
_NOT_EXPORTED = ("added_at",)


def _export(store, path: str) -> int:
    """Vendor records (not learned selectors) to a JSON file that can be checked in."""
    records = {vid: {k: v for k, v in rec.items() if k not in _NOT_EXPORTED}
               for vid, rec in sorted(store.list_vendors().items())}
    with open(path, "w") as f:
        json.dump(records, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Wrote {len(records)} vendor record(s) to {path}")
    return 0


def _import(store, path: str) -> int:
    """Add the file's records that the store doesn't have yet. Existing records are
    left alone (they may have been changed since), so this is safe to re-run."""
    with open(path) as f:
        records = json.load(f)
    existing = store.list_vendors()
    added = skipped = bad = 0
    for raw_id, record in records.items():
        vendor_id = slugify(raw_id)
        if vendor_id in existing:
            print(f"{vendor_id}: already in the store, kept as it is")
            skipped += 1
            continue
        problems = check_record(vendor_id, record)
        if problems:
            for problem in problems:
                print(f"{vendor_id}: PROBLEM: {problem}")
            bad += 1
            continue
        store.update_vendor(vendor_id, {**record, "added_at": time.time()})
        print(f"{vendor_id}: added (enabled={record.get('enabled', True) is not False})")
        added += 1
    print(f"Imported {added}, kept {skipped} existing, {bad} with problems")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["add", "list", "show", "enable", "disable", "set",
                                           "export", "import"])
    parser.add_argument("vendor_id", nargs="?", help="the vendor id; for export/import: the "
                                                     "JSON file (default vendors.json)")
    parser.add_argument("pairs", nargs="*", metavar="KEY=VALUE",
                        help="set: record keys (value read as JSON when it parses)")
    parser.add_argument("--name", help="add: display name (e.g. Wencor)")
    parser.add_argument("--login-url", help="add: the vendor's login page (https)")
    parser.add_argument("--website", help="add: a home page with a Login link, if no login URL")
    parser.add_argument("--hosts", help="add: comma-separated hosts where the login may be typed")
    parser.add_argument("--aliases", help="add: comma-separated other names team members may type")
    parser.add_argument("--adapter", help="add: adapter name (default: the built-in one for this "
                                          "id, else generic)")
    args = parser.parse_args(argv)

    s = get_settings()
    store = make_store(s.store_backend, s.gcp_project, s.store_file)

    if args.action == "list":
        records = store.list_vendors()
        for vendor_id in sorted(set(records) | {c.vendor_id for c in ADAPTERS.values()}):
            record = records.get(vendor_id)
            reg = VendorRegistry(store)
            state = ("enabled" if reg.enabled(vendor_id) else "DISABLED")
            kind = (f"adapter={record.get('adapter') or 'built-in'}" if record
                    else "built-in, no record")
            hosts = ",".join((record or {}).get("allowed_hosts") or []) or "-"
            aliases = ",".join((record or {}).get("aliases") or []) or "-"
            print(f"{vendor_id:16s} {reg.display_name(vendor_id):20s} {state:9s} {kind:24s} "
                  f"hosts={hosts}  aliases={aliases}")
        return 0

    if args.action in ("export", "import"):
        return _export(store, args.vendor_id or "vendors.json") if args.action == "export" \
            else _import(store, args.vendor_id or "vendors.json")

    if not args.vendor_id:
        parser.error(f"{args.action} needs a vendor id (see: list)")
    vendor_id = slugify(args.vendor_id)

    if args.action == "show":
        record = store.get_vendor(vendor_id)
        print(json.dumps({"vendor_id": vendor_id, "record": record,
                          "learned_selectors": store.get_selectors(vendor_id)},
                         indent=2, default=str))
        problems = check_record(vendor_id, record or {}) if record else []
        for problem in problems:
            print(f"PROBLEM: {problem}")
        return 1 if problems else 0

    if args.action == "add":
        if store.get_vendor(vendor_id) is not None:
            parser.error(f"{vendor_id} already has a record (use: set / show)")
        record = {
            "display_name": args.name or args.vendor_id,
            "aliases": _split(args.aliases),
            "enabled": False,
            "allowed_hosts": [h.lower() for h in _split(args.hosts)],
            "added_at": time.time(),
            # its first run's results are flagged NEW_VENDOR_CHECK (pipeline.py)
            "first_run_done": False,
        }
        if args.login_url:
            record["login_url"] = args.login_url
        if args.website:
            record["website"] = args.website
        adapter = args.adapter or (None if vendor_id in ADAPTERS else "generic")
        if adapter:
            record["adapter"] = adapter
        problems = check_record(vendor_id, record)
        if problems:
            for problem in problems:
                print(f"PROBLEM: {problem}")
            print("Nothing saved.")
            return 1
        store.update_vendor(vendor_id, record)
        print(f"{vendor_id}: added, DISABLED until checked.")
        print(f"Next: the client creates secret {secret_key(vendor_id, SHARED)}; then run the "
              f"onboarding run, check its offers, and: manage_vendors.py enable {vendor_id}")
        return 0

    if args.action in ("enable", "disable"):
        enabled = args.action == "enable"
        record = store.get_vendor(vendor_id)
        if enabled and record is not None:
            problems = check_record(vendor_id, record)
            if problems:
                for problem in problems:
                    print(f"PROBLEM: {problem}")
                print("Not enabled.")
                return 1
        store.update_vendor(vendor_id, {"enabled": enabled})
        print(f"{vendor_id}: enabled={enabled}")
        return 0

    # set
    if not args.pairs:
        parser.error("set needs KEY=VALUE pairs")
    fields = {}
    for pair in args.pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            parser.error(f"{pair!r}: expected KEY=VALUE")
        fields[key.strip()] = _set_value(raw)
    merged = {**(store.get_vendor(vendor_id) or {}), **fields}
    problems = check_record(vendor_id, merged)
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}")
        print("Nothing saved.")
        return 1
    store.update_vendor(vendor_id, fields)
    print(f"{vendor_id}: set {', '.join(fields)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
