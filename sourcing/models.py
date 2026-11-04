"""Shared data types and state enums."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from .conditions import CONDITION_CODES

SHARED = "_shared"


class RowStatus(str, Enum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    DONE = "DONE"
    NOT_FOUND = "NOT_FOUND"
    NOT_SOLD = "NOT_SOLD"      # the portal says it doesn't sell this part
    WAITING_AUTH = "WAITING_AUTH"
    NO_CREDENTIALS = "NO_CREDENTIALS"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    FAILED = "FAILED"


class AccountState(str, Enum):
    ACTIVE = "ACTIVE"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    NEEDS_CREDENTIAL_UPDATE = "NEEDS_CREDENTIAL_UPDATE"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    SUSPENDED = "SUSPENDED"


# States in which the worker must not try to log in on its own.
BLOCKED_ACCOUNT_STATES = {
    AccountState.AWAITING_HUMAN,
    AccountState.NEEDS_CREDENTIAL_UPDATE,
    AccountState.NEEDS_ATTENTION,
    AccountState.SUSPENDED,
}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def account_id(vendor_id: str, owner: str) -> str:
    """Firestore doc id for a (vendor, user) account, e.g. proponent__jane@co.com."""
    return f"{vendor_id}__{owner}"


@dataclass
class RequestRow:
    """One row of a `Requests` tab. `sheet_id` identifies which spreadsheet this
    came from — the system can have many (see store.py's sheet registry)."""

    sheet_row: int  # 1-based row number in the sheet (header is row 1)
    rfq_id: str
    vendor_raw: str
    part_number: str
    qty_needed: str = ""
    condition_wanted: str = ""
    requested_by: str = ""
    status: str = ""
    run_id: str = ""
    sheet_id: str = ""
    # Optional request details from the client's RFQ layout (2026-10-19); only
    # copied to the RFQ tab, not used in the search.
    alternate_part_number: str = ""
    keyword: str = ""
    part_description: str = ""
    serialized: str = ""
    uom: str = ""
    # Outright or Exchange: the kind of deal the customer asks for.
    type_requested: str = ""

    @property
    def vendor_id(self) -> str:
        return slugify(self.vendor_raw)

    @property
    def row_id(self) -> str:
        # Prefixed with sheet_id: the same row number exists in every sheet.
        return f"{self.sheet_id}__row{self.sheet_row}"

    @property
    def requester(self) -> str:
        return (self.requested_by or "").strip().lower()


@dataclass
class Offer:
    """One price line a vendor shows for a part (a part can have several)."""

    part_number: str
    price: Optional[str] = None
    currency: Optional[str] = None
    qty_available: Optional[str] = None
    condition: Optional[str] = None
    lead_time: Optional[str] = None
    source_url: Optional[str] = None
    # Minimum order quantity for the line, only when the page states one.
    moq: Optional[str] = None
    # The vendor's own description and unit of measure, read from the page by the
    # adapter (RFQ tab only).
    description: Optional[str] = None
    uom: Optional[str] = None
    # Traceability and release certificate ("tag") from the line's certification
    # notes, read by the model by the client's rules (prompt 3.11, 3.12; RFQ tab only).
    trace: Optional[str] = None
    tagged_by: Optional[str] = None
    # The tag's date as the page writes it ("15 OCT 1996", DASI's Tag Details
    # hover text, 2026-10-30; RFQ tab only).
    tag_date: Optional[str] = None
    # Stock per warehouse, one block each (extract.format_warehouses), e.g.
    # "Warehouse: Brea, CA\nQty on hand: 45\nNext day in: May 20, 2027\nQty due in: 200"
    warehouses: Optional[str] = None
    # What the line is for the requested part: "requested" (the part itself, maybe
    # written differently), "substitute" (an approved alternate) or "other".
    listing: Optional[str] = None
    # Set when a value couldn't be found in the page data it was read from
    # (e.g. "CHECK_PRICE"); shown in the Flag column for a person to verify.
    review: Optional[str] = None
    # Unit price at the quantity needed, from the page's price-break table (set
    # by the adapter; None = not worked out).
    qty_price: Optional[str] = None
    # The line's whole price-break table, (from qty, to qty or "" for "500+",
    # unit price) per row, as the page shows it; written to Internal Comments.
    price_breaks: list[tuple[str, str, str]] = field(default_factory=list)
    # A note from the vendor's page the adapter adds to Internal Comments as
    # written (Wencor's "Ships from Georgia (USA) Distribution on November 3,
    # 2026", team 2026-11-03).
    vendor_note: Optional[str] = None


@dataclass
class ResultRow:
    """One row of the `Results` tab."""

    rfq_id: str
    vendor: str
    part_number: str
    price: str = ""
    currency: str = ""
    qty_available: str = ""
    warehouses: str = ""
    condition: str = ""
    lead_time: str = ""
    source_url: str = ""
    fetched_at: str = ""
    requested_by: str = ""
    flag: str = ""
    approved: str = ""
    requested_part: str = ""
    qty_needed: str = ""
    qty_price: str = ""
    moq: str = ""
    # Not a column: the least the vendor sells (MOQ, or where price breaks start),
    # for the RFQ tab's Qty Quoted (pipeline.min_order, 2026-10-21).
    min_order_qty: str = ""
    # Not a column: the RFQ tab's Unit Price, the price-break price for the
    # quantity quoted (pipeline.quote_price, 2026-10-23).
    quote_price: str = ""
    # Not a column: the substitute(s) a pointer line for the requested part
    # points to (pipeline.alternate_notes); its RFQ line is qty 0, price 0.01.
    points_to: str = ""
    # For the team member: how a requested part and its listed alternates relate
    # (pipeline.alternate_notes), then the line's price-break table
    # (pipeline.price_break_note).
    internal_comments: str = ""
    # For the RFQ tab only (not Results columns): the request's details, and the
    # description / unit of measure (the request's, else the vendor's).
    condition_wanted: str = ""
    alternate_part_number: str = ""
    keyword: str = ""
    part_description: str = ""
    serialized: str = ""
    uom: str = ""
    type_requested: str = ""
    trace: str = ""
    tagged_by: str = ""
    tag_date: str = ""
    # The RFQ tab's Delivery: "0" in stock, else the line's lead time (client,
    # 2026-10-23); rfq_delivery turns it into days.
    delivery: str = ""

    def as_sheet_record(self) -> dict[str, str]:
        """Column heading -> value. Writers place values by heading, so columns
        people add or move in the sheet are left alone."""
        return {
            "RFQ ID": self.rfq_id, "Vendor": self.vendor,
            "Requested Part": self.requested_part, "Part Number": self.part_number,
            "Unit Price": self.price, "Price at Qty Needed": self.qty_price,
            "Currency": self.currency, "Qty Needed": self.qty_needed,
            "Qty Available": self.qty_available, "MOQ": self.moq,
            "Warehouses": self.warehouses,
            "Condition": self.condition, "Lead Time": self.lead_time,
            "Source URL": self.source_url, "Fetched At": self.fetched_at,
            "Requested By": self.requested_by, "Flag": self.flag,
            "Internal Comments": self.internal_comments, "Approved": self.approved,
        }

    def as_sheet_values(self) -> list[str]:
        return list(self.as_sheet_record().values())

    def as_rfq_record(self) -> dict[str, str]:
        """Column heading -> value for the RFQ tab (the client's
        RFQ_Spreadsheet_Sample layout, 2026-10-19). Quote-side columns follow the
        client's own quote-reading rules (inputai-skill.md, 2026-10-19); mappings
        marked "for now" wait for the client's answer
        (docs/RFQ_Sample_Column_Review.md)."""
        quote = rfq_quote(self)
        # Quantity Requested = Qty Quoted = the site's stock (client, 2026-10-23,
        # their AS3491-01 line: 50 asked, 7915 / 7915 at the price for 50), or out
        # of stock the quantity due in on the soonest date, No Quote lines too
        # (2026-10-26, 2026-10-27). Nothing due in: the customer's quantity.
        asked = self.qty_needed
        if (quote["Type Quoted"] in ("Outright", "No Quote")
                and (_number(quote["Qty Quoted"]) or 0) > 0):
            asked = quote["Qty Quoted"]
        return {
            "Vendor Reference": self.vendor,            # for now: the vendor's name
            "Part Number": self.requested_part,
            "Alternate Part Number": self.alternate_part_number,
            "Keyword": self.keyword,
            "Part Description": self.part_description,
            # Fixed values for the AvSight import, whatever the request says
            # (client, 2026-10-26: "ALWAYS").
            "Serialized": "N",
            # The request's unit, else the line's; EA when neither says (the
            # client's "always EA" until LB requests, 2026-10-30).
            "UOM": uom_code(self.uom) or "EA",
            "Condition Code": quote["Condition Quoted"],
            "Quantity Requested": asked,
            "Type Requested": "Outright",
            "Quoted Date": self.fetched_at[:10],
            "PN Quoted": self.part_number,
            **quote,                                    # Condition / Qty / Type Quoted, Unit Price
            # A line that can't be quoted has no delivery, even when the page shows
            # a lead time (client, 2026-10-26, CA02020A).
            "Delivery": "" if quote["Type Quoted"] == "No Quote" else rfq_delivery(self),
            # Serial Number: only when a vendor states it; ours don't. Tag Date: the
            # vendor's, when the page gives one (DASI, 2026-10-30).
            "Validity": "", "Serial Number": "", "Tagged By": self.tagged_by,
            "Tag Date": rfq_tag_date(self.tag_date),
            "Trace": self.trace,
            "Internal Comments": self.internal_comments,
            "External Comments": "",                    # for the customer: never written
        }


def _number(text: str) -> Optional[float]:
    """"$25,237.00" -> 25237.0; None for empty or non-numeric text (e.g. "Not shown by ...")."""
    try:
        return float(re.sub(r"[$,\s]", "", text or ""))
    except ValueError:
        return None


# Unit Price of the line that points from a requested part to its substitute
# (client, 2026-10-23: qty 0, price 0.01, "QUOTING PN ...").
POINTER_PRICE = "0.01"


def rfq_quote(row: "ResultRow") -> dict[str, str]:
    """Condition Quoted, Qty Quoted, Type Quoted and Unit Price, by the client's
    rules. A line can be quoted when it has a price and a lead time (client,
    2026-10-27); in stock counts as delivery 0, and so does stock due in on a
    date. Without a unit price, an in-stock condition code, or (out of stock) a
    delivery, it is "No Quote" (its condition code, "-" when unknown; price 0, qty 0, or out of stock
    the quantity due in; the client's rules say 0.00, plain 0 asked for 2026-10-28). An out-of-stock line with a price and a delivery
    is quoted without a condition code too, Condition Quoted left empty
    (2026-10-27, substitute 426EN9-8: the page shows none for it). Otherwise it is "Outright" (the deal type; our vendors
    only sell outright) and Qty Quoted is:
    - in stock: the site's whole stock (client, 2026-10-23);
    - out of stock with stock due in: what comes in on the soonest date, all
      deliveries that day added up (2026-10-27: 4140073-1, 75 on Dec 7, 2026
      and 200 on Jul 26, 2027 -> 75; 418-09104, 1 + 3 + 3 on one date -> 7);
    - out of stock, nothing due in: the customer's quantity (2026-10-27,
      406Q80-1: 1 asked -> 1, delivery = the lead time);
    - no stock figure shown: the quantity requested, or the vendor's minimum
      (MOQ, or where price breaks start) when less is asked (2026-10-21).
    Unit Price is the price-break price for the customer's quantity
    (pipeline.quote_price). A pointer line to a substitute is qty 0, price
    0.01, with no condition or type (the client named only those)."""
    if row.points_to:
        return {"Condition Quoted": "", "Qty Quoted": "0", "Type Quoted": "",
                "Unit Price": POINTER_PRICE}
    stock = 0 if _out_of_stock(row) else _number(row.qty_available)
    due = (soonest_due_in(row.warehouses)[1] or 0) if stock == 0 else 0
    price = _number(row.price)
    known = row.condition in CONDITION_CODES
    if (price is None or (stock == 0 and not rfq_delivery(row))
            or (stock != 0 and not known)):
        # A known condition stays on the No Quote line (DASI's SV lots with "Send
        # RFQ" and no price, 2026-10-30): "-" only when the page shows none.
        return {"Condition Quoted": row.condition if known else "-",
                "Qty Quoted": _qty_str(due), "Type Quoted": "No Quote", "Unit Price": "0"}
    asked = _number(row.qty_needed) or 1          # Qty Needed empty = 1
    need = max(asked, _number(row.min_order_qty) or _number(row.moq) or 0)
    if stock == 0:
        qty = due or asked
    else:
        qty = stock if stock is not None else need
    unit = row.quote_price if _number(row.quote_price) is not None else row.price
    return {"Condition Quoted": row.condition if known else "",
            "Qty Quoted": _qty_str(qty),
            "Type Quoted": "Outright", "Unit Price": unit.replace("$", "").replace(",", "")}


def rfq_tag_date(text: str) -> str:
    """The page's tag date as the client's YYYY-MM-DD ("15 OCT 1996" ->
    "1996-10-15"); a combined line's "(1 of 2 lots)" stays after it. A date we
    can't read is written as the page shows it."""
    date_part, sep, rest = (text or "").partition(" (")
    day = parse_date(date_part)
    return f"{day.isoformat()}{sep}{rest}" if day else (text or "")


# Ways the pages and the requests write the same unit; anything else is
# compared as written.
_UOM_ALIASES = {"EACH": "EA", "PC": "EA", "PCS": "EA", "PCE": "EA", "PIECE": "EA",
                "LBS": "LB", "POUND": "LB", "POUNDS": "LB"}


def uom_code(text: Optional[str]) -> str:
    code = (text or "").strip().upper().rstrip(".")
    return _UOM_ALIASES.get(code, code)


def _out_of_stock(row: "ResultRow") -> bool:
    return "out of stock" in row.qty_available.lower() or _number(row.qty_available) == 0


def _qty_str(qty: float) -> str:
    return str(int(qty)) if qty == int(qty) else str(qty)


# Dates as the portals show them: "Dec 7, 2026", "December 7, 2026", "2026-12-07".
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%Y-%m-%d", "%d %b %Y")


def parse_date(text: str) -> Optional[date]:
    text = re.sub(r"\s+", " ", (text or "").strip().rstrip("."))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def due_in(warehouses: str) -> list[tuple[Optional[date], float]]:
    """(date, qty) for every "Qty due in" in the Warehouses cell, all warehouses
    (extract.format_warehouses); date None when the page shows none or it can't
    be read."""
    out: list[tuple[Optional[date], float]] = []
    when: Optional[date] = None
    for line in (warehouses or "").splitlines():
        label, _, value = line.partition(":")
        label = label.strip()
        if label == "Warehouse":
            when = None
        elif label == "Next day in":
            when = parse_date(value)
        elif label == "Qty due in":
            match = re.search(r"\d[\d,]*\.?\d*", value)
            if match:
                out.append((when, float(match.group().replace(",", ""))))
            when = None
    return out


def soonest_due_in(warehouses: str) -> tuple[Optional[date], Optional[float]]:
    """(soonest date, everything due in that day, all warehouses added up)
    (client, 2026-10-27). Due-in quantities without a readable date are only
    used when no quantity has one: (None, their total). (None, None) when
    nothing is due in."""
    entries = due_in(warehouses)
    if not entries:
        return None, None
    dated = [(d, q) for d, q in entries if d]
    if not dated:
        return None, sum(q for _, q in entries)
    first = min(d for d, _ in dated)
    return first, sum(q for d, q in dated if d == first)


# "3-5 weeks" -> 35: the larger end of a range, times 7 for weeks and 30 for months.
_LEAD = re.compile(r"(\d+)(?:\s*(?:-|to)\s*(\d+))?\s*(business\s+)?(day|week|month)s?\b",
                   re.IGNORECASE)


def lead_time_days(lead: str) -> str:
    """A lead time as a number of days, by the client's rules ("129" -> "129",
    "3-5 weeks" -> "35"); empty when it isn't a number of days, weeks or months."""
    lead = (lead or "").strip()
    if lead.isdigit():
        return lead
    match = _LEAD.search(lead)
    if not match:
        return ""
    low, high, _, unit = match.groups()
    return str(int(high or low) * {"day": 1, "week": 7, "month": 30}[unit.lower()])


def rfq_delivery(row: "ResultRow") -> str:
    """Delivery for the RFQ tab as a number of days: "0" in stock; out of stock
    with stock due in, the days from the quote date to the soonest delivery
    (client, 2026-10-27: 4140073-1 quoted Oct 26, 75 due Dec 7 -> 42); else the
    line's lead time (2026-10-23). A soonest date already past, or no quote
    date, falls back to the lead time."""
    if _out_of_stock(row):
        first, _ = soonest_due_in(row.warehouses)
        try:
            quoted = date.fromisoformat(row.fetched_at[:10])
        except ValueError:
            quoted = None
        if first and quoted and first >= quoted:
            return str((first - quoted).days)
    lead = (row.delivery or row.lead_time or "").strip()
    if not lead:
        stock = _number(row.qty_available)
        return "0" if stock and stock > 0 else ""
    return lead_time_days(lead)


# Headings renamed since tabs were created: an old heading is renamed in place,
# so its column (and values already under it) keeps its place.
# "Price" -> "Unit Price" 2026-10-16: it is one piece's price, not a break price.
RENAMED_RESULTS_HEADINGS = {"price": "Unit Price"}

RFQ_HEADER = list(ResultRow("", "", "").as_rfq_record())

RESULTS_HEADER = [
    "RFQ ID", "Vendor", "Requested Part", "Part Number", "Unit Price", "Price at Qty Needed", "Currency",
    "Qty Needed", "Qty Available", "MOQ", "Warehouses", "Condition", "Lead Time", "Source URL", "Fetched At",
    "Requested By", "Flag", "Internal Comments", "Approved",
]


@dataclass
class LlmUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "LlmUsage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cost_usd += other.cost_usd

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class SearchOutcome:
    found: bool
    offers: list[Offer] = field(default_factory=list)
    usage: LlmUsage = field(default_factory=LlmUsage)
    # The portal's own words when it says it doesn't sell the part.
    not_sold: Optional[str] = None
    # The results page stayed empty ("No data found") even after a reload: maybe
    # a portal hiccup, so the pipeline searches the part again before logout.
    empty_page: bool = False
    page_url: str = ""          # results page, for the Source URL of a NOT_SOLD row
    # Part numbers the portal lists as interchanges / replacements of the requested
    # part for a vendor with substitutes off (Wencor, Aviall): no entry of their own,
    # only a line in the requested part's Internal Comments (team, 2026-11-04).
    alternates: list[str] = field(default_factory=list)
    # Page-change signals (sourcing/health.py). page_kind: the extractor's view of
    # the page ("results", "no_results", "error", "other", ...). grid_used: True if
    # the structured results grid was read, False if the adapter fell back to page
    # text, None if the adapter doesn't read a grid. Both default to "unknown".
    page_kind: str = ""
    grid_used: Optional[bool] = None
