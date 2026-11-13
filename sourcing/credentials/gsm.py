"""GCP Secret Manager provider for vendor logins.

Secrets are read by name only: the Skylink IAM grant covers names starting
with `sourcing-` but does not allow listing secrets in the project. New
secrets are created only with an admin's own Google access (create_login_secret).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from .base import SECRET_PREFIX, CredentialError, CredentialProvider

logger = logging.getLogger("sourcing.credentials.gsm")


def parse_overrides(raw: str) -> dict[str, str]:
    """`a=b,c=d` -> {"a": "b", "c": "d"}: read secret `a` from secret `b`."""
    pairs = (p.split("=", 1) for p in raw.split(",") if "=" in p)
    return {k.strip(): v.strip() for k, v in pairs if k.strip() and v.strip()}


class SecretManagerProvider(CredentialProvider):
    def __init__(self, project: str, overrides: Optional[dict[str, str]] = None,
                 client=None) -> None:
        if not project:
            raise CredentialError("Secret Manager is not configured: GCP_PROJECT not set")
        self._project = project
        self._overrides = dict(overrides or {})
        self._client = client

    def _get_client(self):
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def secret_name(self, key: str) -> str:
        return self._overrides.get(key, key)

    def _path(self, key: str) -> str:
        return f"projects/{self._project}/secrets/{self.secret_name(key)}"

    def _access(self, key: str) -> Optional[bytes]:
        from google.api_core import exceptions

        try:
            resp = self._get_client().access_secret_version(name=f"{self._path(key)}/versions/latest")
        except exceptions.NotFound:
            return None
        except exceptions.PermissionDenied:
            # GCP answers 403 for secrets we may not read, existing or not.
            logger.warning("No permission to read secret %s (missing, or no access)",
                           self.secret_name(key))
            return None
        return resp.payload.data

    async def _fetch(self, key: str) -> Optional[dict]:
        data = await asyncio.to_thread(self._access, key)
        if data is None:
            return None
        name = self.secret_name(key)
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError(f"Secret '{name}' is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CredentialError(f"Secret '{name}' must be a JSON object")
        return payload

    def _add_version(self, key: str, data: bytes) -> None:
        from google.api_core import exceptions

        client = self._get_client()
        path = self._path(key)
        try:
            client.add_secret_version(parent=path, payload={"data": data})
            return
        except exceptions.NotFound:
            pass
        try:
            client.create_secret(parent=f"projects/{self._project}",
                                 secret_id=self.secret_name(key),
                                 secret={"replication": {"automatic": {}}})
        except exceptions.PermissionDenied as exc:
            raise CredentialError(
                f"Secret '{self.secret_name(key)}' does not exist and this account cannot "
                "create it; ask the Skylink admin to create it") from exc
        client.add_secret_version(parent=path, payload={"data": data})

    async def _write(self, key: str, payload: dict) -> None:
        await asyncio.to_thread(self._add_version, key, json.dumps(payload).encode("utf-8"))
        logger.info("Added a new version of secret %s", self.secret_name(key))


def _create_as(project: str, name: str, data: bytes, client) -> str:
    from google.api_core import exceptions

    path = f"projects/{project}/secrets/{name}"
    try:
        client.create_secret(parent=f"projects/{project}", secret_id=name,
                             secret={"replication": {"automatic": {}},
                                     "labels": {"app": "sourcing"}})
        state = "created"
    except exceptions.AlreadyExists:
        # A secret with no enabled version holds no login yet (e.g. a create that
        # failed half way); filling it overwrites nothing.
        # The filter only goes in `request` (the client has no filter= argument;
        # 500 on /credentials/new for an existing secret, 2026-11-12).
        versions = client.list_secret_versions(request={"parent": path,
                                                        "filter": "state:ENABLED"})
        if any(True for _ in versions):
            return "exists"
        state = "filled"
    client.add_secret_version(parent=path, payload={"data": data})
    return state


async def create_login_secret(project: str, key: str, payload: dict, credentials=None,
                              client=None) -> str:
    """Create a new login's secret AS THE SIGNED-IN ADMIN (their Google token), so
    Google's IAM decides who may create secrets; our service account still can't.
    Returns "created", "filled" (existed without a value) or "exists" (left alone)."""
    from google.api_core import exceptions

    if not key.startswith(SECRET_PREFIX):
        raise CredentialError(f"Only {SECRET_PREFIX}* secrets are created here")
    if client is None:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient(credentials=credentials)
    try:
        state = await asyncio.to_thread(_create_as, project, key,
                                        json.dumps(payload).encode("utf-8"), client)
    except exceptions.PermissionDenied as exc:
        raise CredentialError(f"Your Google account may not create secrets in {project} "
                              "(needs Secret Manager Admin)") from exc
    except exceptions.GoogleAPICallError as exc:
        raise CredentialError(f"Secret Manager refused: {type(exc).__name__}") from exc
    logger.info("Secret %s: %s", key, state)
    return state
