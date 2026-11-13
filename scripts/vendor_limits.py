"""Show and change a vendor's login / search limits (sourcing/limits.py), and
reset an account after a person has checked it.

The limits live in the vendor record (`vendors/<id>` -> `limits`) of the store
in .env: `.local_store.json` locally (STORE_BACKEND=file), Firestore on Cloud
Run. A vendor without them runs on the defaults, which the first run saves
there marked unconfirmed.

  python scripts/vendor_limits.py list
  python scripts/vendor_limits.py show jamaica_bearings [--owner jane@skylinkintl.com]
  python scripts/vendor_limits.py set jamaica_bearings vendor_lockout_after=1 login_safety_margin=0
  python scripts/vendor_limits.py unset jamaica_bearings max_searches_per_day   # back to the default
  python scripts/vendor_limits.py reset-login jamaica_bearings [--owner jane@skylinkintl.com] [--today]
  python scripts/vendor_limits.py resume jamaica_bearings      # end a "too many requests" pause

`set` checks every key and value (a typo is refused) and marks the limits
confirmed. Every change is written to the audit log.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing.config import get_settings  # noqa: E402
from sourcing.limits import DEFAULT_LIMITS, day_key, effective_limits, validate  # noqa: E402
from sourcing.models import SHARED, AccountState, account_id  # noqa: E402
from sourcing.store import make_store  # noqa: E402
from sourcing.vendors import adapter_class  # noqa: E402
from sourcing.vendors.registry import VendorRegistry  # noqa: E402


def _ts(value) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ") if value else "-"


def _vendor_id(store, name: str) -> str:
    """As the worker reads the Vendor column: record id, alias or built-in name."""
    return VendorRegistry(store).resolve_id(name)


def _adapter_defaults(vendor_id: str, record: dict) -> dict:
    cls = adapter_class(vendor_id, record)
    return dict(getattr(cls, "default_limits", {}) or {})


def _show(store, vendor_id: str, owner: str) -> None:
    record = store.get_vendor(vendor_id) or {}
    stored = record.get("limits") if isinstance(record.get("limits"), dict) else {}
    adapter = _adapter_defaults(vendor_id, record)
    limits, _ = effective_limits(record, adapter)
    if not isinstance(record.get("limits"), dict):
        status = "none set: runs on the defaults"
    elif record.get("limits_confirmed"):
        status = f"confirmed ({_ts(record.get('limits_updated_at'))} by {record.get('limits_updated_by', '?')})"
    else:
        status = f"UNCONFIRMED defaults saved {_ts(record.get('limits_saved_at'))}"
    print(f"{vendor_id}: limits {status}")
    for key in DEFAULT_LIMITS:
        source = "vendor" if key in stored else "adapter" if key in adapter else "default"
        print(f"  {key:30s} {limits[key]!s:20s} ({source})")
    usage = store.get_vendor_usage(vendor_id) or {}
    today = day_key(time.time(), limits)
    searches = usage.get("searches_today", 0) if usage.get("day") == today else 0
    print(f"  searches today ({today}): {searches}/{limits['max_searches_per_day']}, "
          f"last request {_ts(usage.get('last_request_at'))}, "
          f"paused until {_ts(usage.get('backoff_until')) if float(usage.get('backoff_until') or 0) > time.time() else '-'}")
    aid = account_id(vendor_id, owner)
    doc = store.get_account(aid)
    if doc:
        logins = doc.get("logins_today", 0) if doc.get("logins_day") == today else 0
        print(f"  account {aid}: state {doc.get('state', '-')}, logins today {logins}/"
              f"{limits['max_logins_per_day']}, failed logins kept "
              f"{len(doc.get('login_failures') or [])} (last {_ts(doc.get('last_failure_at'))})")
    else:
        print(f"  account {aid}: no record yet")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["list", "show", "set", "unset", "reset-login", "resume"])
    parser.add_argument("vendor", nargs="?")
    parser.add_argument("values", nargs="*", help="set: KEY=VALUE ...; unset: KEY ...")
    parser.add_argument("--owner", default=SHARED,
                        help="account owner email for show / reset-login (default: shared login)")
    parser.add_argument("--by", default=getpass.getuser(), help="who made the change (audit)")
    parser.add_argument("--today", action="store_true",
                        help="reset-login: also set today's login count back to 0 "
                             "(max_logins_per_day), e.g. after logins spent on tests")
    args = parser.parse_args()

    s = get_settings()
    store = make_store(s.store_backend, s.store_project, s.store_file)

    if args.action == "list":
        vendors = store.list_vendors()
        if not vendors:
            print("No vendor records yet (a vendor's first run creates one).")
        for vendor_id, record in sorted(vendors.items()):
            limits = record.get("limits")
            status = ("none" if not isinstance(limits, dict) else
                      "confirmed" if record.get("limits_confirmed") else "UNCONFIRMED defaults")
            print(f"{vendor_id:30s} limits: {status}")
        return 0

    if not args.vendor:
        parser.error(f"{args.action} needs a vendor (see: list)")
    vendor_id = _vendor_id(store, args.vendor)
    owner = args.owner.strip().lower() if args.owner != SHARED else SHARED

    if args.action == "show":
        _show(store, vendor_id, owner)
        return 0

    if args.action in ("set", "unset"):
        if not args.values:
            parser.error(f"{args.action} needs at least one "
                         f"{'KEY=VALUE' if args.action == 'set' else 'KEY'}")
        record = store.get_vendor(vendor_id) or {}
        limits = dict(record.get("limits") or {})
        changes = {}
        for item in args.values:
            if args.action == "set":
                key, sep, raw = item.partition("=")
                if not sep:
                    parser.error(f"{item!r}: expected KEY=VALUE")
                try:
                    value = validate(key.strip(), raw.strip())
                except (ValueError, TypeError) as exc:
                    parser.error(f"{item!r}: {exc}")
                changes[key.strip()] = (limits.get(key.strip()), value)
                limits[key.strip()] = value
            else:
                if item not in DEFAULT_LIMITS:
                    parser.error(f"{item!r}: unknown limit")
                changes[item] = (limits.pop(item, None), "default")
        store.update_vendor(vendor_id, {
            "limits": limits, "limits_confirmed": True,
            "limits_updated_at": time.time(), "limits_updated_by": args.by})
        store.audit("vendor_limits_changed", vendor_id=vendor_id, by=args.by,
                    changes={k: {"old": o, "new": n} for k, (o, n) in changes.items()})
        for key, (old, new) in changes.items():
            print(f"{vendor_id}: {key}: {old} -> {new}")
        _show(store, vendor_id, owner)
        return 0

    if args.action == "reset-login":
        aid = account_id(vendor_id, owner)
        doc = store.get_account(aid)
        if not doc:
            print(f"{aid}: no account record, nothing to reset")
            return 1
        fields = {"login_failures": [], "last_failure_at": None,
                  "consecutive_login_failures": 0, "last_error": None,
                  "state": AccountState.NEEDS_LOGIN.value}
        if args.today:
            fields["logins_today"] = 0
        store.update_account(aid, fields)
        store.audit("login_reset", account_id=aid, by=args.by, old_state=doc.get("state"),
                    old_logins_today=doc.get("logins_today") if args.today else None)
        print(f"{aid}: failed logins cleared, state {doc.get('state')} -> NEEDS_LOGIN "
              "(the next run logs in)"
              + (f"; today's logins {doc.get('logins_today') or 0} -> 0" if args.today else ""))
        return 0

    # resume
    store.update_vendor_usage(vendor_id, {"backoff_until": None})
    store.audit("vendor_resumed", vendor_id=vendor_id, by=args.by)
    print(f"{vendor_id}: pause cleared; the next run may search it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
