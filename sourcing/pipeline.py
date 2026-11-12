"""Worker pipeline: NEW rows -> group by account -> lease -> login -> search -> sink.

Rows are first grouped by (vendor, requester), then by the account that
actually gets used (the requester's own login or the vendor's `_shared`
one), so two requesters sharing one login never use the same Context at once.
Different accounts run in parallel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlencode

from .auth.hitl import hold_until_logged_out, logout_with_handoff
from .auth.manager import AuthManager, CannotLogin, LoginDeferred, NeedsHuman
from .conditions import CONDITION_CODES, normalize_condition
from . import health
from .extract import in_stock
from .credentials.base import (
    SHARED, CredentialError, CredentialNotFound, CredentialProvider, VendorCredential, secret_key,
)
from .models import (
    POINTER_PRICE, AccountState, LlmUsage, Offer, RequestRow, ResultRow, RowStatus, SearchOutcome,
    account_id, complete_by, lead_time_days, rfq_delivery, rfq_quote, soonest_due_in, uom_code,
)
from .limits import DEFAULT_LIMITS, LimitReached, VendorLimiter
from .sinks.sheet import QUOTA_RETRIES, QUOTA_WAIT, RETRY_DELAYS, over_quota
from .store import Store
from .vendors import adapter_class
from .vendors.registry import VendorRegistry, record_from_secret
from .vendors.base import BulkResultsIncomplete, RateLimited, SessionExpired, VendorAdapter

logger = logging.getLogger("sourcing.pipeline")

AdapterFactory = Callable[[str, dict, dict], Optional[VendorAdapter]]

# Most parts searched again one by one after a bulk search missed them (vendor
# config retry_not_found_max); the rest stay NOT_FOUND.
RETRY_NOT_FOUND_MAX = 100

# Written in a Results cell when the page doesn't show that value (or what was
# read couldn't be found on the page): never a guess.
# A value the portal doesn't show is written as "Not shown by <vendor>"; one the
# extractor reported but verification couldn't find on the page as CHECK_PAGE
# (the Flag names the field), so a team member can tell the two apart.
CHECK_PAGE = "Check page (see Flag)"
# Price at Qty Needed when the page has quantity pricing the adapter couldn't read.
QTY_PRICE_UNKNOWN = "Check page (quantity pricing)"

# Hard limits so a page that stops responding can't freeze the run (vendor
# config bulk_timeout_seconds / search_timeout_seconds override them). A bulk
# batch of 25 with paging takes ~1.5-3 min on a slow day, but each of its steps
# may wait up to the 60s action / 90s page-load timeouts; a single search ~0.5-2 min.
BULK_TIMEOUT_SECONDS = 600
SEARCH_TIMEOUT_SECONDS = 300

# _process_bulk results. TOO_BIG: the results couldn't all be read (too long
# for the LLM, past the pages we read); half the batch is tried next.
BULK_OK, BULK_TOO_BIG, BULK_FAILED = "ok", "too_big", "failed"
EMPTY_PAGE_NOTE = "Results page was empty (portal showed no results)"
# Added to every Results row of a vendor's first run (record `first_run_done: false`,
# e.g. set up from its secret this run): a person compares a few lines with the site.
NEW_VENDOR_FLAG = "NEW_VENDOR_CHECK"


@dataclass
class Deps:
    sheets: object          # SheetsClient (or a fake with the same methods)
    store: Store
    creds: CredentialProvider
    browser: object         # BrowserbaseClient / NullBrowser
    sink: object            # ResultSink
    notifier: object        # ChatNotifier
    adapter_factory: AdapterFactory
    max_login_attempts: int = 2
    lease_seconds: int = 1800
    # Shown in the run's Chat message, e.g. "anthropic claude-sonnet-5-5, effort medium"
    llm_label: str = ""
    # Asks the requester for a 2FA code over Chat (auth/hitl.py); None = such rows
    # go NEEDS_ATTENTION straight away.
    code_handoff: Optional[object] = None
    # The API's address: a rejected password's Chat message links to its login
    # update page (service/credentials_ui.py). Empty = no link.
    public_base_url: str = ""


@dataclass
class VendorTally:
    """Per-vendor numbers for the run's Chat message."""
    parts: int = 0
    counts: dict = field(default_factory=lambda: defaultdict(int))
    offers: int = 0
    mode: str = ""
    usage: LlmUsage = field(default_factory=LlmUsage)
    # Page-change signals (sourcing/health.py), counted once per real search
    # (twins reuse a search, so they don't add to these).
    grid_reads: int = 0          # searches where the adapter tried the results grid
    grid_fallback: int = 0       #   ... and fell back to page text
    page_reads: int = 0          # searches the extractor labelled with a page_kind
    bad_page_kind: int = 0       #   ... that it couldn't read as a results page
    extracted_offers: int = 0    # offers the extractor returned (before verification)
    check_page_offers: int = 0   #   ... with a value verification removed ("Check page")
    # Page actions the coded way couldn't do (assist.py): done by the LLM, with a
    # saved recipe, or not done (the lines say so in their Flag).
    page_actions: list = field(default_factory=list)

    def health_metrics(self) -> dict:
        """This vendor's run counters as the dict sourcing/health.py reads."""
        done = self.counts.get(RowStatus.DONE.value, 0)
        not_found = self.counts.get(RowStatus.NOT_FOUND.value, 0) + \
            self.counts.get(RowStatus.NOT_SOLD.value, 0)
        return {
            "parts": self.parts,
            "searched": done + not_found,
            "grid_reads": self.grid_reads, "grid_fallback": self.grid_fallback,
            "page_reads": self.page_reads, "bad_page_kind": self.bad_page_kind,
            "offer_total": self.extracted_offers, "check_page_offers": self.check_page_offers,
            "not_found": not_found,
            "attention": self.counts.get(RowStatus.NEEDS_ATTENTION.value, 0),
        }


@dataclass
class RunSummary:
    run_id: str
    counts: dict = field(default_factory=lambda: defaultdict(int))
    usage: LlmUsage = field(default_factory=LlmUsage)
    started_at: float = field(default_factory=time.time)
    vendors: dict = field(default_factory=lambda: defaultdict(VendorTally))
    # (sheet row, part number, vendor, status, note) of every row that didn't end DONE
    not_done: list = field(default_factory=list)
    # vendor id -> name for the Chat message (from the vendor record when it has one)
    vendor_names: dict = field(default_factory=dict)
    # vendor id -> secret it was set up from this run; vendors whose first run worked
    new_vendors: dict = field(default_factory=dict)
    first_runs: set = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "rows_total": sum(self.counts.values()),
            "counts": dict(self.counts),
            "llm": self.usage.as_dict(),
            "seconds": round(time.time() - self.started_at, 1),
        }


# Rows listed by name in the Chat message; the rest are counted.
CHAT_MAX_LISTED_ROWS = 10


class LlmKeyRejected(Exception):
    """The LLM provider refused our key (401 / 403). No search can be read until
    the key is fixed, so the account stops searching (runs 2026-11-05 16:37 and
    16:44: an invalid key made the pipeline search Incora's live portal ~40 times,
    bulk twice, then one by one, then again before logout)."""


def llm_key_rejected(exc: BaseException) -> bool:
    """An authentication / permission error from an LLM provider's SDK."""
    module = type(exc).__module__.split(".")[0]
    return (module in ("anthropic", "openai", "google")
            and getattr(exc, "status_code", None) in (401, 403))


def _vendor_name(vendor_id: str, names: Optional[dict] = None) -> str:
    if names and names.get(vendor_id):
        return names[vendor_id]
    cls = adapter_class(vendor_id)
    return cls.display_name if cls else (vendor_id or "(no vendor)")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _sheet_link(sheet_id: str) -> str:
    if not sheet_id:
        return "(unknown sheet)"
    if sheet_id.lower().endswith(".xlsx"):
        return f"`{sheet_id.replace(chr(92), '/').rsplit('/', 1)[-1]}` (local file)"
    return f"<https://docs.google.com/spreadsheets/d/{sheet_id}|Google Sheet …{sheet_id[-8:]}>"


def summary_message(summary: RunSummary, result: dict, sheet_id: str, llm_label: str,
                    warnings: Optional[dict] = None) -> str:
    """The end-of-run Chat message: totals, per vendor, LLM use and the rows that
    didn't end DONE. Google Chat formatting: *bold*, `code`, <url|text>.

    `warnings`: {vendor id: [lines]} from the page-change detector (health.py)."""
    rows = result["rows_total"]
    seconds = result["seconds"]
    llm = summary.usage
    problems = sum(n for status, n in result["counts"].items()
                   if status not in (RowStatus.DONE.value, RowStatus.NOT_FOUND.value,
                                     RowStatus.NOT_SOLD.value, RowStatus.NEW.value))
    title = "finished" if not problems else f"finished, {problems} row(s) need attention"
    counts = " · ".join(f"{status} {n}" for status, n in
                        sorted(result["counts"].items(), key=lambda kv: -kv[1]))
    offers = sum(v.offers for v in summary.vendors.values())
    lines = [
        f"*SourcingAI run {title}*",
        f"{rows} part(s) in {_duration(seconds)}"
        + (f" ({seconds / rows:.1f}s per part)" if rows else ""),
        f"Sheet: {_sheet_link(sheet_id)}",
        f"Run: `{summary.run_id}`",
        "",
        "*Results*",
        counts or "no rows",
        f"Offers written to Results: {offers}",
        "",
        "*By vendor*",
    ]
    for vendor_id, v in sorted(summary.vendors.items()):
        if not v.parts:
            continue
        vcounts = ", ".join(f"{s} {n}" for s, n in sorted(v.counts.items(), key=lambda kv: -kv[1]))
        lines.append(f"• {_vendor_name(vendor_id, summary.vendor_names)}: {v.parts} part(s)"
                     + (f", {v.mode}" if v.mode else "") + f" · {vcounts}"
                     + f" · {v.offers} offer(s) · LLM ${v.usage.cost_usd:.4f}")
    actions = {v_id: v.page_actions for v_id, v in summary.vendors.items() if v.page_actions}
    if actions:
        lines += ["", "*⚠ Page actions the code couldn't do alone (the vendor's page changed?)*"]
        for vendor_id, vlines in sorted(actions.items()):
            lines.append(f"• {_vendor_name(vendor_id, summary.vendor_names)}:")
            lines += [f"   - {line}" for line in vlines]
        lines.append("Lines with a gap say so in their Flag; update the adapter's selectors.")
    if warnings:
        lines += ["", "*⚠ Possible page change*"]
        for vendor_id, vlines in sorted(warnings.items()):
            lines.append(f"• {_vendor_name(vendor_id, summary.vendor_names)}:")
            lines += [f"   - {line}" for line in vlines]
        lines.append("Re-run `scripts/onboard_vendor.py <vendor> --fresh` and compare the offers "
                     "with the portal; update the adapter if the pages moved.")
    lines += ["", "*LLM*"]
    if llm_label:
        lines.append(f"• Model: {llm_label}")
    lines.append(f"• Calls: {llm.calls}")
    lines.append(f"• Tokens: {llm.input_tokens:,} in + {llm.output_tokens:,} out"
                 + (f" (avg {llm.input_tokens // rows:,} + {llm.output_tokens // rows:,} per part)"
                    if rows else ""))
    lines.append(f"• Cost: ${llm.cost_usd:.4f}"
                 + (f" total, ${llm.cost_usd / rows:.4f} per part" if rows else ""))
    new = sorted(set(summary.new_vendors) | summary.first_runs)
    if new:
        lines += ["", "*New vendors*"]
        for vendor_id in new:
            parts = []
            if vendor_id in summary.new_vendors:
                parts.append(f"set up from secret `{summary.new_vendors[vendor_id]}`")
            parts.append(f"first run worked; its Results rows are flagged {NEW_VENDOR_FLAG}: "
                         "please compare a few lines with the website"
                         if vendor_id in summary.first_runs else
                         "no search finished yet (see Not DONE)")
            lines.append(f"• {_vendor_name(vendor_id, summary.vendor_names)}: "
                         + "; ".join(parts))
    if summary.not_done:
        lines += ["", f"*Not DONE* ({len(summary.not_done)})"]
        for sheet_row, part, vendor_id, status, note in summary.not_done[:CHAT_MAX_LISTED_ROWS]:
            lines.append(f"• Row {sheet_row} `{part}` "
                         f"({_vendor_name(vendor_id, summary.vendor_names)}): {status}"
                         + (f": {note[:100]}" if note else ""))
        if len(summary.not_done) > CHAT_MAX_LISTED_ROWS:
            lines.append(f"• … and {len(summary.not_done) - CHAT_MAX_LISTED_ROWS} more "
                         f"(see the Status column)")
    return "\n".join(lines)


def default_adapter_factory(extractor, max_page_chars: int = 60000) -> AdapterFactory:
    def factory(vendor_id: str, selectors: dict, vendor_config: dict) -> Optional[VendorAdapter]:
        # vendor_config is the vendor record: its "adapter" picks the class
        cls = adapter_class(vendor_id, vendor_config)
        if cls is None:
            return None
        return cls(extractor=extractor, selectors=selectors, vendor_config=vendor_config,
                   max_page_chars=max_page_chars)
    return factory


def _number(value: Optional[str]) -> Optional[float]:
    match = re.search(r"\d[\d,]*\.?\d*", value or "")
    return float(match.group().replace(",", "")) if match else None


def _qty_text(qty: float) -> str:
    return str(int(qty)) if qty == int(qty) else str(qty)


def min_order(offer: Offer) -> tuple[Optional[float], str]:
    """The least the vendor sells of this line, and where that comes from: the
    stated MOQ ("moq"), else the first price-break row when it starts above 1
    ("breaks", e.g. "50-649 pcs: 0.27" with no MOQ stated). (None, "") when
    the page shows neither. Both numbers are on the page (client, 2026-10-21).
    A stated MOQ always wins: price breaks that start higher are discounts, not
    the minimum (team, 2026-11-04: Monroe's "Minimum order is 100" with bulk
    rates from 500 was quoted with MOQ 500)."""
    moq = _number(offer.moq)
    if moq is not None and moq > 1:
        return moq, "moq"
    if moq is not None:
        return None, ""
    starts = [n for n in (_number(low) for low, _, _ in offer.price_breaks) if n is not None]
    first = min(starts) if starts else None
    if first is not None and first > 1:
        return first, "breaks"
    return None, ""


def priced_qty(offer: Offer, qty_needed: Optional[str]) -> Optional[float]:
    """The quantity the line is priced and quoted at: the quantity needed, or the
    vendor's minimum when less is needed (Qty Needed empty = 1). None when the
    vendor has no minimum above what's needed."""
    minimum, _ = min_order(offer)
    need = _number(qty_needed) or 1
    return minimum if minimum is not None and need < minimum else None


def _break_row(offer: Offer, qty: float) -> Optional[tuple[str, str, str]]:
    """The price-break row that holds this quantity, if the line has one."""
    for low, high, price in offer.price_breaks:
        lo, hi = _number(low), _number(high)
        if lo is not None and lo <= qty and (hi is None or qty <= hi):
            return low, high, price
    return None


def _break_price(offer: Offer, qty: float) -> Optional[str]:
    row = _break_row(offer, qty)
    return row[2] if row else None


def _stock(qty_available: Optional[str]) -> Optional[float]:
    """The line's stock as a number: 0 for "Out of stock", None when not shown."""
    if "out of stock" in (qty_available or "").lower():
        return 0
    return _number(qty_available)


def price_qty(offer: Offer, qty_needed: Optional[str]) -> float:
    """The quantity the RFQ Unit Price and MOQ are for: what the customer asked
    (Qty Needed empty = 1; the vendor's minimum when less is asked), or the stock
    when the site has less (client, 2026-10-23). 0 for an out-of-stock line.
    Not Qty Quoted, which is the whole stock (models.rfq_quote)."""
    need = priced_qty(offer, qty_needed) or _number(qty_needed) or 1
    stock = _stock(offer.qty_available)
    # Less on hand than asked, the rest due in: quoted for the quantity asked
    # (models.complete_by, team 2026-11-06), so priced for it too.
    if complete_by(stock, need, offer.warehouses or ""):
        return need
    return min(need, stock) if stock is not None else need


def quote_price(offer: Offer, row: RequestRow, price: str) -> str:
    """RFQ Unit Price: the price-break price for the customer's quantity, or the
    stock when the site has less (client, 2026-10-23: 50 of AS3491-01 is quoted
    at the 22-172 row's $2.15, not 1-4's $15.64); out of stock, at the quantity asked. A line without a price-break
    table: the unit price the results grid shows for it."""
    need = priced_qty(offer, row.qty_needed) or _number(row.qty_needed) or 1
    qty = price_qty(offer, row.qty_needed) or need
    return _break_price(offer, qty) or price


def qty_price(offer: Offer, row: RequestRow, price: str) -> str:
    """Price at Qty Needed: the adapter's figure from the price-break table, else
    the unit price when only one is needed (or no price at all). Below the
    vendor's minimum it is the price at the minimum, the quantity quoted."""
    floor = priced_qty(offer, row.qty_needed)
    if floor is not None:
        return _break_price(offer, floor) or price
    if offer.qty_price:
        return offer.qty_price
    need = _number(row.qty_needed)
    if not offer.price or need is None or need <= 1:
        return price
    return QTY_PRICE_UNKNOWN


# Internal Comments use the client's own wording, in capitals (2026-10-23), as
# points with their details below them:
#   • MOQ 22 FOR THIS PRICE BREAK, STK BREA, CA
#       ◦ BREA, CA: 7915 ON HAND
#   • PRICE BREAKS:
#       ◦ 1-4 $15.64
#       ...
#       ◦ 173+ $1.06
#   • APPROVED SUBSTITUTE ALSO OFFERED: PN 125
# Order: MOQ and stock location, price breaks, substitute note.
POINT, SUB_POINT = "• ", "    ◦ "


def moq_note(offer: Offer, qty_needed: Optional[str] = None) -> str:
    """The MOQ part of the first point, only from numbers the page states:
    - "MOQ 22 FOR THIS PRICE BREAK": the quantity priced (price_qty) is in a
      price-break row that starts above 1, so 22 is the least that gets this
      price (the client's example: 50 asked, row 22-172 at $2.15);
    - "MOQ 50, PRICED AT 50 (ASKED 10)": less than the vendor's minimum (stated
      MOQ, or where price breaks start) is asked, so the Unit Price is the
      minimum's. Not "QUOTED 50": Qty Quoted is the site's stock, or out of
      stock what's due in or the customer's quantity (client, 2026-10-23,
      2026-10-27), and the comment must not state another quantity;
    - "MOQ 50": a stated MOQ that what's asked already meets."""
    minimum, _ = min_order(offer)
    need = _number(qty_needed) or 1
    if minimum is not None and need < minimum:
        asked = (qty_needed or "").strip() or "1"
        return f"MOQ {_qty_text(minimum)}, PRICED AT {_qty_text(minimum)} (ASKED {asked})"
    row = _break_row(offer, price_qty(offer, qty_needed) or need)
    if row and (_number(row[0]) or 0) > 1:
        return f"MOQ {row[0]} FOR THIS PRICE BREAK"
    if _number(offer.moq) is not None:
        return f"MOQ {offer.moq.strip()}"
    return ""


def _warehouses(warehouses: Optional[str]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(name, qty on hand, [(next-in date, qty due in)]) per warehouse in the
    Warehouses cell (extract.format_warehouses blocks)."""
    out = []
    for block in (warehouses or "").split("\n\n"):
        name, qty, incoming, date = "", "", [], None
        for line in block.splitlines():
            label, _, value = line.partition(":")
            value = value.strip()
            if label == "Warehouse":
                name = value
            elif label == "Qty on hand":
                qty = value
            elif label == "Next day in":
                if date is not None:
                    incoming.append((date, ""))
                date = value
            elif label == "Qty due in":
                incoming.append((date or "date not shown", value))
                date = None
        if date is not None:
            incoming.append((date, ""))
        if name or qty or incoming:
            out.append((name or "warehouse not named", qty, incoming))
    return out


def _by_date(incoming: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One entry per date, quantities added up, in the page's order (418-09104,
    2026-10-26: three deliveries on DEC 6, 2026 of 1, 3 and 3 -> one of 7). A
    quantity that isn't a number is kept as its own entry."""
    totals: dict[str, Optional[float]] = {}
    out: list[tuple[str, str]] = []
    for date, qty in incoming:
        number = _number(qty)
        if qty and number is None:
            out.append((date, qty))
            continue
        if date not in totals:
            totals[date] = None
            out.append((date, ""))
        if number is not None:
            totals[date] = (totals[date] or 0) + number
    return [(d, _qty_text(totals[d]) if not q and totals.get(d) is not None else q)
            for d, q in out]


def stock_note(offer: Offer, qty_needed: Optional[str], *, priced: bool, stocked: bool) -> str:
    """First point: the MOQ and where the stock is, as the client writes it
    ("MOQ 22 FOR THIS PRICE BREAK, STK BREA, CA"; every warehouse with stock,
    joined with " / "; "OUT OF STOCK" when every warehouse shows 0), then one
    sub-point per warehouse with its quantity on hand and its incoming stock,
    one entry per date (same date added up), in stock or not: the old Results
    comment listed them for every line, and the team still wants them
    (2026-11-06, 416F25-4: 5 on hand, Nov 25 10, Nov 27 1 + 6 -> two entries).
    What an in-stock line leaves out is the restock lead time ("53 days if out
    of stock", client 2026-10-23); that is never written here. Out of stock with
    nothing due in, the client's own line instead (2026-10-27, 406Q80-1):
    "336 DAY LEAD TIME, FOB: NETHERLANDS", the MOQ below it. Order limits stay
    in the Warehouses column."""
    stock = _warehouses(offer.warehouses)
    lead = lead_time_days(offer.lead_time or "") if not stocked else ""
    if lead and stock and not any(incoming for _, _, incoming in stock):
        fob = " / ".join(name for name, _, _ in stock if name != "warehouse not named")
        moq = moq_note(offer, qty_needed) if priced else ""
        return "\n".join(f"{POINT}{n}".upper() for n in (
            f"{lead} DAY LEAD TIME" + (f", FOB: {fob}" if fob else ""), moq) if n)
    # STK names where the stock is: a warehouse showing 0 on hand isn't one (seen
    # 2026-10-23: "STK BREA, CA" above "BREA, CA: 0 ON HAND"). No figure shown
    # still counts, as before.
    held = [name for name, qty, _ in stock if _number(qty) != 0]
    where = ("STK " + " / ".join(held) if held else "OUT OF STOCK") if stock else ""
    head = ", ".join(n for n in (moq_note(offer, qty_needed) if priced else "", where) if n)
    if not head:
        return ""
    lines = [f"{POINT}{head}".upper()]
    for name, qty, incoming in stock:
        parts = [f"{qty} ON HAND"] if qty else []
        parts += [f"NEXT IN {d}{': ' + q if q else ''}" for d, q in _by_date(incoming)]
        if parts:
            lines.append(f"{SUB_POINT}{name}: {', '.join(parts)}".upper())
    return "\n".join(lines)


def price_break_note(offer: Offer, currency: str) -> str:
    """The line's whole price-break table, as the client writes it, one
    sub-point per row: "• PRICE BREAKS:" then "    ◦ 1-4 $15.64" ...
    "    ◦ 173+ $1.06". "$" only for USD; another currency is written as its code."""
    if not offer.price_breaks:
        return ""
    unit = "$" if currency == "USD" else f"{currency} " if currency else ""
    lines = [f"{POINT}PRICE BREAKS:"]
    for low, high, price in offer.price_breaks:
        span = f"{low}+" if not high else low if high == low else f"{low}-{high}"
        lines.append(f"{SUB_POINT}{span} {unit}{price}")
    return "\n".join(lines)


def request_details(row: RequestRow, offer: Optional[Offer] = None) -> dict:
    """The request's details for the RFQ tab, copied to each of its Results rows.
    Description and UOM: the team member's, else what the vendor's page shows. Trace and
    Tagged By and Tag Date: the vendor's, from the line's certification notes."""
    return dict(
        condition_wanted=row.condition_wanted or "",
        alternate_part_number=row.alternate_part_number, keyword=row.keyword,
        serialized=row.serialized, type_requested=row.type_requested,
        part_description=row.part_description or (offer and offer.description) or "",
        uom=row.uom or (offer and offer.uom) or "",
        trace=(offer and offer.trace) or "", tagged_by=(offer and offer.tagged_by) or "",
        tag_date=(offer and offer.tag_date) or "")


def join_comments(*notes: str) -> str:
    """Internal Comments can hold several notes, one after another."""
    return "\n".join(n for n in notes if n)


def missing(vendor: str, offer: Offer, field: str) -> str:
    """Text for an empty cell: removed by verification, or not on the portal."""
    if f"{field}_NOT_ON_PAGE" in (offer.review or ""):
        return CHECK_PAGE
    return f"Not shown by {vendor}"


def lead_days(offer: Offer, vendor: str = "the vendor") -> str:
    """Lead Time column: only the number of days, and only for a line that is not
    in stock ("129 days (next due ...)" -> "129"). In stock -> empty. A lead time
    given another way (e.g. in weeks) is written as the page shows it."""
    if in_stock(offer.qty_available):
        return ""
    lead = (offer.lead_time or "").strip()
    match = re.search(r"(\d[\d,]*)\s*days?\b", lead, re.IGNORECASE)
    if match:
        return match.group(1).replace(",", "")
    return lead or missing(vendor, offer, "LEAD_TIME")


def same_part(listed: Optional[str], asked: str) -> bool:
    """The requested part number exactly, letter case aside. ACHC3-13 for
    ACHC-3-13 is another number: the client never puts different numbers in
    Requested / Quoted (2026-10-23), so such lines aren't written."""
    return (listed or "").strip().upper() == asked.strip().upper()


def same_part_groups(rows: list[RequestRow]) -> list[tuple[RequestRow, list[RequestRow]]]:
    """Rows -> (row to search, later rows asking the same part number), in the
    order each part first appears. The part is searched once and every row of
    it is recorded together, so its lines sit together in RFQ and Results even
    when the Requests tab has other parts between them (e.g. MS20470AD3-4 asked
    in EA and again in LB, 2026-10-30)."""
    groups: dict[str, list[RequestRow]] = {}
    for row in rows:
        groups.setdefault(row.part_number.strip().upper(), []).append(row)
    return [(group[0], group[1:]) for group in groups.values()]


def twin_outcome(outcome: SearchOutcome, searched: RequestRow,
                 twin: RequestRow) -> SearchOutcome:
    """The searched row's outcome for a twin: its own copy (offers get per-row
    values), and Price at Qty for the twin's quantity, from the line's price
    breaks, when it asks another quantity (the adapter's figure was for the
    searched row's). No LLM usage: the search was paid for once."""
    copy = deepcopy(outcome)
    copy.usage = LlmUsage()
    if _number(twin.qty_needed) != _number(searched.qty_needed):
        need = _number(twin.qty_needed) or 1
        for offer in copy.offers:
            offer.qty_price = _break_price(offer, need) if offer.qty_price else None
    return copy


def same_uom(listed: Optional[str], asked: str) -> bool:
    """A line in the unit the request asks for (2026-10-30: an LB request for
    MS20470AD3-4 got DASI's EA lots too). A request without a unit matches
    every line. A line whose page shows no unit counts as EA: an LB request
    only gets lines that say LB, never one we would have to guess."""
    asked = uom_code(asked)
    return not asked or (uom_code(listed) or "EA") == asked


def alternate_notes(row: RequestRow, lines: list[tuple[Offer, ResultRow]]) -> list[ResultRow]:
    """Substitutes the portal lists, by the client's rule (2026-10-23):
    requested and quoted part numbers are never different (Avsight's Part
    Research shows lines by number), so a substitute B for the requested A is
    its own line, B / B, saying "APPROVED SUBSTITUTE FOR PN A". A's own line says
    "APPROVED SUBSTITUTE ALSO OFFERED: PN B" when A is quoted: a price and in
    stock, or out of stock with a delivery (2026-10-28; before, only in stock)
    ("... ALSO OFFERED WITH 154 DAY LEAD TIME: PN B" for a B without stock, client
    2026-10-27, 426EN10-8 / 426EN9-8; the days added 2026-10-28, "WITH LEAD TIME"
    when the page gives none), otherwise "QUOTING PN B". When the portal shows no line for A, a line for A
    is added with qty 0 and price 0.01 pointing to B (models.rfq_quote). Only
    lines the portal itself calls a substitute count, never a look-alike number."""
    results = [r for _, r in lines]
    subs = [r for o, r in lines if o.listing == "substitute"]
    if not subs:
        return results
    asked = row.part_number

    def pns(rows: list[ResultRow]) -> str:
        return ", ".join(f"PN {pn}" for pn in dict.fromkeys(r.part_number for r in rows))

    alternates = pns(subs)
    for r in subs:
        r.requested_part = r.part_number
        r.internal_comments = join_comments(r.internal_comments,
                                            f"{POINT}APPROVED SUBSTITUTE FOR PN {asked}")
    own_lines = [(o, r) for o, r in lines if o.listing != "substitute"]
    if any(o.price and (in_stock(o.qty_available) or rfq_delivery(r)) for o, r in own_lines):
        stocked = [r for o, r in lines if o.listing == "substitute" and in_stock(o.qty_available)]
        # Out-of-stock substitutes grouped by their lead time, one point each.
        waiting: dict[str, list[ResultRow]] = {}
        for r in subs:
            if r not in stocked:
                waiting.setdefault(lead_time_days(r.lead_time), []).append(r)
        note = join_comments(
            f"{POINT}APPROVED SUBSTITUTE ALSO OFFERED: {pns(stocked)}" if stocked else "",
            *(f"{POINT}APPROVED SUBSTITUTE ALSO OFFERED WITH "
              f"{f'{days} DAY ' if days else ''}LEAD TIME: {pns(rows)}"
              for days, rows in waiting.items()))
    else:
        note = f"{POINT}QUOTING {alternates}"
    for _, r in own_lines:
        r.internal_comments = join_comments(r.internal_comments, note)
    if not own_lines:
        first = subs[0]
        results.insert(0, ResultRow(
            rfq_id=first.rfq_id, vendor=first.vendor, requested_part=asked,
            qty_needed=first.qty_needed, part_number=asked, price=POINTER_PRICE,
            points_to=", ".join(dict.fromkeys(r.part_number for r in subs)),
            source_url=first.source_url, fetched_at=first.fetched_at,
            requested_by=first.requested_by, internal_comments=note, **request_details(row)))
    return results


def add_listed_alternates(alternates: list[str], results: list[ResultRow]) -> None:
    """Wencor and Aviall (substitutes off, team 2026-11-04): the interchanges /
    replacements the page lists get no entry of their own, only this line in the
    requested part's Internal Comments."""
    pns = list(dict.fromkeys(a for a in alternates if a))
    if not pns:
        return
    note = f"{POINT}ALTERNATE PN LISTED: " + ", ".join(f"PN {pn}" for pn in pns)
    for r in results:
        if not r.points_to:           # a pointer line already says which part it quotes
            r.internal_comments = join_comments(r.internal_comments, note)


def flag_for(offer: Offer, row: RequestRow) -> str:
    flags = []
    need, have = _number(row.qty_needed), _stock(offer.qty_available)

    # Below the vendor's minimum the minimum is quoted, so stock under it is short too.
    floor = priced_qty(offer, row.qty_needed)
    quoted = floor if floor is not None else need
    if quoted is not None and have is not None and have < quoted:
        flags.append("QTY_SHORT")
    # Compare codes, so "New" in the sheet and "NE" from the portal match.
    want, have_cond = normalize_condition(row.condition_wanted), normalize_condition(offer.condition)
    if want and have_cond and have_cond.lower() != want.lower():
        flags.append("CONDITION_DIFFERS")
    if not offer.price and in_stock(offer.qty_available):
        flags.append("NO_PRICE")
    if floor is not None:
        # The vendor won't sell the quantity asked for (stated MOQ, or price
        # breaks starting above it): the minimum is quoted instead.
        flags.append("BELOW_MOQ")
    if offer.listing == "substitute":
        flags.append("SUBSTITUTE")
    # Out of stock with stock due in but no date to read: the RFQ tab adds it all
    # up and uses the lead time for Delivery, so a person checks the dates.
    due_date, due = soonest_due_in(offer.warehouses or "")
    if not in_stock(offer.qty_available) and due and due_date is None:
        flags.append("NO_DUE_DATE")
    # The warehouses read hold less than the line's total stock: some weren't read
    # (Proponent 4066-0014, 2026-11-04: 22 in total, only Brea's 17 read). Shown,
    # never silent (team: complete results or a visible gap).
    on_hand = [_number(q) for _, q, _ in _warehouses(offer.warehouses or "")]
    if (have and on_hand and all(q is not None for q in on_hand) and sum(on_hand) < have
            and "WAREHOUSES_INCOMPLETE" not in (offer.review or "")):
        flags.append("WAREHOUSES_SHORT")
    if offer.review:
        flags.append(offer.review)
    return ",".join(flags)


class Pipeline:
    def __init__(self, deps: Deps, run_id: str) -> None:
        self.d = deps
        self.run_id = run_id
        self.summary = RunSummary(run_id)
        # Open while a bulk batch is recorded: its Results lines and statuses are
        # collected here and saved in one go (_process_bulk), 3 sheet writes for
        # the batch instead of 3 per part (Sheets write quota, 2026-10-28).
        self._batch_results: Optional[list] = None
        self._batch_statuses: Optional[list] = None
        self.auth = AuthManager(deps.store, deps.browser, deps.max_login_attempts,
                                handoff=deps.code_handoff)
        self.vendors = VendorRegistry(deps.store)
        # Per-vendor search gap and caps for this run (sourcing/limits.py).
        self.limiter = VendorLimiter(deps.store)
        # vendor id -> adapter of a vendor whose record has no `limits` yet
        self._no_limits: dict[str, VendorAdapter] = {}
        # row_id -> (status, note) a part on the retry list gets if it isn't retried
        self._retry_why: dict[str, tuple[RowStatus, Optional[str]]] = {}
        # row_id of a searched row -> later rows of the same login asking the same
        # part number: they use its search and are recorded right after it
        # (same_part_groups, 2026-10-30).
        self._twins: dict[str, list[RequestRow]] = {}

    def _vendor_key(self, row: RequestRow) -> str:
        """The row's vendor id (record id, alias or built-in name resolved), as the
        run's per-vendor numbers are keyed."""
        vendor_id = self.vendors.resolve_id(row.vendor_raw) or row.vendor_raw
        self.summary.vendor_names.setdefault(vendor_id, self.vendors.display_name(vendor_id))
        return vendor_id

    # -- row status helpers ----------------------------------------------
    async def _set(self, rows: list[RequestRow], status: RowStatus, note: Optional[str] = None,
                   extra: Optional[dict] = None, twins: bool = True) -> None:
        """Rows' status. A searched row's twins get it too (a login, limit or
        search error is theirs as well), except from _record: a twin's own
        result can differ (another UOM)."""
        if twins:
            rows = self._with_twins(rows)
        if not rows:
            return
        if self._batch_statuses is not None:
            self._batch_statuses.append((rows, status, note, extra))
            return
        await self._write_statuses([(rows, status, note, extra)])

    def _with_twins(self, rows: list[RequestRow]) -> list[RequestRow]:
        out = {r.row_id: r for row in rows for r in (row, *self._twins.get(row.row_id, ()))}
        return list(out.values())

    async def _write_statuses(self, items: list[tuple]) -> None:
        """(rows, status, note, extra) items to the Requests tab in one write
        request, then logged and recorded. A status write can be sent twice
        safely, so it's retried, after a 429 once the quota's minute has passed."""
        items = [i for i in items if i[0]]
        if not items:
            return
        set_statuses = getattr(self.d.sheets, "set_statuses", None)
        delays, quota_waits, attempt = list(RETRY_DELAYS), 0, 0
        while True:
            attempt += 1
            try:
                if set_statuses is not None:
                    await asyncio.to_thread(set_statuses, [(r, s, n) for r, s, n, _ in items],
                                            self.run_id)
                else:
                    for rows, status, note, _ in items:
                        await asyncio.to_thread(self.d.sheets.set_status, rows, status,
                                                self.run_id, note)
                break
            except Exception as exc:
                if over_quota(exc) and quota_waits < QUOTA_RETRIES:
                    quota_waits += 1
                    wait = QUOTA_WAIT
                elif delays:
                    wait = delays.pop(0)
                else:
                    raise
                logger.warning("Status write attempt %d failed (%r); again in %.0fs", attempt,
                               exc, wait)
                await asyncio.sleep(wait)
        for rows, status, note, extra in items:
            self._note_status(rows, status, note, extra)

    def _note_status(self, rows: list[RequestRow], status: RowStatus, note: Optional[str],
                     extra: Optional[dict]) -> None:
        for row in rows:
            logger.info("Row %d %s (%s): -> %s%s", row.sheet_row, row.part_number,
                        row.vendor_raw, status.value, f" | {note}" if note else "")
            self.summary.counts[status.value] += 1
            tally = self.summary.vendors[self._vendor_key(row)]
            tally.parts += 1
            tally.counts[status.value] += 1
            tally.offers += int((extra or {}).get("offers") or 0)
            if status != RowStatus.DONE:
                self.summary.not_done.append((row.sheet_row, row.part_number, self._vendor_key(row),
                                              status.value, note))
            self.d.store.record_row(row.row_id, {
                "rfq_id": row.rfq_id, "vendor_id": row.vendor_id, "part_number": row.part_number,
                "requested_by": row.requester, "status": status.value, "run_id": self.run_id,
                "error": note if status in (RowStatus.FAILED, RowStatus.NEEDS_ATTENTION) else None,
                "updated_at": time.time(), **(extra or {}),
            })

    # -- top level --------------------------------------------------------
    async def run(self) -> dict:
        self.d.store.update_job(self.run_id, {"started_at": self.summary.started_at,
                                              "status": "RUNNING"})
        seen: set[int] = set()
        try:
            await self._write_unsaved_results()
            # Keep going while new rows keep arriving during the run.
            first = True
            while True:
                rows = [r for r in await asyncio.to_thread(self.d.sheets.read_new_rows, first)
                        if r.sheet_row not in seen]
                if not rows:
                    if first:
                        logger.info("Nothing to process: only rows with a part number and an "
                                    "empty or NEW Status are picked up")
                    break
                first = False
                seen.update(r.sheet_row for r in rows)
                logger.info("Run %s picked up %d NEW rows", self.run_id, len(rows))
                await asyncio.to_thread(self.d.sheets.set_status, rows, RowStatus.RUNNING, self.run_id)
                await self._process_batch(rows)
            await self._save_default_limits()
            status = "DONE"
        except Exception:
            status = "FAILED"
            raise
        finally:
            result = self.summary.as_dict()
            self.d.store.update_job(self.run_id, {
                "finished_at": time.time(), "status": status,
                "rows_total": result["rows_total"], "counts": result["counts"],
                "cost": result["llm"],
            })
        logger.info("Run summary: %s", result)
        warnings = self._check_page_changes()
        # A run in which every row only went back to NEW (a vendor limit, a busy
        # account) posts nothing: the sweep would repeat it every 15 minutes, and
        # the limit itself was already announced once that day.
        if any(status != RowStatus.NEW.value for status in result["counts"]):
            await self.d.notifier.send(summary_message(
                self.summary, result, getattr(self.d.sheets, "sheet_id", ""), self.d.llm_label,
                warnings))
        return result

    def _check_page_changes(self) -> dict:
        """Compare each vendor's run counters with its recent runs, save this run
        to the vendor's health history, and return {vendor id: [warning lines]}
        for vendors whose pages may have changed. Never raises: a detector fault
        must not fail a run."""
        warnings: dict[str, list] = {}
        record_base = {"run_id": self.run_id, "finished_at": time.time()}
        for vendor_id, tally in self.summary.vendors.items():
            metrics = tally.health_metrics()
            if not metrics["parts"]:
                continue
            try:
                history = self.d.store.get_vendor_health(vendor_id)
                lines = health.evaluate(metrics, history)
                self.d.store.append_vendor_health(vendor_id, {**record_base, **metrics})
            except Exception:                 # pragma: no cover - store hiccup
                logger.exception("Page-change check failed for %s", vendor_id)
                continue
            if lines:
                name = self.summary.vendor_names.get(vendor_id, vendor_id)
                logger.warning("Possible page change at %s: %s", name, "; ".join(lines))
                warnings[vendor_id] = lines
        if warnings:
            self.d.store.update_job(self.run_id, {"page_change_warnings": warnings})
        return warnings

    async def _process_batch(self, rows: list[RequestRow]) -> None:
        by_account: dict[str, tuple[VendorCredential, list[RequestRow]]] = {}
        by_requester: dict[tuple[str, str], list[RequestRow]] = defaultdict(list)
        unknown: dict[tuple[str, str], list[RequestRow]] = defaultdict(list)

        for row in rows:
            vendor_id = self.vendors.resolve_id(row.vendor_raw)
            if not vendor_id:
                await self._set([row], RowStatus.FAILED, "Vendor is empty")
            elif not self.vendors.enabled(vendor_id):
                await self._set([row], RowStatus.FAILED, self.vendors.problem(vendor_id))
            elif self.vendors.record(vendor_id) is None and not self.vendors.has_adapter(vendor_id):
                unknown[(vendor_id, row.requester)].append(row)
            elif self.d.adapter_factory(vendor_id, {}, self.vendors.record(vendor_id) or {}) is None:
                await self._set([row], RowStatus.FAILED, self.vendors.problem(vendor_id)
                                or f"Vendor '{row.vendor_raw}' is not supported yet")
            else:
                by_requester[(vendor_id, row.requester)].append(row)

        # A vendor no one has set up yet: its login secret is all it needs.
        for (vendor_id, requester), group in unknown.items():
            if await self._set_up_from_secret(vendor_id, requester, group):
                by_requester[(vendor_id, requester)].extend(group)

        for (vendor_id, requester), group in by_requester.items():
            try:
                cred = await self.d.creds.resolve(vendor_id, requester)
            except CredentialNotFound as exc:
                await self._set(group, RowStatus.NO_CREDENTIALS, str(exc))
                await self.d.notifier.send(
                    f"No {vendor_id} login found for you, so {len(group)} part(s) were skipped. "
                    f"Ask the admin to add it in Secret Manager ({exc}).", to=requester)
                continue
            except CredentialError as exc:
                await self._set(group, RowStatus.FAILED, f"Credential error: {exc}")
                continue
            aid = account_id(cred.vendor_id, cred.owner)
            logger.info("%s / %s: using %s login (secret key %s, totp=%s) for %d row(s)",
                        vendor_id, requester or "(no requester)",
                        "shared" if cred.is_shared else "personal", cred.secret_key,
                        "yes" if cred.totp_secret else "no", len(group))
            if aid in by_account:
                by_account[aid][1].extend(group)
            else:
                by_account[aid] = (cred, list(group))

        await asyncio.gather(*(self._process_account(cred, group)
                               for cred, group in by_account.values()))

    async def _set_up_from_secret(self, vendor_id: str, requester: str,
                                  rows: list[RequestRow]) -> bool:
        """Set up a vendor that has a login secret but no record and no adapter
        code: a generic-adapter record whose login site comes from the secret's
        `url`. False (rows get their status and the requester a Chat message)
        when there is no secret or it can't be used."""
        if self.vendors.record(vendor_id) is not None:      # set up for another requester
            return True
        name = rows[0].vendor_raw.strip()
        try:
            cred = await self.d.creds.resolve(vendor_id, requester)
        except CredentialNotFound:
            note = (f"Vendor '{name}' is not set up: no login secret "
                    f"{secret_key(vendor_id, SHARED)}. The admin adds it in Secret Manager with "
                    "the vendor's login page as 'url', plus 'username' and 'password'")
            await self._set(rows, RowStatus.NO_CREDENTIALS, note)
            await self.d.notifier.send(note, to=requester)
            return False
        except CredentialError as exc:
            await self._set(rows, RowStatus.FAILED, f"Credential error: {exc}")
            return False
        record, problem = record_from_secret(name, cred)
        if record is None:
            await self._set(rows, RowStatus.NEEDS_ATTENTION, problem)
            await self.d.notifier.send(f"Vendor '{name}': {problem}", to=requester)
            return False
        self.vendors.add(vendor_id, record)
        self.summary.new_vendors[vendor_id] = cred.secret_key
        self.summary.vendor_names[vendor_id] = record["display_name"]
        logger.info("Vendor %s set up from secret %s: generic adapter, login sites %s",
                    vendor_id, cred.secret_key, ", ".join(record["allowed_hosts"]))
        return True

    def _first_run_check(self, adapter: VendorAdapter, vendor_key: str) -> None:
        """A vendor's first run that finished a search: stop flagging its results."""
        if adapter.config.get("first_run_done") is not False:
            return
        tally = self.summary.vendors[vendor_key].counts
        if any(tally.get(s.value) for s in (RowStatus.DONE, RowStatus.NOT_FOUND,
                                             RowStatus.NOT_SOLD)):
            self.d.store.update_vendor(vendor_key, {"first_run_done": True,
                                                    "first_run_id": self.run_id})
            self.summary.first_runs.add(vendor_key)

    @staticmethod
    def _flag(adapter: VendorAdapter, flag: str) -> str:
        if adapter.config.get("first_run_done") is False:
            return ",".join(f for f in (flag, NEW_VENDOR_FLAG) if f)
        return flag

    # -- one account -----------------------------------------------------
    async def _process_account(self, cred: VendorCredential, rows: list[RequestRow]) -> None:
        aid = account_id(cred.vendor_id, cred.owner)
        vendor_id = cred.vendor_id
        adapter = self.d.adapter_factory(vendor_id, self.d.store.get_selectors(vendor_id),
                                         self.d.store.get_vendor(vendor_id) or {})
        limits = adapter.limits
        if not adapter.limits_set and adapter.real_portal:
            self._no_limits.setdefault(vendor_id, adapter)
        # Before any browser or login: a vendor over its limits isn't touched.
        stop = self.limiter.block(vendor_id, limits)
        if stop:
            await self._wait_for_limit(adapter, vendor_id, rows, str(stop), stop.kind)
            return
        if not self.d.store.acquire_lease(aid, self.run_id, self.d.lease_seconds):
            await self._set(rows, RowStatus.NEW, f"Account {aid} is busy; will retry")
            return

        handle = None
        signed_in = False
        pending = []
        for lead, twins in same_part_groups(rows):
            pending.append(lead)
            if twins:
                self._twins[lead.row_id] = twins
                logger.info("[%s] Rows %s: same part %s as row %d, one search for all",
                            aid, ",".join(str(t.sheet_row) for t in twins), lead.part_number,
                            lead.sheet_row)
        # Searched again one by one at the end, before logout: parts a bulk search
        # didn't find, empty results pages and searches that timed out or failed
        # (vendor config retry_not_found=false turns this off).
        retry: Optional[list[RequestRow]] = (
            [] if adapter.config.get("retry_not_found", True) else None)
        retry_max = int(adapter.config.get("retry_not_found_max", RETRY_NOT_FOUND_MAX))
        # Search strategy: bulk only for runs with at least bulk_min_parts parts
        # (vendor config); otherwise the site's one-part search, part by part.
        # REMOVE-IF-PER-PART-DROPPED: the `min_parts is not None` checks here (None =
        # --one-by-one). Keep _process_row and the adapter's search(): single-part
        # runs, the bulk fallback and the end-of-run retry all use them.
        min_parts = adapter.bulk_min_parts
        bulk_ok = (adapter.bulk_batch_size > 1 and min_parts is not None
                   and len(pending) >= min_parts)
        bulk_failures = 0
        bulk_size = adapter.bulk_batch_size  # halved when a batch's results are too big
        self.summary.vendors[self._vendor_key(rows[0])].mode = (
            f"bulk search, up to {adapter.bulk_batch_size} per search" if bulk_ok
            else "one by one")
        if bulk_ok:
            logger.info("[%s] Bulk search: %d parts (bulk from %d), up to %d per search", aid,
                        len(pending), min_parts, adapter.bulk_batch_size)
        else:
            logger.info("[%s] One search per part: %d part(s)%s", aid, len(pending),
                        "" if min_parts is None or adapter.bulk_batch_size <= 1
                        else f" (bulk from {min_parts})")
        try:
            people = tuple(sorted({r.requester for r in rows if r.requester}))
            handle = await self.auth.open_authenticated(adapter, cred, people)
            signed_in = True
            relogged = False
            while pending or retry:
                if not pending:
                    # Last pass: one more search per part that came back without a
                    # usable answer (not in the bulk results, empty page, error).
                    pending, skipped, retry = retry[:retry_max], retry[retry_max:], None
                    logger.info("[%s] Searching %d part(s) again one by one before logout "
                                "(not found in bulk, empty page or search error)",
                                aid, len(pending))
                    if skipped:
                        logger.info("[%s] %d more not retried (retry_not_found_max=%d)",
                                    aid, len(skipped), retry_max)
                        for row in skipped:
                            status, note = self._retry_why.get(
                                row.row_id, (RowStatus.NOT_FOUND, None))
                            await self._set([row], status, note)
                    bulk_ok = False
                    continue
                try:
                    if bulk_ok and len(pending) > 1:
                        batch = pending[:bulk_size]
                        await self.limiter.before_request(vendor_id, limits, len(batch))
                        result = await self._process_bulk(adapter, handle, batch, retry)
                        if result == BULK_OK:
                            del pending[:len(batch)]
                            bulk_failures = 0
                        elif result == BULK_TOO_BIG:
                            # More results than one search can return in full: the
                            # same parts again in smaller batches, kept for this run.
                            bulk_size = len(batch) // 2
                            if bulk_size < 2:
                                bulk_ok = False
                                logger.warning("[%s] Bulk results too big even for %d parts; "
                                               "searching the rest one by one", aid, len(batch))
                            else:
                                logger.info("[%s] Bulk results too big for %d parts; trying "
                                            "%d per search", aid, len(batch), bulk_size)
                        else:
                            # One failure (a slow page, a stuck dropdown) retries the
                            # same batch in bulk; two in a row turn bulk off.
                            bulk_failures += 1
                            if bulk_failures >= 2:
                                bulk_ok = False  # one-by-one for the rest of this run
                                logger.warning("[%s] Bulk search failed twice in a row; "
                                               "searching the rest one by one", aid)
                            else:
                                logger.info("[%s] Trying the same %d parts in bulk again",
                                            aid, len(batch))
                        continue
                    await self.limiter.before_request(vendor_id, limits)
                    await self._process_row(adapter, handle, pending[0], retry)
                    pending.pop(0)
                except SessionExpired as exc:
                    if relogged:
                        raise CannotLogin(AccountState.NEEDS_ATTENTION,
                                          "Session expired again right after re-login")
                    logger.info("[%s] Session expired (%s); re-login once and retry", aid, exc)
                    relogged = True
                    signed_in = False
                    await self.auth.login(handle, adapter, cred, people)
                    signed_in = True
        except (LimitReached, LoginDeferred) as exc:
            pending = pending + (retry or [])  # parts still waiting for their retry
            await self._wait_for_limit(adapter, vendor_id, pending, str(exc),
                                       "login" if isinstance(exc, LoginDeferred) else exc.kind)
        except RateLimited as exc:
            pending = pending + (retry or [])
            self.limiter.back_off(vendor_id, limits)
            self.d.store.audit("vendor_rate_limited", vendor_id=vendor_id, reason=str(exc))
            logger.warning("[%s] %s; no searches on %s for %d min", aid, exc,
                           adapter.display_name, limits["backoff_minutes"])
            await self._wait_for_limit(
                adapter, vendor_id, pending,
                f"the portal said we sent too many requests ({exc}); paused for "
                f"{limits['backoff_minutes']} min", "rate_limited")
        except NeedsHuman as exc:
            pending = pending + (retry or [])  # parts still waiting for their retry
            # Asked but no code: nothing is wrong with the account, so a person can
            # retry by setting the rows back to NEW (the rows themselves are not
            # retried by the sweep: each try sends the person another code).
            # Not asked (no handoff, no code box found): a developer has to look.
            state = AccountState.NEEDS_LOGIN if exc.asked else AccountState.NEEDS_ATTENTION
            self.d.store.update_account(aid, {"state": state.value, "last_error": str(exc)})
            why = ("no code arrived in time or it was not accepted" if exc.asked else
                   "this run can't ask a person for it")
            await self._set(pending, RowStatus.NEEDS_ATTENTION,
                            f"{adapter.display_name} needs a 2FA code ({exc.method}); {why}")
            await self._notify_requesters(
                pending, f"{adapter.display_name} is asking for a 2FA code ({exc.method}) and "
                f"{why}; {len(pending)} part(s) are waiting. Set their Status back to NEW to "
                "try again.")
        except CannotLogin as exc:
            pending = pending + (retry or [])  # parts still waiting for their retry
            status = (RowStatus.NO_CREDENTIALS
                      if exc.state == AccountState.NEEDS_CREDENTIAL_UPDATE
                      else RowStatus.NEEDS_ATTENTION)
            await self._set(pending, status, exc.reason)
            fix = ""
            if exc.state == AccountState.NEEDS_CREDENTIAL_UPDATE and self.d.public_base_url:
                fix = (" Update the password here, then set the parts' Status back to NEW: "
                       f"{self.d.public_base_url.rstrip('/')}/credentials/update?"
                       + urlencode({"vendor": cred.vendor_id, "owner": cred.owner}))
            await self._notify_requesters(
                pending, f"{adapter.display_name} login for {cred.owner} stopped: {exc.reason} "
                f"(account is now {exc.state.value}). {len(pending)} part(s) are waiting.{fix}")
        except LlmKeyRejected as exc:
            pending = pending + (retry or [])
            logger.error("[%s] The LLM provider rejected the API key (%s); no more searches "
                         "on %s this run", aid, exc, adapter.display_name)
            await self._set(pending, RowStatus.FAILED,
                            f"LLM API key rejected ({exc}); not searched. Fix the key, then "
                            "set Status back to NEW"[:300])
        except Exception as exc:
            if llm_key_rejected(exc):            # e.g. while reading the login page
                logger.error("[%s] The LLM provider rejected the API key: %s", aid, exc)
            else:
                logger.exception("[%s] Unexpected error", aid)
            pending = pending + (retry or [])  # parts still waiting for their retry
            await self._set(pending, RowStatus.FAILED, f"Unexpected error: {exc!r}"[:300])
        finally:
            events = getattr(getattr(adapter, "assist", None), "events", None) or []
            if events:
                logger.warning("[%s] Page actions the code couldn't do alone: %s", aid,
                               "; ".join(events))
                self.summary.vendors[self._vendor_key(rows[0])].page_actions.extend(events)
            confirmed = adapter.confirmed_learned()
            if confirmed:
                self.d.store.save_selectors(vendor_id, confirmed)
            if len(confirmed) < len(adapter.learned):
                logger.info("[%s] Not saved (no sign-in / search confirmed them): %s", aid,
                            ", ".join(sorted(set(adapter.learned) - set(confirmed))))
            self._first_run_check(adapter, self._vendor_key(rows[0]))
            if handle is not None:
                people = tuple(sorted({r.requester for r in rows if r.requester}))
                if signed_in and adapter.config.get("logout_after_run", True):
                    await self._logout(adapter, handle, aid, people)
                elif not signed_in:
                    # A re-login that failed may still have opened a session.
                    await self.auth.logout_if_maybe_signed_in(adapter, handle, aid, people)
                await self.d.browser.close(handle)
                logger.info("[%s] Browser session %s released", aid, handle.session_id)
            self.d.store.release_lease(aid, self.run_id)

    async def _logout(self, adapter: VendorAdapter, handle, aid: str, people: tuple = ()) -> None:
        """Sign out of the portal so no vendor session is left behind: an open one
        blocks the team's own sign-in elsewhere (Aviall, 2026-11-02). Two tries,
        then the requesters are asked to log out through Live View (hitl.py). The
        next run then logs in again (set logout_after_run=false in the vendor
        config to keep the session in the saved Context instead)."""
        try:
            ok = await logout_with_handoff(
                adapter, handle.page, self.d.code_handoff, account=aid, people=people,
                live_view_url=lambda: self.d.browser.live_view_url(handle.session_id))
        except Exception as exc:
            logger.warning("[%s] Logout failed: %r", aid, exc)
            ok = False
        if not ok and getattr(self.d.browser, "waits_for_logout", False):
            # Never close a local browser signed in (team rule, 2026-11-04).
            ok = await hold_until_logged_out(aid, adapter, handle, self.d.browser)
        if ok:
            self.d.store.update_account(aid, {"last_login_at": None, "last_logout_at": time.time(),
                                              "state": AccountState.NEEDS_LOGIN.value})
            self.d.store.audit("logout_ok", account_id=aid)
            logger.info("[%s] Signed out of %s", aid, adapter.display_name)
        else:
            logger.warning("[%s] Could not confirm sign-out of %s; the vendor session stays "
                           "in the saved Context until the portal expires it",
                           aid, adapter.display_name)

    async def _wait_for_limit(self, adapter: VendorAdapter, vendor_id: str,
                              rows: list[RequestRow], reason: str, kind: str) -> None:
        """Rows a vendor limit holds back: Status NEW again with the reason, so a
        later run searches them. The requesters hear about it once a day per
        vendor and kind, not on every 15-minute sweep."""
        if not rows:
            return
        note = f"{adapter.display_name}: {reason}"
        await self._set(rows, RowStatus.NEW, note)
        if self.limiter.should_notify(vendor_id, adapter.limits, kind):
            await self._notify_requesters(
                rows, f"{note}. {len(rows)} part(s) are waiting and will be searched "
                "automatically once the limit allows.")

    async def _save_default_limits(self) -> None:
        """A vendor that ran on the default limits gets an empty, unconfirmed
        `limits` in its record, and one Chat message asks for the vendor's real
        rules; once saved the message isn't sent again. Empty, not a copy of the
        values: the vendor keeps following the current defaults (code and adapter)
        until a person sets its own."""
        for vendor_id, adapter in self._no_limits.items():
            record = self.d.store.get_vendor(vendor_id) or {}
            if isinstance(record.get("limits"), dict):
                continue
            limits = {**DEFAULT_LIMITS, **adapter.default_limits}
            self.d.store.update_vendor(vendor_id, {
                "limits": {}, "limits_confirmed": False,
                "limits_saved_at": time.time(), "limits_saved_by": f"run {self.run_id} (defaults)"})
            self.d.store.audit("vendor_limits_defaulted", vendor_id=vendor_id)
            logger.info("Vendor %s had no limits; saved the defaults (unconfirmed)", vendor_id)
            await self.d.notifier.send(
                f"Vendor {adapter.display_name} has no login/search limits set, so the safe "
                f"defaults were used and saved (unconfirmed): locks after "
                f"{limits['vendor_lockout_after']} failed login(s) in "
                f"{limits['lockout_window_minutes'] // 60}h, at most {limits['max_logins_per_day']} "
                f"logins a day, {limits['min_seconds_between_requests']:g}s between searches, "
                f"{limits['max_searches_per_run']} searches per run and "
                f"{limits['max_searches_per_day']} per day. Please get {adapter.display_name}'s "
                "real rules (how many failed logins lock the account, any search limits) and "
                f"set them: python scripts/vendor_limits.py set {vendor_id} KEY=VALUE ...")

    async def _notify_requesters(self, rows: list[RequestRow], text: str) -> None:
        requesters = sorted({r.requester for r in rows if r.requester})
        for requester in requesters or [None]:
            await self.d.notifier.send(text, to=requester)

    def _retry_later(self, retry: Optional[list[RequestRow]], row: RequestRow,
                     status: RowStatus, note: Optional[str]) -> bool:
        """Put the row on the end-of-run retry list. False when there is none
        (retry_not_found off, or this is already the retry pass)."""
        if retry is None:
            return False
        logger.info("Row %d: %s; searching it again before logout", row.sheet_row,
                    note or status.value)
        self._retry_why[row.row_id] = (status, note)
        retry.append(row)
        return True

    async def _process_row(self, adapter: VendorAdapter, handle, row: RequestRow,
                           retry: Optional[list[RequestRow]] = None) -> None:
        """One search for one part. A timeout, an error or an empty results page
        goes on `retry` (when given) instead of getting its final status."""
        started = time.time()
        logger.info("Row %d: searching %s for %s", row.sheet_row, adapter.display_name,
                    row.part_number)
        limit = float(adapter.config.get("search_timeout_seconds", SEARCH_TIMEOUT_SECONDS))
        try:
            outcome: SearchOutcome = await asyncio.wait_for(adapter.search(handle.page, row), limit)
        except (SessionExpired, RateLimited):
            raise
        except asyncio.TimeoutError:
            logger.warning("Row %d: search stopped, no result within %ds (page stuck?)",
                           row.sheet_row, limit)
            note = f"Search timed out after {limit:.0f}s"
            if not self._retry_later(retry, row, RowStatus.FAILED, note):
                await self._set([row], RowStatus.FAILED, note,
                                {"seconds": round(time.time() - started, 1)})
            return
        except Exception as exc:
            if llm_key_rejected(exc):
                raise LlmKeyRejected(str(exc).splitlines()[0][:200]) from exc
            logger.exception("Search failed for %s", row.part_number)
            note = f"Search error: {exc!r}"[:300]
            if not self._retry_later(retry, row, RowStatus.FAILED, note):
                await self._set([row], RowStatus.FAILED, note,
                                {"seconds": round(time.time() - started, 1)})
            return
        self.summary.usage.add(outcome.usage)
        self.summary.vendors[self._vendor_key(row)].usage.add(outcome.usage)
        timing = {"seconds": round(time.time() - started, 1), "llm": outcome.usage.as_dict()}
        if not outcome.found and not outcome.not_sold:
            # NOT_FOUND only when we can prove we were still signed in.
            if not await adapter.is_authenticated(handle.page):
                raise SessionExpired("Auth probe failed after an empty result")
            if outcome.empty_page and self._retry_later(
                    retry, row, RowStatus.NOT_FOUND, EMPTY_PAGE_NOTE):
                return
        await self._record(adapter, row, outcome, timing)

    async def _process_bulk(self, adapter: VendorAdapter, handle, batch: list[RequestRow],
                            retry: Optional[list[RequestRow]] = None) -> str:
        """One search for the whole batch: BULK_OK, or BULK_TOO_BIG / BULK_FAILED
        with nothing recorded (the caller retries smaller, again, or one by one).
        Parts the bulk search didn't find go to `retry` (when given) instead of
        being marked NOT_FOUND, so the caller can search them one by one before
        logging out."""
        started = time.time()
        logger.info("Rows %s: bulk searching %s for %d parts",
                    ",".join(str(r.sheet_row) for r in batch), adapter.display_name, len(batch))
        limit = float(adapter.config.get("bulk_timeout_seconds", BULK_TIMEOUT_SECONDS))
        try:
            outcomes, usage = await asyncio.wait_for(
                adapter.search_many(handle.page, batch), limit)
        except (SessionExpired, RateLimited):
            raise
        except asyncio.TimeoutError:
            logger.warning("Bulk search stopped: no result within %ds (page stuck?)", limit)
            return BULK_FAILED
        except BulkResultsIncomplete as exc:
            logger.warning("Bulk search incomplete (%s)", str(exc).splitlines()[0][:300])
            return BULK_TOO_BIG
        except Exception as exc:
            if llm_key_rejected(exc):
                raise LlmKeyRejected(str(exc).splitlines()[0][:200]) from exc
            logger.warning("Bulk search failed (%s)", str(exc).splitlines()[0][:300])
            return BULK_FAILED
        self.summary.usage.add(usage)
        self.summary.vendors[self._vendor_key(batch[0])].usage.add(usage)
        missing = [r for r in batch if r.row_id not in outcomes]
        if missing and not await adapter.is_authenticated(handle.page):
            raise SessionExpired("Auth probe failed after a bulk search with missing parts")
        share = usage.cost_usd / len(batch)
        seconds = round(time.time() - started, 1)
        # "Answered" includes the vendor's own "not found" (Incora's × Stock Not Found);
        # "no answer" is a part the results page didn't mention at all.
        logger.info("Bulk search: %d of %d part(s) answered by the page (%d with offers), "
                    "%d with no answer, %.1fs, LLM %d call(s) $%.4f",
                    len(batch) - len(missing), len(batch),
                    sum(1 for o in outcomes.values() if getattr(o, "offers", None)),
                    len(missing), seconds, usage.calls, usage.cost_usd)
        # Every part's lines and status collected, then saved in one go: 3 sheet
        # writes for the batch (Results, RFQ tab, statuses).
        self._batch_results, self._batch_statuses = [], []
        try:
            for row in batch:
                outcome = outcomes.get(row.row_id)
                if outcome is None and retry is not None:
                    retry.append(row)
                    continue
                outcome = outcome or SearchOutcome(found=False)
                await self._record(adapter, row, outcome, {
                    "seconds": seconds, "bulk_batch": len(batch),
                    "llm": {"cost_usd_share": round(share, 6), "batch_calls": usage.calls},
                })
        finally:
            entries, statuses = self._batch_results, self._batch_statuses
            self._batch_results = self._batch_statuses = None
            await self._save_results(entries, statuses)
        return BULK_OK

    async def _record(self, adapter: VendorAdapter, row: RequestRow, outcome: SearchOutcome,
                      timing: dict) -> None:
        """Write a row's offers to the sink and set its status, then the same for
        each row asking the same part (its twins), from this search: their lines
        follow this row's, in the same write in a bulk batch."""
        copies = [(t, twin_outcome(outcome, row, t)) for t in self._twins.get(row.row_id, ())]
        await self._record_one(adapter, row, outcome, timing)
        for twin, copy in copies:
            logger.info("Row %d: same part as row %d, using its search", twin.sheet_row,
                        row.sheet_row)
            await self._record_one(adapter, twin, copy, {
                **timing, "llm": LlmUsage().as_dict(), "same_search_as_row": row.sheet_row})

    async def _record_one(self, adapter: VendorAdapter, row: RequestRow,
                          outcome: SearchOutcome, timing: dict) -> None:
        # Page-change signals, once per real search (a twin reuses the search).
        if "same_search_as_row" not in timing:
            tally = self.summary.vendors[self._vendor_key(row)]
            if outcome.grid_used is not None:
                tally.grid_reads += 1
                tally.grid_fallback += int(outcome.grid_used is False)
            if outcome.page_kind:
                tally.page_reads += 1
                tally.bad_page_kind += int(outcome.page_kind in health.BAD_PAGE_KINDS)
            for off in outcome.offers:
                tally.extracted_offers += 1
                tally.check_page_offers += int(bool(off.review))
        usage = outcome.usage.as_dict()
        logger.info("Row %d: %s, %d offer(s), %.1fs, LLM %s call(s) $%.4f", row.sheet_row,
                    "found" if outcome.found else "no results", len(outcome.offers),
                    timing.get("seconds", 0.0), usage.get("calls", 0), usage.get("cost_usd", 0.0))
        for offer in outcome.offers:
            logger.info("Row %d offer: pn=%s price=%s %s qty=%s cond=%s lead=%s%s", row.sheet_row,
                        offer.part_number, offer.price, offer.currency or "",
                        offer.qty_available, offer.condition, offer.lead_time,
                        f" | flags: {offer.review}" if offer.review else "")
        if outcome.not_sold:
            await self._record_not_sold(adapter, row, outcome, timing)
            return
        if not outcome.found:
            await self._set([row], RowStatus.NOT_FOUND,
                            EMPTY_PAGE_NOTE if outcome.empty_page else None, timing,
                            twins=False)
            return

        # Only the requested part itself and the substitutes the portal names:
        # a look-alike number (ACHC3-13 for ACHC-3-13) would put two different
        # numbers in Requested / Quoted, which the client never does (2026-10-23).
        # Substitutes only for vendors that show them (not Wencor / Aviall, 2026-11-03).
        subs = getattr(adapter, "show_substitutes", True)
        offers = [o for o in outcome.offers
                  if (o.listing == "substitute" and subs)
                  or (o.listing != "substitute" and same_part(o.part_number, row.part_number))]
        others = sorted({o.part_number for o in outcome.offers if o not in offers})
        if others:
            logger.info("Row %d: not written, other part numbers: %s", row.sheet_row,
                        ", ".join(others))
        if not offers:
            await self._set([row], RowStatus.NOT_FOUND,
                            f"Only other part numbers listed: {', '.join(others)}"[:250], timing,
                            twins=False)
            return
        # Lines in another unit than the request's are never quoted, nor converted.
        units = sorted({uom_code(o.uom) for o in offers if not same_uom(o.uom, row.uom)})
        offers = [o for o in offers if same_uom(o.uom, row.uom)]
        if units:
            logger.info("Row %d: not written, other UOM: %s (asked %s)", row.sheet_row,
                        ", ".join(units), uom_code(row.uom))
        if not offers:
            await self._set([row], RowStatus.NOT_FOUND,
                            f"No {uom_code(row.uom)} lines on {adapter.display_name} "
                            f"(only {', '.join(units)})"[:250], timing,
                            twins=False)
            return

        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        vendor = adapter.display_name
        lines = []
        for o in offers:
            stocked = in_stock(o.qty_available)
            # Out-of-stock lines keep a price the page shows, for comparison
            # (client, 2026-10-23); no stock shown at all: no price (extract.py 2.2).
            priced = stocked or bool(o.price)
            price = o.price or missing(vendor, o, "PRICE")
            currency = (o.currency or adapter.currency) if o.price else ""
            lines.append((o, ResultRow(
                rfq_id=row.rfq_id, vendor=vendor,
                requested_part=row.part_number, qty_needed=row.qty_needed or "",
                part_number=o.part_number,
                price=price if priced else "",
                qty_price=qty_price(o, row, price) if priced else "",
                quote_price=quote_price(o, row, price) if priced else "",
                # Only next to a price; the adapter's currency when the page shows just "$".
                currency=currency,
                qty_available=o.qty_available or missing(vendor, o, "QTY"),
                # Most parts have no minimum: blank unless verification removed one.
                moq=o.moq or (CHECK_PAGE if "MOQ_NOT_ON_PAGE" in (o.review or "") else ""),
                min_order_qty=_qty_text(minimum) if (minimum := min_order(o)[0]) else "",
                warehouses=o.warehouses or missing(vendor, o, "WAREHOUSES"),
                # default_condition (vendor config): for a portal that never shows a
                # condition but only sells one kind (set only once the client confirms).
                condition=(o.condition or normalize_condition(
                    adapter.config.get("default_condition")) or missing(vendor, o, "CONDITION"))
                if priced else "",
                lead_time=lead_days(o, vendor),
                # RFQ Delivery: 0 in stock, else the lead time (client, 2026-10-23);
                # in stock at a vendor with a fixed shipping time, that time (Incora 7,
                # client 2026-11-06). A No Quote line gets none (as_rfq_record).
                delivery=(adapter.delivery_days or "0") if stocked else lead_days(o, vendor),
                source_url=o.source_url or "", fetched_at=fetched_at,
                requested_by=row.requester, flag=self._flag(adapter, flag_for(o, row)),
                # MOQ and stock location, price breaks; alternate_notes adds any
                # substitute note below them.
                internal_comments=join_comments(
                    stock_note(o, row.qty_needed, priced=priced, stocked=stocked),
                    price_break_note(o, currency) if priced else "", o.vendor_note or ""),
                **request_details(row, o),
            )))
        for o, r in lines:
            # An in-stock line must show its condition (client, 2026-10-23); when
            # the page gave none, say so, as the RFQ tab makes it "No Quote". A
            # priced out-of-stock line with a delivery is quoted without one
            # (2026-10-27), so a person fills in its Condition Code.
            quoted = in_stock(o.qty_available) or rfq_quote(r)["Type Quoted"] == "Outright"
            if quoted and r.condition not in CONDITION_CODES:
                r.flag = ",".join(f for f in (r.flag, "NO_CONDITION") if f)
        results = alternate_notes(row, lines)
        add_listed_alternates(outcome.alternates, results)
        await self._write_results(row, results, RowStatus.DONE, None,
                                  {**timing, "offers": len(results)})

    async def _write_results(self, row: RequestRow, results: list[ResultRow], status: RowStatus,
                             note: Optional[str], extra: dict) -> None:
        """Write a row's Results rows, then set its status; inside a bulk batch
        they're collected and saved with the batch's other rows."""
        if self._batch_results is not None:
            self._batch_results.append((row, results, status, note, extra))
            return
        await self._save_results([(row, results, status, note, extra)])

    async def _save_results(self, entries: list[tuple], statuses: list[tuple] = ()) -> None:
        """Results lines of every (row, results, status, note, extra) entry in one
        sink write (one Results append, one RFQ append), then every row's status,
        `statuses` included, in one status write. When the Results write fails
        after its retries, every row's lines are kept in the store and written at
        the start of the next run (the search isn't repeated) and the rows are
        FAILED; when only the RFQ tab fails, the rows keep their status and only
        the RFQ lines are kept (2026-10-27, Row 18)."""
        lines = [r for _, results, _, _, _ in entries for r in results]
        outcomes = await self.d.sink.write(lines) if lines else []
        error = next((o.error for o in outcomes if not o.ok), None)
        rfq_error = next((o.rfq_error for o in outcomes if getattr(o, "rfq_error", None)), None)
        items = list(statuses)
        for row, results, status, note, extra in entries:
            if error:
                self.d.store.record_row(row.row_id, {
                    "unsaved_sheet": row.sheet_id, "unsaved_row": asdict(row),
                    "unsaved_results": [asdict(r) for r in results],
                    "unsaved_status": status.value, "unsaved_note": note,
                })
                items.append(([row], RowStatus.FAILED,
                              f"Could not write results: {error}"[:250] +
                              f" ({len(results)} row(s) kept; written on the next run)", extra))
                continue
            if rfq_error and results:
                self.d.store.record_row(row.row_id, {
                    "unsaved_sheet": row.sheet_id, "unsaved_row": asdict(row),
                    "unsaved_rfq": [asdict(r) for r in results]})
                note = ((note + "; ") if note else "") + (
                    f"RFQ tab not written ({rfq_error[:120]}); written on the next run")
            items.append(([row], status, note, extra))
        if error:
            logger.warning("Results write failed for %d row(s); kept for the next run: %s",
                           len(entries), error)
        await self._write_statuses(items)

    async def _write_unsaved_results(self) -> None:
        """Results a run found but couldn't write (sheet connection dropped):
        write them now and give their rows the status they would have had. Skipped
        when the Requests row now holds another part (rows were deleted above it)."""
        sheet_id = getattr(self.d.sheets, "sheet_id", "")
        for row_id, doc in self.d.store.rows_with_unsaved_results(sheet_id):
            row = RequestRow(**doc["unsaved_row"])
            current = await asyncio.to_thread(self.d.sheets.part_at, row.sheet_row)
            clear = {"unsaved_sheet": None, "unsaved_row": None, "unsaved_results": None,
                     "unsaved_status": None, "unsaved_note": None, "unsaved_rfq": None}
            if (current or "").strip().upper() != row.part_number.strip().upper():
                logger.warning("Row %d: kept results for %s not written: the row now holds %r",
                               row.sheet_row, row.part_number, current)
                self.d.store.record_row(row_id, clear)
                continue
            if not doc.get("unsaved_results"):
                # Only RFQ tab lines were kept: Results and the row's status are fine.
                write_rfq = getattr(self.d.sink, "write_rfq", None)
                rfq = [ResultRow(**r) for r in doc.get("unsaved_rfq") or []]
                if write_rfq is not None and await write_rfq(rfq):
                    logger.warning("Row %d: kept RFQ lines for %s still not written; next run",
                                   row.sheet_row, row.part_number)
                    continue
                self.d.store.record_row(row_id, clear)
                logger.info("Row %d: wrote %d kept RFQ line(s) for %s", row.sheet_row, len(rfq),
                            row.part_number)
                continue
            results = [ResultRow(**r) for r in doc["unsaved_results"]]
            outcomes = await self.d.sink.write(results)
            if not all(o.ok for o in outcomes):
                logger.warning("Row %d: kept results for %s still not written; next run",
                               row.sheet_row, row.part_number)
                continue
            if any(getattr(o, "rfq_error", None) for o in outcomes):
                # Results written, RFQ lines not: keep only those for the next run.
                clear = {"unsaved_results": None, "unsaved_status": None, "unsaved_note": None,
                         "unsaved_rfq": [asdict(r) for r in results]}
            self.d.store.record_row(row_id, clear)
            logger.info("Row %d: wrote %d kept result row(s) for %s", row.sheet_row,
                        len(results), row.part_number)
            note = doc.get("unsaved_note")
            await self._set([row], RowStatus(doc.get("unsaved_status") or RowStatus.DONE.value),
                            (note + "; " if note else "") + "results written on a later run",
                            {"offers": len(results)})

    async def _record_not_sold(self, adapter: VendorAdapter, row: RequestRow,
                               outcome: SearchOutcome, timing: dict) -> None:
        """The portal says it doesn't sell the part: one Results row with its own
        words and the results page, so a team member can check it, and Status NOT_SOLD."""
        vendor = adapter.display_name
        result = ResultRow(
            rfq_id=row.rfq_id, vendor=vendor, requested_part=row.part_number,
            qty_needed=row.qty_needed or "", part_number=row.part_number,
            qty_available=outcome.not_sold or "", source_url=outcome.page_url,
            fetched_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            requested_by=row.requester, flag=self._flag(adapter, "NOT_SOLD"), **request_details(row),
        )
        await self._write_results(row, [result], RowStatus.NOT_SOLD,
                                  f"{vendor}: {outcome.not_sold}", timing)
