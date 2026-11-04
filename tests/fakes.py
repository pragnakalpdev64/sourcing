"""In-memory stand-ins for the sheet, browser, vendor portal, sink and Chat."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sourcing.browser.browserbase import BrowserHandle
from sourcing.models import Offer, RequestRow, RowStatus, SearchOutcome
from sourcing.sinks.base import WriteOutcome
from sourcing.vendors.base import (
    InvalidCredentials, MfaRequired, RateLimited, SessionExpired, VendorAdapter,
)


class FakeSheet:
    def __init__(self, rows: list[dict], sheet_id: str = "test-sheet") -> None:
        self.sheet_id = sheet_id
        self.rows: list[RequestRow] = [
            RequestRow(sheet_row=i + 2, sheet_id=sheet_id, rfq_id=r.get("rfq", f"RFQ{i}"),
                       vendor_raw=r["vendor"], part_number=r["part"], qty_needed=r.get("qty", ""),
                       condition_wanted=r.get("cond", ""), requested_by=r.get("by", ""),
                       status=r.get("status", "NEW"))
            for i, r in enumerate(rows)
        ]
        self.notes: dict[int, str] = {}
        self.results: list[list[str]] = []
        self.on_read = None  # hook to add rows mid-run

    def read_new_rows(self, include_running: bool = False) -> list[RequestRow]:
        if self.on_read:
            self.on_read(self)
        pickup = ("", "NEW", "RUNNING") if include_running else ("", "NEW")
        return [RequestRow(**vars(r)) for r in self.rows if r.status in pickup]

    def set_status(self, rows, status: RowStatus, run_id=None, note=None) -> None:
        for row in rows:
            target = next(r for r in self.rows if r.sheet_row == row.sheet_row)
            target.status = status.value
            row.status = status.value
            if run_id is not None:
                target.run_id = run_id
            if note is not None:
                self.notes[row.sheet_row] = note

    def append_results(self, values: list[list[str]]) -> None:
        self.results.extend(values)

    def status_of(self, part: str) -> str:
        return next(r.status for r in self.rows if r.part_number == part)

    def part_at(self, sheet_row: int):
        return next((r.part_number for r in self.rows if r.sheet_row == sheet_row), None)


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts = 0
        self.opened: list[str] = []
        self.closed = 0
        self.cookies: dict[str, bool] = {}  # context_id -> signed in

    async def create_context(self) -> str:
        self.contexts += 1
        return f"ctx{self.contexts}"

    async def open(self, context_id: str, **_) -> BrowserHandle:
        self.opened.append(context_id)
        return BrowserHandle(session_id=f"s{len(self.opened)}", page=FakePage(self, context_id),
                             browser=None, playwright=None)

    async def live_view_url(self, session_id: str) -> str:
        return "https://live.example/" + session_id

    async def close(self, handle, *, release=True) -> None:
        self.closed += 1


@dataclass
class FakePage:
    browser: FakeBrowser
    context_id: str

    @property
    def signed_in(self) -> bool:
        return self.browser.cookies.get(self.context_id, False)

    def set_signed_in(self, value: bool) -> None:
        self.browser.cookies[self.context_id] = value


@dataclass
class Portal:
    """Behaviour of the fake vendor portal, shared by all adapter instances."""
    password: str = "pw"
    mfa: Optional[str] = None                     # e.g. "sms"
    catalog: dict = field(default_factory=lambda: {"P1": "10.00", "P2": "20.00", "P3": "30.00"})
    expire_before: set = field(default_factory=set)  # parts whose search finds the session expired
    logins: list = field(default_factory=list)       # owner of each login attempt
    searches: list = field(default_factory=list)
    # part -> answers for its next searches, used up in order: "error",
    # "empty" (portal showed no results), "not_sold", "rate_limited"; then the
    # catalog answers.
    script: dict = field(default_factory=dict)


class FakeAdapter(VendorAdapter):
    vendor_id = "proponent"
    display_name = "Proponent"
    default_limits = {"min_seconds_between_requests": 0}

    def __init__(self, portal: Portal, **kw) -> None:
        super().__init__(**kw)
        self.portal = portal

    async def is_authenticated(self, page: FakePage) -> bool:
        return page.signed_in

    async def login(self, page: FakePage, cred) -> None:
        self.portal.logins.append(cred.owner)
        if cred.password != self.portal.password:
            raise InvalidCredentials("bad password")
        if self.portal.mfa:
            raise MfaRequired(self.portal.mfa)
        page.set_signed_in(True)
        self.learned["username_input"] = "#user"

    async def search(self, page: FakePage, row: RequestRow) -> SearchOutcome:
        self.portal.searches.append(row.part_number)
        if row.part_number in self.portal.expire_before:
            self.portal.expire_before.discard(row.part_number)
            page.set_signed_in(False)
        if not page.signed_in:
            raise SessionExpired("logged out")
        script = self.portal.script.get(row.part_number)
        step = script.pop(0) if script else None
        if step == "error":
            raise RuntimeError("results grid did not load")
        if step == "rate_limited":
            raise RateLimited("Portal says 'too many requests'")
        if step == "empty":
            return SearchOutcome(found=False, empty_page=True)
        if step == "not_sold":
            return SearchOutcome(found=False, not_sold="This product is restricted for sale.",
                                 page_url=f"https://portal/r?id={row.part_number}")
        price = self.portal.catalog.get(row.part_number)
        if price is None:
            return SearchOutcome(found=False)
        return SearchOutcome(found=True, offers=[Offer(
            part_number=row.part_number, price=price, currency="USD", qty_available="5",
            condition="NE", lead_time="Stock", source_url="https://portal/x")])


class FakeSink:
    def __init__(self, sheet: FakeSheet) -> None:
        self.sheet = sheet
        self.fail_next = 0   # writes that fail like a dropped sheet connection
        self.rows = []       # ResultRows written, for the RFQ tab's columns

    async def write(self, rows):
        if self.fail_next:
            self.fail_next -= 1
            return [WriteOutcome(ok=False, error="RemoteDisconnected") for _ in rows]
        self.sheet.append_results([r.as_sheet_values() for r in rows])
        self.rows.extend(rows)
        return [WriteOutcome(ok=True) for _ in rows]


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[Optional[str], str]] = []

    async def send(self, text: str, *, to: Optional[str] = None) -> None:
        self.messages.append((to, text))
