"""CredentialProvider interface (implemented by GCP Secret Manager).

Secret names (Secret Manager allows only letters, digits, `-` and `_`):
  sourcing-vendor-<vendor>                 the vendor's shared login
  sourcing-vendor-<vendor>__<user-email>   one requester's own login (optional)
e.g. sourcing-vendor-proponent, sourcing-vendor-proponent__jane-skylink-com.
The requester's own login is tried first, then the shared one.
Value: JSON {"username": ..., "password": ..., "totp_secret": ..., "url": ...}.
A portal that signs in with an emailed code instead of a password (Notion,
tested 2026-10-22) has "passwordless": true and no password; the code comes
from a person (sourcing/auth/hitl.py).
Secret values must never be logged or written to the sheet / Firestore.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..models import SHARED


class CredentialError(Exception):
    pass


class CredentialNotFound(CredentialError):
    pass


SECRET_PREFIX = "sourcing-vendor-"


def _name_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def secret_key(vendor_id: str, owner: str) -> str:
    base = SECRET_PREFIX + _name_part(vendor_id)
    return base if owner == SHARED else f"{base}__{_name_part(owner)}"


@dataclass(frozen=True)
class VendorCredential:
    vendor_id: str
    owner: str  # user email, or "_shared"
    secret_key: str
    username: str
    password: str = field(repr=False)
    totp_secret: Optional[str] = field(default=None, repr=False)
    url: Optional[str] = None  # login (or start) page from the secret, if given
    # Other sites the login may pass through (e.g. a single sign-on page), from the
    # secret. Only a Secret Manager admin can set them, like the password itself.
    allowed_hosts: tuple = ()
    # Signs in with the username and a code the portal sends, never a password.
    passwordless: bool = field(default=False, repr=False)   # repr: no "password" in logs

    @property
    def is_shared(self) -> bool:
        return self.owner == SHARED

    def totp_now(self) -> Optional[str]:
        if not self.totp_secret:
            return None
        import pyotp

        seed = re.sub(r"\s+", "", self.totp_secret)
        return pyotp.TOTP(seed).now()


class CredentialProvider(ABC):
    @abstractmethod
    async def _fetch(self, key: str) -> Optional[dict]:
        """Return the parsed secret JSON, or None if no such secret."""

    @abstractmethod
    async def _write(self, key: str, payload: dict) -> None:
        """Create or replace the secret value."""

    async def resolve(self, vendor_id: str, user_email: str) -> VendorCredential:
        """The requester's own account first, then the vendor's shared account."""
        owners = [o for o in ((user_email or "").strip().lower(), SHARED) if o]
        for owner in owners:
            key = secret_key(vendor_id, owner)
            payload = await self._fetch(key)
            if payload is None:
                continue
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "").strip()
            passwordless = payload.get("passwordless") in (True, "true", "yes", "1")
            if not username or not (password or passwordless):
                raise CredentialError(f"Secret '{key}' is missing username or password")
            totp = str(payload.get("totp_secret") or "").strip() or None
            url = str(payload.get("url") or "").strip() or None
            hosts = payload.get("allowed_hosts") or ()
            if isinstance(hosts, str):
                hosts = hosts.split(",")
            hosts = tuple(h.strip().lower() for h in hosts if str(h).strip())
            return VendorCredential(vendor_id, owner, key, username, password, totp, url, hosts,
                                    passwordless)
        raise CredentialNotFound(
            f"No credentials for vendor '{vendor_id}' (tried: "
            + ", ".join(secret_key(vendor_id, o) for o in owners) + ")"
        )

    async def update(self, vendor_id: str, owner: str, fields: dict,
                     must_exist: bool = False) -> None:
        """Merge new values (e.g. a changed password) into an account's secret.
        must_exist: the login update page only adds versions; new logins are
        created by the client in Secret Manager."""
        key = secret_key(vendor_id, owner)
        payload = await self._fetch(key)
        if payload is None and must_exist:
            raise CredentialNotFound(f"No secret {key} to update")
        payload = payload or {}
        payload.update({k: v for k, v in fields.items() if v})
        await self._write(key, payload)


class MemoryCredentialProvider(CredentialProvider):
    """For tests and local dry runs."""

    def __init__(self, secrets: Optional[dict[str, dict]] = None) -> None:
        self.secrets = dict(secrets or {})

    async def _fetch(self, key: str) -> Optional[dict]:
        value = self.secrets.get(key)
        return dict(value) if value is not None else None

    async def _write(self, key: str, payload: dict) -> None:
        self.secrets[key] = dict(payload)
