"""Settings from the developer's .env must not change what the tests check.

sourcing.config loads .env into the environment; a local SHEET_ACCESS=relay (add-on
testing, 2026-11-16) made the direct-sheet tests look for the add-on's copy.
Tests that need relay or a local trigger set them with monkeypatch.
"""
import pytest

from sourcing.config import get_settings

_LOCAL_ONLY = ("SHEET_ACCESS", "TRIGGER_RUN_LOCAL", "TRIGGER_LOCAL_ARGS")


@pytest.fixture(autouse=True)
def _no_local_env(monkeypatch):
    for name in _LOCAL_ONLY:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
