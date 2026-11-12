"""State store: jobs, rows, accounts (+ leases), vendors (+ usage), learned selectors,
2FA code requests, audit.

`FirestoreStore` is used on Cloud Run. `MemoryStore` has the same behaviour
and is used for local dry runs and tests. `FileStore` is a MemoryStore saved to
a local JSON file, for local runs without Firestore.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

WORKER_LOCK = "worker"    # held by the running Cloud Run Job
TRIGGER_LOCK = "trigger"  # short debounce for /trigger
# Set by /trigger when rows came in while a worker was running (or just started):
# that worker read its rows at the start, so it reads the sheet once more before
# it ends instead of leaving them for the next sweep. Kept as a lock so Firestore
# and the memory store need nothing new.
RERUN_FLAG = "rerun"
RERUN_HOLDER = "requested"
RERUN_SECONDS = 3600


def _lease_is_free(doc: Optional[dict], holder: str, now: float,
                   holder_field: str, expires_field: str) -> bool:
    if not doc:
        return True
    current = doc.get(holder_field)
    if not current or current == holder:
        return True
    return float(doc.get(expires_field) or 0) < now


class Store(ABC):
    # --- named locks (one worker run at a time, trigger debounce) ------
    @abstractmethod
    def acquire_lock(self, name: str, holder: str, ttl_seconds: int) -> bool: ...

    @abstractmethod
    def release_lock(self, name: str, holder: str) -> None: ...

    @abstractmethod
    def is_locked(self, name: str) -> bool: ...

    # --- accounts --------------------------------------------------------
    @abstractmethod
    def get_account(self, account_id: str) -> Optional[dict]: ...

    @abstractmethod
    def update_account(self, account_id: str, fields: dict) -> None: ...

    # Our account can't list secrets, so the account records are the list of
    # logins the login update page offers (service/credentials_ui.py).
    @abstractmethod
    def list_accounts(self) -> dict[str, dict]: ...

    @abstractmethod
    def acquire_lease(self, account_id: str, holder: str, ttl_seconds: int) -> bool: ...

    @abstractmethod
    def release_lease(self, account_id: str, holder: str) -> None: ...

    # --- vendors / selectors --------------------------------------------
    @abstractmethod
    def get_vendor(self, vendor_id: str) -> Optional[dict]: ...

    @abstractmethod
    def list_vendors(self) -> dict[str, dict]: ...

    @abstractmethod
    def update_vendor(self, vendor_id: str, fields: dict) -> None:
        """Merge `fields` into the vendor record (created if missing)."""

    # Per-vendor counters for the request limits (sourcing/limits.py): searches
    # today, last request time, back-off. Changes every search, so kept apart
    # from the vendor record an admin edits.
    @abstractmethod
    def get_vendor_usage(self, vendor_id: str) -> Optional[dict]: ...

    @abstractmethod
    def update_vendor_usage(self, vendor_id: str, fields: dict) -> None: ...

    # Per-vendor run-health history for the page-change detector (sourcing/health.py):
    # a capped list of recent runs' metric dicts, newest last.
    @abstractmethod
    def get_vendor_health(self, vendor_id: str) -> list[dict]: ...

    @abstractmethod
    def append_vendor_health(self, vendor_id: str, record: dict, keep: int = 10) -> None: ...

    @abstractmethod
    def get_selectors(self, vendor_id: str) -> dict: ...

    @abstractmethod
    def save_selectors(self, vendor_id: str, selectors: dict) -> None: ...

    # --- sheet registry: which spreadsheets the worker processes --------
    @abstractmethod
    def register_sheet(self, sheet_id: str, fields: dict) -> None:
        """Upsert a sheet. Called from /trigger with whatever the caller sent
        (label, tab names), so a new sheet needs no admin step or redeploy."""

    @abstractmethod
    def list_enabled_sheets(self, enabled_only: bool = True) -> list[dict]: ...

    @abstractmethod
    def set_sheet_enabled(self, sheet_id: str, enabled: bool) -> None: ...

    # --- jobs / rows / audit --------------------------------------------
    @abstractmethod
    def update_job(self, run_id: str, fields: dict) -> None: ...

    @abstractmethod
    def record_row(self, row_id: str, fields: dict) -> None: ...

    @abstractmethod
    def rows_with_unsaved_results(self, sheet_id: str) -> list[tuple[str, dict]]:
        """(row_id, row doc) for rows of this sheet whose Results write failed, or
        whose RFQ tab lines couldn't be written (`unsaved_rfq`)."""

    # 2FA code requests (sourcing/auth/hitl.py), by an id derived from the link's
    # token. The API writes the person's code and the worker reads it: two
    # processes, so a FileStore keeps these out of its main file.
    @abstractmethod
    def get_hitl(self, hitl_id: str) -> Optional[dict]: ...

    @abstractmethod
    def update_hitl(self, hitl_id: str, fields: dict) -> None: ...

    @abstractmethod
    def audit(self, event: str, **fields: Any) -> None: ...


class MemoryStore(Store):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.locks: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.vendors: dict[str, dict] = {}
        self.vendor_usage: dict[str, dict] = {}
        self.vendor_health: dict[str, list] = {}
        self.sheets: dict[str, dict] = {}
        self.selectors: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.rows: dict[str, dict] = {}
        self.audit_log: list[dict] = []
        self.hitl: dict[str, dict] = {}

    def acquire_lock(self, name: str, holder: str, ttl_seconds: int) -> bool:
        with self._lock:
            now = time.time()
            if not _lease_is_free(self.locks.get(name), holder, now, "holder", "expires_at"):
                return False
            self.locks[name] = {"holder": holder, "expires_at": now + ttl_seconds}
            return True

    def release_lock(self, name: str, holder: str) -> None:
        with self._lock:
            doc = self.locks.get(name)
            if doc and doc.get("holder") == holder:
                self.locks.pop(name, None)

    def is_locked(self, name: str) -> bool:
        with self._lock:
            return not _lease_is_free(self.locks.get(name), "", time.time(), "holder", "expires_at")

    def get_account(self, account_id: str) -> Optional[dict]:
        with self._lock:
            doc = self.accounts.get(account_id)
            return copy.deepcopy(doc) if doc else None

    def update_account(self, account_id: str, fields: dict) -> None:
        with self._lock:
            self.accounts.setdefault(account_id, {}).update(copy.deepcopy(fields))

    def list_accounts(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self.accounts)

    def acquire_lease(self, account_id: str, holder: str, ttl_seconds: int) -> bool:
        with self._lock:
            now = time.time()
            doc = self.accounts.get(account_id)
            if not _lease_is_free(doc, holder, now, "lease_holder", "lease_expires_at"):
                return False
            self.accounts.setdefault(account_id, {}).update(
                {"lease_holder": holder, "lease_expires_at": now + ttl_seconds}
            )
            return True

    def release_lease(self, account_id: str, holder: str) -> None:
        with self._lock:
            doc = self.accounts.get(account_id)
            if doc and doc.get("lease_holder") == holder:
                doc["lease_holder"] = None
                doc["lease_expires_at"] = None

    def get_vendor(self, vendor_id: str) -> Optional[dict]:
        return copy.deepcopy(self.vendors.get(vendor_id))

    def list_vendors(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self.vendors)

    def update_vendor(self, vendor_id: str, fields: dict) -> None:
        with self._lock:
            self.vendors.setdefault(vendor_id, {}).update(copy.deepcopy(fields))

    def get_vendor_usage(self, vendor_id: str) -> Optional[dict]:
        with self._lock:
            return copy.deepcopy(self.vendor_usage.get(vendor_id))

    def update_vendor_usage(self, vendor_id: str, fields: dict) -> None:
        with self._lock:
            self.vendor_usage.setdefault(vendor_id, {}).update(copy.deepcopy(fields))

    def get_vendor_health(self, vendor_id: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self.vendor_health.get(vendor_id, []))

    def append_vendor_health(self, vendor_id: str, record: dict, keep: int = 10) -> None:
        with self._lock:
            history = self.vendor_health.setdefault(vendor_id, [])
            history.append(copy.deepcopy(record))
            del history[:-keep]

    def register_sheet(self, sheet_id: str, fields: dict) -> None:
        with self._lock:
            doc = self.sheets.setdefault(sheet_id, {
                "enabled": True, "requests_tab": "Requests", "results_tab": "Results",
                "registered_at": time.time(),
            })
            doc.update({k: v for k, v in fields.items() if v is not None})
            doc["last_seen_at"] = time.time()

    def list_enabled_sheets(self, enabled_only: bool = True) -> list[dict]:
        with self._lock:
            return [{"sheet_id": sid, **doc} for sid, doc in self.sheets.items()
                    if not enabled_only or doc.get("enabled", True)]

    def set_sheet_enabled(self, sheet_id: str, enabled: bool) -> None:
        with self._lock:
            self.sheets.setdefault(sheet_id, {})["enabled"] = enabled

    def get_selectors(self, vendor_id: str) -> dict:
        return dict(self.selectors.get(vendor_id, {}))

    def save_selectors(self, vendor_id: str, selectors: dict) -> None:
        with self._lock:
            self.selectors.setdefault(vendor_id, {}).update(selectors)

    def update_job(self, run_id: str, fields: dict) -> None:
        with self._lock:
            self.jobs.setdefault(run_id, {}).update(copy.deepcopy(fields))

    def record_row(self, row_id: str, fields: dict) -> None:
        with self._lock:
            self.rows.setdefault(row_id, {}).update(copy.deepcopy(fields))

    def rows_with_unsaved_results(self, sheet_id: str) -> list[tuple[str, dict]]:
        with self._lock:
            return [(rid, copy.deepcopy(doc)) for rid, doc in self.rows.items()
                    if doc.get("unsaved_sheet") == sheet_id
                    and (doc.get("unsaved_results") or doc.get("unsaved_rfq"))]

    def get_hitl(self, hitl_id: str) -> Optional[dict]:
        with self._lock:
            return copy.deepcopy(self.hitl.get(hitl_id))

    def update_hitl(self, hitl_id: str, fields: dict) -> None:
        with self._lock:
            self.hitl.setdefault(hitl_id, {}).update(copy.deepcopy(fields))

    def audit(self, event: str, **fields: Any) -> None:
        with self._lock:
            self.audit_log.append({"event": event, "at": time.time(), **fields})


class FirestoreStore(Store):
    def __init__(self, project: Optional[str] = None, client=None) -> None:
        from google.cloud import firestore

        self._fs = firestore
        self.db = client or firestore.Client(project=project or None)

    # Firestore transactions retry the function on contention, so it must be
    # side-effect free apart from the transaction writes.
    def _try_lease(self, ref, holder: str, ttl_seconds: int,
                   holder_field: str, expires_field: str) -> bool:
        transaction = self.db.transaction()

        @self._fs.transactional
        def _run(txn) -> bool:
            snap = ref.get(transaction=txn)
            doc = snap.to_dict() if snap.exists else None
            now = time.time()
            if not _lease_is_free(doc, holder, now, holder_field, expires_field):
                return False
            txn.set(ref, {holder_field: holder, expires_field: now + ttl_seconds}, merge=True)
            return True

        return _run(transaction)

    def _release(self, ref, holder: str, holder_field: str, expires_field: str) -> None:
        transaction = self.db.transaction()

        @self._fs.transactional
        def _run(txn) -> None:
            snap = ref.get(transaction=txn)
            if snap.exists and (snap.to_dict() or {}).get(holder_field) == holder:
                txn.set(ref, {holder_field: None, expires_field: None}, merge=True)

        _run(transaction)

    def acquire_lock(self, name: str, holder: str, ttl_seconds: int) -> bool:
        ref = self.db.collection("locks").document(name)
        return self._try_lease(ref, holder, ttl_seconds, "holder", "expires_at")

    def release_lock(self, name: str, holder: str) -> None:
        ref = self.db.collection("locks").document(name)
        self._release(ref, holder, "holder", "expires_at")

    def is_locked(self, name: str) -> bool:
        snap = self.db.collection("locks").document(name).get()
        doc = snap.to_dict() if snap.exists else None
        return not _lease_is_free(doc, "", time.time(), "holder", "expires_at")

    def get_account(self, account_id: str) -> Optional[dict]:
        snap = self.db.collection("accounts").document(account_id).get()
        return snap.to_dict() if snap.exists else None

    def update_account(self, account_id: str, fields: dict) -> None:
        self.db.collection("accounts").document(account_id).set(fields, merge=True)

    def list_accounts(self) -> dict[str, dict]:
        return {snap.id: snap.to_dict() or {} for snap in self.db.collection("accounts").stream()}

    def acquire_lease(self, account_id: str, holder: str, ttl_seconds: int) -> bool:
        ref = self.db.collection("accounts").document(account_id)
        return self._try_lease(ref, holder, ttl_seconds, "lease_holder", "lease_expires_at")

    def release_lease(self, account_id: str, holder: str) -> None:
        ref = self.db.collection("accounts").document(account_id)
        self._release(ref, holder, "lease_holder", "lease_expires_at")

    def get_vendor(self, vendor_id: str) -> Optional[dict]:
        snap = self.db.collection("vendors").document(vendor_id).get()
        return snap.to_dict() if snap.exists else None

    def list_vendors(self) -> dict[str, dict]:
        return {snap.id: snap.to_dict() or {} for snap in self.db.collection("vendors").stream()}

    def update_vendor(self, vendor_id: str, fields: dict) -> None:
        # Merge by top-level field, like the other stores: a new `limits` map
        # replaces the old one (merge=True would keep keys that were removed).
        self.db.collection("vendors").document(vendor_id).set(fields, merge=list(fields))

    def get_vendor_usage(self, vendor_id: str) -> Optional[dict]:
        snap = self.db.collection("vendor_usage").document(vendor_id).get()
        return snap.to_dict() if snap.exists else None

    def update_vendor_usage(self, vendor_id: str, fields: dict) -> None:
        self.db.collection("vendor_usage").document(vendor_id).set(fields, merge=list(fields))

    def get_vendor_health(self, vendor_id: str) -> list[dict]:
        snap = self.db.collection("vendor_health").document(vendor_id).get()
        return (snap.to_dict() or {}).get("runs", []) if snap.exists else []

    def append_vendor_health(self, vendor_id: str, record: dict, keep: int = 10) -> None:
        runs = self.get_vendor_health(vendor_id)
        runs.append(record)
        self.db.collection("vendor_health").document(vendor_id).set({"runs": runs[-keep:]})

    def register_sheet(self, sheet_id: str, fields: dict) -> None:
        ref = self.db.collection("sheets").document(sheet_id)
        transaction = self.db.transaction()

        @self._fs.transactional
        def _run(txn) -> None:
            snap = ref.get(transaction=txn)
            payload = {k: v for k, v in fields.items() if v is not None}
            payload["last_seen_at"] = time.time()
            if not snap.exists:
                payload.setdefault("enabled", True)
                payload.setdefault("requests_tab", "Requests")
                payload.setdefault("results_tab", "Results")
                payload["registered_at"] = time.time()
            txn.set(ref, payload, merge=True)

        _run(transaction)

    def list_enabled_sheets(self, enabled_only: bool = True) -> list[dict]:
        docs = self.db.collection("sheets").stream()
        out = []
        for snap in docs:
            doc = snap.to_dict() or {}
            if not enabled_only or doc.get("enabled", True):
                out.append({"sheet_id": snap.id, **doc})
        return out

    def set_sheet_enabled(self, sheet_id: str, enabled: bool) -> None:
        self.db.collection("sheets").document(sheet_id).set({"enabled": enabled}, merge=True)

    def get_selectors(self, vendor_id: str) -> dict:
        snap = self.db.collection("selectors").document(vendor_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def save_selectors(self, vendor_id: str, selectors: dict) -> None:
        self.db.collection("selectors").document(vendor_id).set(selectors, merge=True)

    def update_job(self, run_id: str, fields: dict) -> None:
        self.db.collection("jobs").document(run_id).set(fields, merge=True)

    def record_row(self, row_id: str, fields: dict) -> None:
        self.db.collection("rows").document(row_id).set(fields, merge=True)

    def rows_with_unsaved_results(self, sheet_id: str) -> list[tuple[str, dict]]:
        docs = self.db.collection("rows").where("unsaved_sheet", "==", sheet_id).stream()
        return [(d.id, doc) for d in docs
                if (doc := d.to_dict() or {}).get("unsaved_results") or doc.get("unsaved_rfq")]

    def get_hitl(self, hitl_id: str) -> Optional[dict]:
        snap = self.db.collection("hitl").document(hitl_id).get()
        return snap.to_dict() if snap.exists else None

    def update_hitl(self, hitl_id: str, fields: dict) -> None:
        self.db.collection("hitl").document(hitl_id).set(fields, merge=True)

    def audit(self, event: str, **fields: Any) -> None:
        self.db.collection("audit").document(uuid.uuid4().hex).set(
            {"event": event, "at": time.time(), **fields}
        )


class FileStore(MemoryStore):
    """MemoryStore saved to a local JSON file after every write, for local runs
    without Firestore. Accounts (Browserbase Context ids, login state), vendor
    config, selectors, sheets, jobs, rows and audit survive between runs, so
    Context reuse can be tested locally.

    Single process only: locks are not saved, and account leases are cleared on
    load (a crashed local run would otherwise block its accounts until the lease
    expires)."""

    _SAVED = ("accounts", "vendors", "vendor_usage", "vendor_health", "sheets", "selectors",
              "jobs", "rows", "audit_log")

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for name in self._SAVED:
                if name in data:
                    setattr(self, name, data[name])
            for doc in self.accounts.values():
                doc["lease_holder"] = None
                doc["lease_expires_at"] = None

    def _save(self) -> None:
        with self._lock:
            data = copy.deepcopy({name: getattr(self, name) for name in self._SAVED})
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, self.path)

    def update_account(self, account_id: str, fields: dict) -> None:
        super().update_account(account_id, fields)
        self._save()

    def acquire_lease(self, account_id: str, holder: str, ttl_seconds: int) -> bool:
        ok = super().acquire_lease(account_id, holder, ttl_seconds)
        if ok:
            self._save()
        return ok

    def release_lease(self, account_id: str, holder: str) -> None:
        super().release_lease(account_id, holder)
        self._save()

    def register_sheet(self, sheet_id: str, fields: dict) -> None:
        super().register_sheet(sheet_id, fields)
        self._save()

    def set_sheet_enabled(self, sheet_id: str, enabled: bool) -> None:
        super().set_sheet_enabled(sheet_id, enabled)
        self._save()

    def save_selectors(self, vendor_id: str, selectors: dict) -> None:
        super().save_selectors(vendor_id, selectors)
        self._save()

    def update_vendor(self, vendor_id: str, fields: dict) -> None:
        super().update_vendor(vendor_id, fields)
        self._save()

    def update_vendor_usage(self, vendor_id: str, fields: dict) -> None:
        super().update_vendor_usage(vendor_id, fields)
        self._save()

    def append_vendor_health(self, vendor_id: str, record: dict, keep: int = 10) -> None:
        super().append_vendor_health(vendor_id, record, keep)
        self._save()

    def update_job(self, run_id: str, fields: dict) -> None:
        super().update_job(run_id, fields)
        self._save()

    def record_row(self, row_id: str, fields: dict) -> None:
        super().record_row(row_id, fields)
        self._save()

    def audit(self, event: str, **fields: Any) -> None:
        super().audit(event, **fields)
        self._save()

    # One small file per 2FA code request, read fresh every time: the local API
    # (uvicorn) writes the code, the worker or onboarding run waits for it.
    def _hitl_path(self, hitl_id: str) -> str:
        folder = f"{self.path}.hitl"
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{os.path.basename(hitl_id)}.json")

    def get_hitl(self, hitl_id: str) -> Optional[dict]:
        try:
            with open(self._hitl_path(hitl_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return None

    def update_hitl(self, hitl_id: str, fields: dict) -> None:
        with self._lock:
            doc = {**(self.get_hitl(hitl_id) or {}), **fields}
            path = self._hitl_path(hitl_id)
            with open(f"{path}.tmp", "w", encoding="utf-8") as fh:
                json.dump(doc, fh, default=str)
            os.replace(f"{path}.tmp", path)


DEFAULT_FILE_STORE = ".local_store.json"


def make_store(backend: str, project: Optional[str] = None,
               file_path: str = DEFAULT_FILE_STORE) -> Store:
    # Cloud Run sets K_SERVICE (service) or CLOUD_RUN_JOB (job). A file or memory
    # store there is wiped after every run, and with it the login-failure and
    # search counters that keep vendors from locking us out (sourcing/limits.py).
    if backend in ("file", "memory") and (os.environ.get("K_SERVICE")
                                          or os.environ.get("CLOUD_RUN_JOB")):
        raise RuntimeError(f"STORE_BACKEND={backend} on Cloud Run would lose the vendor "
                           "login and search counters after each run; use firestore")
    if backend == "memory":
        return MemoryStore()
    if backend == "file":
        return FileStore(file_path)
    if backend == "firestore":
        return FirestoreStore(project=project)
    raise ValueError(f"Unknown STORE_BACKEND '{backend}' (expected firestore, file or memory)")
