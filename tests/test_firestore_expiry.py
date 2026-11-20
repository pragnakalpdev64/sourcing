"""Stored data expires (team, 2026-11-20: "clean the data in 30 days"): the writes
that grow carry an `expire_at` date for Firestore's TTL policy (infra/firestore_ttl.sh);
settings and accounts never get one."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sourcing.store import EXPIRE_FIELD, FirestoreStore, expiry


class FakeDoc:
    def __init__(self, db, collection, doc_id):
        self.db, self.collection, self.id = db, collection, doc_id

    def set(self, fields, merge=False):
        self.db.writes.append((self.collection, dict(fields)))


class FakeCollection:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def document(self, doc_id):
        return FakeDoc(self.db, self.name, doc_id)


class FakeBatch:
    def __init__(self, db):
        self.db = db

    def set(self, ref, fields):
        ref.set(fields)

    def commit(self):
        pass


class FakeDb:
    def __init__(self):
        self.writes = []

    def collection(self, name):
        return FakeCollection(self, name)

    def batch(self):
        return FakeBatch(self)


def _store():
    store = FirestoreStore.__new__(FirestoreStore)
    store.db = FakeDb()
    return store


def test_growing_collections_get_an_expiry_date():
    store = _store()
    store.update_job("run-1", {"status": "DONE"})
    store.record_row("r1", {"status": "DONE"})
    store.update_hitl("h1", {"state": "code_given"})
    store.audit("login_ok", account_id="a")
    store.put_sheet_snapshot("s", {"Requests": [["Part Number"]]}, "Sheet")
    store.set_sheet_progress("s", {"state": "running"})
    store.add_sheet_ops("s", [{"kind": "cells"}])
    now = datetime.now(timezone.utc)
    days = {c: (f[EXPIRE_FIELD] - now).days for c, f in store.db.writes}
    assert days == {"jobs": 29, "rows": 29, "hitl": 29, "audit": 29, "sheet_ops": 29,
                    "sheet_snapshots": 6, "sheet_progress": 6}   # 30 / 7 days, less a moment


def test_settings_and_accounts_never_expire():
    store = _store()
    store.save_selectors("proponent", {"username_input": "#u"})
    assert all(EXPIRE_FIELD not in f for _, f in store.db.writes)
    assert expiry("accounts") == {} and expiry("vendors") == {} and expiry("locks") == {}


def test_expiry_is_a_timestamp_from_the_write():
    at = datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert expiry("rows", at.timestamp()) == {EXPIRE_FIELD: at + timedelta(days=30)}
