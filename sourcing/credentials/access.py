"""Who may change which vendor login on the login update page
(service/credentials_ui.py, docs/CREDENTIAL_UPDATE_UI_PLAN.md):

  - their own personal login: always
  - a shared login: the people in its record's `editors`, and admins
  - admins (CREDENTIAL_ADMINS): every login; they also add logins and choose
    a shared login's editors on the page (creating the secret needs their own
    Google right to create secrets, checked by Google, not here)

The logins are the account records (store `accounts/<vendor>__<owner>`): our
account can't list secrets. The worker adds a record the first time it uses a
login; scripts/manage_logins.py adds one before that and sets its editors.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..models import SHARED, account_id
from ..vendors import adapter_class
from .base import secret_key


@dataclass(frozen=True)
class Login:
    vendor_id: str
    owner: str            # user email, or "_shared"
    editors: tuple = ()
    state: str = ""
    password_changed_at: Optional[float] = None
    password_changed_by: str = ""

    @property
    def account_id(self) -> str:
        return account_id(self.vendor_id, self.owner)

    @property
    def secret_key(self) -> str:
        return secret_key(self.vendor_id, self.owner)

    @property
    def is_shared(self) -> bool:
        return self.owner == SHARED


def _clean(emails: Iterable) -> tuple:
    return tuple(sorted({str(e).strip().lower() for e in emails or () if str(e).strip()}))


def login_from_record(aid: str, doc: dict) -> Optional[Login]:
    # Records written before 2026-10-23 may lack vendor_id / user_email; the id
    # is "<vendor>__<owner>" and vendor ids never hold "__" (slugify).
    vendor_id, _, owner = aid.partition("__")
    vendor_id = doc.get("vendor_id") or vendor_id
    owner = (doc.get("user_email") or owner or "").strip().lower()
    if not vendor_id or not owner:
        return None
    return Login(vendor_id, owner, _clean(doc.get("editors")), doc.get("state") or "",
                 doc.get("password_changed_at"), doc.get("password_changed_by") or "")


def all_logins(store) -> list[Login]:
    logins = (login_from_record(aid, doc) for aid, doc in store.list_accounts().items())
    return sorted((l for l in logins if l), key=lambda l: (l.vendor_id, not l.is_shared, l.owner))


def is_admin(email: str, admins: Iterable = ()) -> bool:
    email = (email or "").strip().lower()
    return bool(email) and email in _clean(admins)


def can_edit(email: str, login: Login, admins: Iterable = ()) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    return is_admin(email, admins) or login.owner == email or email in login.editors


def editable_logins(store, email: str, admins: Iterable = ()) -> list[Login]:
    return [l for l in all_logins(store) if can_edit(email, l, admins)]


def vendor_name(store, vendor_id: str) -> str:
    record = store.get_vendor(vendor_id) or {}
    cls = adapter_class(vendor_id)
    return record.get("display_name") or (cls.display_name if cls else vendor_id)
