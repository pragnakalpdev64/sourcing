"""Settings from the developer's .env must not change what the tests check.

sourcing.config loads .env into the environment; a local SHEET_ACCESS=relay (add-on
testing, 2026-11-16) made the direct-sheet tests look for the add-on's copy.
Tests that need relay or a local trigger set them with monkeypatch.
INFRA_SECRETS=gsm would read the real infra keys from Secret Manager at the first
get_settings(): tests never touch real secrets (tests/test_infra_secrets.py fakes it).
"""
import pytest

from sourcing.config import get_settings

_LOCAL_ONLY = ("SHEET_ACCESS", "TRIGGER_RUN_LOCAL", "TRIGGER_LOCAL_ARGS", "INFRA_SECRETS",
               "ADDON_OAUTH_PROJECT_NUMBERS")


@pytest.fixture(autouse=True)
def _no_local_env(monkeypatch):
    for name in _LOCAL_ONLY:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
