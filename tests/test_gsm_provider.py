import json
from types import SimpleNamespace

import pytest
from google.api_core import exceptions

from sourcing.credentials.base import CredentialError, CredentialNotFound, secret_key
from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides

PROJECT = "skylink-material-planning"


class FakeSecretManager:
    """Latest version per secret; `denied` secrets answer 403 like missing IAM."""

    def __init__(self, secrets=None, denied=(), can_create=True):
        self.versions = {name: [json.dumps(v).encode()] for name, v in (secrets or {}).items()}
        self.denied = set(denied)
        self.can_create = can_create

    def _secret(self, path):
        return path.split("/secrets/")[1].split("/")[0]

    def access_secret_version(self, name):
        secret = self._secret(name)
        if secret in self.denied:
            raise exceptions.PermissionDenied("denied")
        if secret not in self.versions:
            raise exceptions.NotFound("missing")
        return SimpleNamespace(payload=SimpleNamespace(data=self.versions[secret][-1]))

    def add_secret_version(self, parent, payload):
        secret = self._secret(parent)
        if secret not in self.versions:
            raise exceptions.NotFound("missing")
        self.versions[secret].append(payload["data"])

    def create_secret(self, parent, secret_id, secret):
        assert parent == f"projects/{PROJECT}"
        if not self.can_create:
            raise exceptions.PermissionDenied("no create")
        self.versions[secret_id] = []


def test_secret_names_follow_skylink_convention():
    assert secret_key("proponent", "_shared") == "sourcing-vendor-proponent"
    assert secret_key("aero_parts", "_shared") == "sourcing-vendor-aero-parts"
    assert secret_key("proponent", "Jane.Doe@Skylink.com") == "sourcing-vendor-proponent__jane-doe-skylink-com"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_shared_and_reads_through_override():
    fake = FakeSecretManager({"sourcing-vendor-test": {
        "vendor": "Proponent", "url": "https://procart.proponent.com/",
        "username": "team", "password": "pw"}})
    sm = SecretManagerProvider(PROJECT, parse_overrides(
        "sourcing-vendor-proponent=sourcing-vendor-test"), client=fake)

    cred = await sm.resolve("proponent", "jane@skylink.com")
    assert cred.is_shared and (cred.username, cred.password) == ("team", "pw")
    assert sm.secret_name(cred.secret_key) == "sourcing-vendor-test"


@pytest.mark.asyncio
async def test_permission_denied_is_treated_as_missing():
    fake = FakeSecretManager({"sourcing-vendor-proponent": {"username": "t", "password": "p"}},
                             denied={"sourcing-vendor-proponent__jane-co-com"})
    sm = SecretManagerProvider(PROJECT, client=fake)
    assert (await sm.resolve("proponent", "jane@co.com")).is_shared

    with pytest.raises(CredentialNotFound):
        await sm.resolve("dasi", "jane@co.com")


@pytest.mark.asyncio
async def test_bad_json_is_a_credential_error():
    fake = FakeSecretManager()
    fake.versions["sourcing-vendor-proponent"] = [b"not json"]
    with pytest.raises(CredentialError, match="not valid JSON"):
        await SecretManagerProvider(PROJECT, client=fake).resolve("proponent", "")


@pytest.mark.asyncio
async def test_update_adds_a_version_and_creates_missing_secrets():
    fake = FakeSecretManager({"sourcing-vendor-proponent": {"username": "t", "password": "old"}})
    sm = SecretManagerProvider(PROJECT, client=fake)

    await sm.update("proponent", "_shared", {"password": "new"})
    assert len(fake.versions["sourcing-vendor-proponent"]) == 2
    assert (await sm.resolve("proponent", "")).password == "new"

    await sm.update("dasi", "_shared", {"username": "u", "password": "p"})
    assert (await sm.resolve("dasi", "")).username == "u"


@pytest.mark.asyncio
async def test_update_without_create_permission_says_who_to_ask():
    sm = SecretManagerProvider(PROJECT, client=FakeSecretManager(can_create=False))
    with pytest.raises(CredentialError, match="ask the Skylink admin"):
        await sm.update("dasi", "_shared", {"username": "u", "password": "p"})
