"""FileStore: local JSON-backed store, so Context reuse can be tested without Firestore."""
from __future__ import annotations

from sourcing.store import FileStore, make_store


def test_state_survives_a_new_store_instance(tmp_path):
    path = str(tmp_path / "store.json")
    store = FileStore(path)
    store.update_account("proponent___shared", {"context_id": "ctx-1", "state": "ACTIVE"})
    store.save_selectors("proponent", {"search_input": "#q"})
    store.register_sheet("s1", {"label": "Local"})

    reopened = FileStore(path)
    assert reopened.get_account("proponent___shared")["context_id"] == "ctx-1"
    assert reopened.get_selectors("proponent") == {"search_input": "#q"}
    assert [s["sheet_id"] for s in reopened.list_enabled_sheets()] == ["s1"]


def test_leases_are_cleared_on_load_and_locks_are_not_saved(tmp_path):
    path = str(tmp_path / "store.json")
    store = FileStore(path)
    assert store.acquire_lease("proponent___shared", "run-1", 1800)
    assert store.acquire_lock("worker", "run-1", 3600)

    # a crashed run never released either; the next local run must not be blocked
    reopened = FileStore(path)
    assert reopened.acquire_lease("proponent___shared", "run-2", 1800)
    assert reopened.acquire_lock("worker", "run-2", 3600)


def test_make_store_file_backend(tmp_path):
    store = make_store("file", file_path=str(tmp_path / "s.json"))
    assert isinstance(store, FileStore)
