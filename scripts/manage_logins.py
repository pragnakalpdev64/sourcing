"""List the vendor logins the login update page knows, add one, and choose who
may change a shared login (docs/CREDENTIAL_UPDATE_UI_PLAN.md).

Our account can't list secrets, so the page offers the logins in the store's
account records. The worker adds a record the first time it uses a login; add
one here before that. A person may always change their own login; a shared
login only its editors and the admins (CREDENTIAL_ADMINS).

  python scripts/manage_logins.py list
  python scripts/manage_logins.py add proponent --owner shared [--editors a@skylink.com,b@skylink.com]
  python scripts/manage_logins.py add proponent --owner jane@skylink.com
  python scripts/manage_logins.py add ils --owner shared   # no vendor record: needs its secret
  python scripts/manage_logins.py grant proponent shared a@skylink.com,b@skylink.com
  python scripts/manage_logins.py revoke proponent shared b@skylink.com

The secret itself (sourcing-vendor-<vendor>[__<email>]) is created by an admin
(on /credentials/new or with gcloud); this only lists it. The page's "Find a
login" does the same for one vendor. Works on the store in .env (STORE_BACKEND).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing.config import get_settings  # noqa: E402
from sourcing.credentials.access import all_logins, login_from_record  # noqa: E402
from sourcing.credentials.base import secret_key  # noqa: E402
from sourcing.models import SHARED, account_id, slugify  # noqa: E402
from sourcing.store import make_store  # noqa: E402
from sourcing.vendors import adapter_class, canonical_vendor_id  # noqa: E402


def _emails(value: str) -> list[str]:
    out = [v.strip().lower() for v in (value or "").split(",") if v.strip()]
    bad = [e for e in out if "@" not in e]
    if bad:
        sys.exit(f"Not an email: {', '.join(bad)}")
    return out


def _owner(value: str) -> str:
    value = value.strip().lower()
    if value in ("shared", SHARED):
        return SHARED
    if "@" not in value:
        sys.exit(f"--owner must be 'shared' or an email, not '{value}'")
    return value


def _secret_exists(vendor_id: str, owner: str) -> bool:
    import asyncio

    from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides

    s = get_settings()
    provider = SecretManagerProvider(s.gcp_project, parse_overrides(s.vendor_secret_overrides))
    return asyncio.run(provider._fetch(secret_key(vendor_id, owner))) is not None


def _vendor(store, value: str, owner: str, check_secret: bool) -> str:
    vendor_id = canonical_vendor_id(slugify(value))
    if store.get_vendor(vendor_id) is None and adapter_class(vendor_id) is None:
        # A vendor with no record yet (e.g. a secret made with gcloud): fine if its
        # secret exists; the first run sets the vendor up from it.
        listed = store.get_account(account_id(vendor_id, owner)) is not None
        if not listed and (not check_secret or not _secret_exists(vendor_id, owner)):
            sys.exit(f"Unknown vendor '{vendor_id}' and no secret "
                     f"{secret_key(vendor_id, owner)}: create the secret first, "
                     "or add the vendor (scripts/manage_vendors.py add)")
    return vendor_id


def _editors(store, vendor_id: str, owner: str) -> list[str]:
    doc = store.get_account(account_id(vendor_id, owner))
    if doc is None:
        sys.exit(f"No login {account_id(vendor_id, owner)}: add it first")
    return list(doc.get("editors") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("vendor")
    add.add_argument("--owner", required=True, help="'shared' or the team member's email")
    add.add_argument("--editors", default="", help="who may change a shared login (emails)")
    for name in ("grant", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("vendor")
        p.add_argument("owner", help="'shared' or the login owner's email")
        p.add_argument("emails")
    args = parser.parse_args()

    s = get_settings()
    store = make_store(s.store_backend, s.store_project, s.store_file)

    if args.cmd == "list":
        admins = ", ".join(s.credential_admins) or "(none: set CREDENTIAL_ADMINS)"
        print(f"Admins (may change every login): {admins}")
        for login in all_logins(store):
            who = "shared" if login.is_shared else login.owner
            editors = ", ".join(login.editors) or "-"
            print(f"  {login.vendor_id:<16} {who:<32} {login.state or 'not used yet':<24} "
                  f"secret {login.secret_key}  editors: {editors}")
        return

    owner = _owner(args.owner)
    vendor_id = _vendor(store, args.vendor, owner, check_secret=args.cmd == "add")
    aid = account_id(vendor_id, owner)
    if args.cmd == "add":
        editors = _emails(args.editors)
        if editors and owner != SHARED:
            sys.exit("--editors is for shared logins; a person's own login is theirs to change")
        doc = store.get_account(aid) or {}
        current = login_from_record(aid, doc)
        merged = sorted(set(editors) | set(current.editors if current else ()))
        store.update_account(aid, {"vendor_id": vendor_id, "user_email": owner,
                                   "secret_key": secret_key(vendor_id, owner),
                                   "editors": merged})
        store.audit("login_registered", account_id=aid, editors=merged)
        print(f"{aid}: listed (secret {secret_key(vendor_id, owner)}"
              + (f", editors {', '.join(merged)})" if merged else ")"))
        print("The secret must exist in Secret Manager before the page can change it.")
        return

    if owner != SHARED:
        sys.exit("Editors are only for shared logins; a person's own login is theirs to change")
    editors = set(_editors(store, vendor_id, owner))
    emails = set(_emails(args.emails))
    editors = editors | emails if args.cmd == "grant" else editors - emails
    store.update_account(aid, {"editors": sorted(editors)})
    store.audit(f"login_{args.cmd}", account_id=aid, emails=sorted(emails))
    print(f"{aid}: editors now {', '.join(sorted(editors)) or '(none)'}")


if __name__ == "__main__":
    main()
