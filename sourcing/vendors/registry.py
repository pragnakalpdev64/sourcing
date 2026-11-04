"""Vendor registry: which vendors exist, read from the vendor records in the
store (Firestore `vendors/<id>`, or `vendors` in .local_store.json).

A record is added with scripts/manage_vendors.py (docs/GENERIC_VENDOR_PLAN.md):

  {"display_name": "Wencor", "aliases": ["wencor group"], "adapter": "generic",
   "enabled": false, "login_url": "https://...", "website": "https://...",
   "allowed_hosts": ["shop.wencor.com"], ...}

The same record is the adapter's vendor config, so keys such as login_url,
allowed_hosts, part_url_template or currency take effect without code. The
login URL is deliberately not a sheet column: whoever can edit the sheet would
otherwise choose where Skylink's password is typed.

Vendors with a built-in adapter (Proponent) work without a record; a record for
them only adds config, aliases or `enabled: false`.
"""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

from ..models import slugify
from . import ADAPTERS, adapter_class
from .base import check_login_url

# Second-level labels under a country code that are part of the suffix, so
# shop.vendor.co.uk -> vendor.co.uk, not co.uk.
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu", "ltd", "plc"}


def site_domain(url: str) -> str:
    """The site a URL belongs to: shop.wencor.com -> wencor.com,
    portal.vendor.co.uk -> vendor.co.uk. Its subdomains count as the same site
    (host_allowed), so a login on login.wencor.com that ends on shop.wencor.com works."""
    host = (urlparse(url).hostname or "").lower()
    labels = host.split(".")
    if len(labels) < 2 or host.replace(".", "").isdigit():
        return host
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def record_from_secret(name: str, cred) -> tuple[Optional[dict], Optional[str]]:
    """A vendor record for a vendor that has a login secret but no record yet:
    (record, None), or (None, why not). The secret's `url` says where to log in;
    only a Secret Manager admin can write it, so its site becomes the vendor's
    login site. Nothing comes from the sheet: whoever edits the sheet must not
    choose where Skylink's password is typed."""
    if not cred.url:
        return None, (f"Secret {cred.secret_key} has no 'url': add the vendor's login page "
                      "to it (the admin, in Secret Manager)")
    hosts = [site_domain(cred.url), *cred.allowed_hosts]
    problem = check_login_url(cred.url, hosts)
    if problem:
        return None, f"Secret {cred.secret_key} url: {problem}"
    return {
        "display_name": name.strip() or cred.vendor_id,
        "adapter": "generic",
        "enabled": True,
        "allowed_hosts": list(dict.fromkeys(hosts)),
        "auto_created_from": cred.secret_key,
        "added_at": time.time(),
        # Results of the first run are flagged NEW_VENDOR_CHECK (pipeline.py).
        "first_run_done": False,
    }, None


class VendorRegistry:
    """Read once per run: a record added mid-run is picked up by the next run."""

    def __init__(self, store) -> None:
        self._store = store
        self._records: Optional[dict[str, dict]] = None

    @property
    def records(self) -> dict[str, dict]:
        if self._records is None:
            self._records = self._store.list_vendors()
        return self._records

    def resolve_id(self, vendor: str) -> str:
        """The vendor id for what a team member typed in the Vendor column: a record id,
        a record's alias, or a built-in adapter's name; else the slug itself."""
        slug = slugify(vendor)
        if not slug:
            return ""
        if slug in self.records:
            return slug
        for vendor_id, record in self.records.items():
            if slug in {slugify(a) for a in record.get("aliases") or []}:
                return vendor_id
        cls = ADAPTERS.get(slug)
        return cls.vendor_id if cls else slug

    def record(self, vendor_id: str) -> Optional[dict]:
        return self.records.get(vendor_id)

    def add(self, vendor_id: str, record: dict) -> None:
        """Save a new record and use it for the rest of this run."""
        self._store.update_vendor(vendor_id, record)
        self.records[vendor_id] = dict(record)

    def has_adapter(self, vendor_id: str) -> bool:
        return adapter_class(vendor_id, self.record(vendor_id)) is not None

    def enabled(self, vendor_id: str) -> bool:
        """False only when a record says `enabled: false` (a vendor added but not
        yet checked). Built-in vendors without a record are enabled."""
        return (self.record(vendor_id) or {}).get("enabled", True) is not False

    def problem(self, vendor_id: str) -> Optional[str]:
        """Why rows for this vendor can't run, or None when they can."""
        record = self.record(vendor_id)
        if not self.enabled(vendor_id):
            return (f"Vendor '{self.display_name(vendor_id)}' is set up but not enabled yet "
                    "(enable it after its onboarding run: scripts/manage_vendors.py enable "
                    f"{vendor_id})")
        if adapter_class(vendor_id, record) is None:
            if record is not None and record.get("adapter"):
                return (f"Vendor '{self.display_name(vendor_id)}' is not supported yet: "
                        f"adapter '{record['adapter']}' is not available")
            return (f"Vendor '{vendor_id}' is not supported yet (add it with "
                    "scripts/manage_vendors.py add)")
        return None

    def display_name(self, vendor_id: str) -> str:
        record = self.record(vendor_id) or {}
        if record.get("display_name"):
            return record["display_name"]
        cls = adapter_class(vendor_id, record)
        return (cls.display_name if cls else "") or vendor_id or "(no vendor)"
