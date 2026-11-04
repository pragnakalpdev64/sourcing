"""Dummy vendor + browser + credentials for the Step 1 dry run (DUMMY_MODE=1).

Lets the full sheet -> worker -> sheet loop run on Cloud Run without
Browserbase, Secret Manager or Claude.
"""
from __future__ import annotations

from typing import Optional

from ..browser.base import BrowserHandle
from ..credentials.base import CredentialProvider
from ..models import Offer, RequestRow, SearchOutcome
from .base import VendorAdapter


class DummyAdapter(VendorAdapter):
    vendor_id = "dummy"
    display_name = "Dummy"
    # No real portal behind it: no need to wait between searches, and a dry run
    # never writes the vendor's limits record.
    default_limits = {"min_seconds_between_requests": 0}
    real_portal = False

    async def is_authenticated(self, page) -> bool:
        return True

    async def login(self, page, cred) -> None:
        return None

    async def search(self, page, row: RequestRow) -> SearchOutcome:
        return SearchOutcome(found=True, offers=[Offer(
            part_number=row.part_number, price="0.00", currency="USD",
            qty_available="0", condition="DUMMY", lead_time="n/a",
            source_url="dummy://no-browser",
        )])


class NullBrowser:
    """Stands in for BrowserbaseClient when no real browser is needed."""

    async def create_context(self) -> str:
        return "dummy-context"

    async def open(self, context_id: str, **_: object) -> BrowserHandle:
        return BrowserHandle(session_id="dummy-session", page=None, browser=None, playwright=None)

    async def live_view_url(self, session_id: str) -> str:
        return "about:blank"

    async def close(self, handle: BrowserHandle, *, release: bool = True) -> None:
        return None


class DummyCredentialProvider(CredentialProvider):
    async def _fetch(self, key: str) -> Optional[dict]:
        return {"username": "dummy", "password": "dummy"}

    async def _write(self, key: str, payload: dict) -> None:
        return None
