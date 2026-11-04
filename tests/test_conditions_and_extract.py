"""Condition codes and the per-offer source URL."""
from __future__ import annotations

import pytest

from sourcing.conditions import normalize_condition
from sourcing.extract import Extractor, format_warehouses
from sourcing.models import LlmUsage


@pytest.mark.parametrize("raw, code", [
    ("As Removed", "AR"), ("ar", "AR"),
    ("Factory New", "FN"), ("FN", "FN"),
    ("New", "NE"), ("NEW", "NE"), ("ne", "NE"),
    ("New Surplus", "NS"), ("NEW SURPLUS", "NS"), ("GOVT NEW SURPLUS", "NS"), ("NS", "NS"),
    ("Overhauled", "OH"), ("OH", "OH"),
    ("Repaired", "RP"), ("RP", "RP"),
    ("Serviceable", "SV"), ("SV", "SV"),
    ("Condition: Factory New", "FN"), ("NE - New", "NE"), ("Govt New Surplus (NS)", "NS"),
])
def test_condition_wordings_map_to_codes(raw, code):
    assert normalize_condition(raw) == code


@pytest.mark.parametrize("raw", ["Inspected/Certified", "Tested", "Service", "Repair",
                                 "Overhaul", None, "  "])
def test_unknown_or_empty_condition_is_left_out(raw):
    # The client's rule: only the listed wordings; no match -> omit the field.
    assert normalize_condition(raw) is None


class _Client:
    def __init__(self, data):
        self.data = data

    async def json_call(self, **_):
        return self.data, LlmUsage(calls=1)


PAGE = "ABC-1 12.00 Qty 3 New\n--- Page details ---\nABC-1 | link: https://vendor.example/p/ABC-1"


@pytest.mark.asyncio
async def test_offer_gets_its_own_link_and_a_condition_code():
    offer = {"part_number": "ABC-1", "price": "12.00", "currency": "USD", "qty_available": "3",
             "condition": "New", "lead_time": "5 days",
             "source_url": "https://vendor.example/p/ABC-1"}
    found, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert found and offers[0].condition == "NE"
    assert offers[0].source_url == "https://vendor.example/p/ABC-1"


@pytest.mark.asyncio
async def test_a_link_not_on_the_page_falls_back_to_the_results_page():
    offer = {"requested_part_number": "ABC-1", "part_number": "ABC-1", "price": "12.00",
             "currency": "USD", "qty_available": "3", "condition": None, "lead_time": None,
             "source_url": "https://vendor.example/p/MADE-UP"}
    _, by_part, _ = await Extractor(_Client({"page_kind": "results", "offers": [offer]})) \
        .extract_offers_bulk(page_text=PAGE, page_url="https://vendor.example/results",
                             parts=[{"part_number": "ABC-1"}])
    assert by_part["ABC-1"][0].source_url == "https://vendor.example/results"


TEXT = ("Results read from the page's results grid: 3 record(s)\n\n"
        "Record 1:\n  Cust_part: F1101385-01\n  Price: 188.84\n  QtyAvailable: 12\n"
        "  other_fields: If out of stock, approximate lead time is 59 days\n"
        "  Expanded details (as shown on the page):\n    Part Condition\n    Factory New\n\n"
        "Record 2:\n  Cust_part: 4132A-934-ZB\n  Price: 86.95\n  QtyAvailable: 220\n\n"
        "Record 3:\n  Cust_part: 5934001-217\n  Price: 2,719.21\n  QtyAvailable: Out of Stock\n"
        "  other_fields: approximate lead time is 129 days. Next due Nov 20, 2026: 65")


def _check(offer, pn):
    from sourcing.extract import _verify, part_context

    return _verify(offer, part_context(TEXT, pn)), offer


def test_values_on_the_page_are_kept():
    from sourcing.models import Offer

    flags, o = _check(Offer("F1101385-01", "188.84", "USD", "12", "FN",
                            "In stock (59 days if out of stock)"), "F1101385-01")
    assert flags is None and (o.price, o.qty_available, o.condition) == ("188.84", "12", "FN")
    assert o.lead_time == "In stock (59 days if out of stock)"
    flags, o = _check(Offer("5934001-217", "2719.21", "USD", "Out of stock", None,
                            "129 days (next due Nov 20, 2026, qty 65)"), "5934001-217")
    assert flags is None and o.lead_time.startswith("129 days")


def test_values_not_on_the_parts_own_data_are_removed():
    from sourcing.models import Offer

    flags, o = _check(Offer("4132A-934-ZB", "86.95", "USD", "227"), "4132A-934-ZB")
    assert o.qty_available is None and flags == "QTY_NOT_ON_PAGE" and o.price == "86.95"
    # another part's price is not this part's price
    flags, o = _check(Offer("4132A-934-ZB", "188.84", "USD", "220"), "4132A-934-ZB")
    assert o.price is None and o.currency is None and flags == "PRICE_NOT_ON_PAGE"
    # an invented lead time, "In stock" for a part with no stock, an unseen condition
    flags, o = _check(Offer("4132A-934-ZB", "86.95", "USD", "220", None, "45 days"),
                      "4132A-934-ZB")
    assert o.lead_time is None and flags == "LEAD_TIME_NOT_ON_PAGE"
    flags, o = _check(Offer("5934001-217", "2719.21", "USD", "Out of stock", "OH",
                            "In stock"), "5934001-217")
    assert o.lead_time is None and o.condition is None
    assert flags == "LEAD_TIME_NOT_ON_PAGE,CONDITION_NOT_ON_PAGE"


@pytest.mark.asyncio
async def test_not_present_on_website_is_read_as_no_value():
    missing = "Not Present on website"
    offer = {"part_number": "ABC-1", "price": "12.00", "currency": missing,
             "qty_available": missing, "condition": missing, "lead_time": missing,
             "source_url": missing, "warehouses": missing, "listing": missing}
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    o = offers[0]
    # no stock shown: the price the model returned is dropped too (rule 2.2)
    assert o.price is None and o.review is None
    assert (o.currency, o.qty_available, o.condition, o.lead_time, o.warehouses, o.listing) \
        == (None,) * 6
    assert o.source_url == "https://vendor.example/results"


@pytest.mark.parametrize("qty, kept_price, kept_lead", [
    ("3", True, True), ("Out of stock", True, True), ("0", True, True), (None, False, False),
])
def test_price_and_condition_only_for_lines_with_stock_shown(qty, kept_price, kept_lead):
    from sourcing.extract import _apply_stock_rule
    from sourcing.models import Offer

    o = Offer("ABC-1", "12.00", "USD", qty, "NE", "45 days", warehouses="Main: 3")
    _apply_stock_rule(o)
    assert (o.price is not None, o.currency is not None, o.condition is not None) == (kept_price,) * 3
    assert (o.lead_time is not None, o.warehouses is not None) == (kept_lead,) * 2


def test_warehouses_one_block_per_warehouse_and_missing_values_left_out():
    missing = "Not Present on website"
    cell = format_warehouses([
        {"name": "Brea, CA", "qty_on_hand": "45",
         "incoming": [{"date": "May 20, 2027", "qty": "200"}]},
        {"name": "Singapore", "qty_on_hand": "5", "incoming": []},
        {"name": "UK", "qty_on_hand": missing,
         "incoming": [{"date": "Nov 26, 2026", "qty": missing}]},
    ])
    assert cell == ("Warehouse: Brea, CA\nQty on hand: 45\nNext day in: May 20, 2027\n"
                    "Qty due in: 200\n\n"
                    "Warehouse: Singapore\nQty on hand: 5\n\n"
                    "Warehouse: UK\nNext day in: Nov 26, 2026")
    assert format_warehouses([]) is None
    assert format_warehouses(None) is None


@pytest.mark.asyncio
async def test_warehouse_numbers_are_checked_against_the_page():
    offer = {"part_number": "ABC-1", "price": "12.00", "currency": "USD", "qty_available": "3",
             "condition": "New", "lead_time": "In stock", "source_url": None, "listing": "requested",
             "warehouses": [{"name": "Main", "qty_on_hand": "3", "incoming": []}]}
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert offers[0].warehouses == "Warehouse: Main\nQty on hand: 3"

    offer["warehouses"] = [{"name": "Main", "qty_on_hand": "999", "incoming": []}]
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert offers[0].warehouses is None and "WAREHOUSES_NOT_ON_PAGE" in offers[0].review


BREAKS_PAGE = ("ABC-1 23.70 Qty 3 New\nOrder Quantity Price\n1-249 $23.70\n250-499 $22.22\n"
               "500+ $20.92")


@pytest.mark.asyncio
async def test_price_breaks_are_read_and_checked_against_the_page():
    missing = "Not Present on website"
    offer = {"part_number": "ABC-1", "price": "23.70", "currency": "USD", "qty_available": "3",
             "condition": "New", "lead_time": "In stock", "source_url": None,
             "listing": "requested", "warehouses": [],
             "price_breaks": [{"min_qty": "1", "max_qty": "249", "price": "$23.70"},
                              {"min_qty": "250", "max_qty": "499", "price": "22.22"},
                              {"min_qty": "500", "max_qty": missing, "price": "20.92"},
                              {"min_qty": missing, "max_qty": missing, "price": "1.00"}]}
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=BREAKS_PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert offers[0].price_breaks == [("1", "249", "23.70"), ("250", "499", "22.22"),
                                      ("500", "", "20.92")]
    assert offers[0].review is None

    # a break price that isn't on the page: the whole table goes, the flag says so
    offer["price_breaks"][1]["price"] = "19.99"
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=BREAKS_PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert offers[0].price_breaks == [] and offers[0].review == "PRICE_BREAKS_NOT_ON_PAGE"
    assert offers[0].price == "23.70"

    # out of stock: prices kept, for comparison (rule 2.2, client 2026-10-23)
    offer["price_breaks"][1]["price"], offer["qty_available"] = "22.22", "Out of stock"
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=BREAKS_PAGE + " Out of stock", page_url="https://vendor.example/results",
        part_number="ABC-1")
    assert len(offers[0].price_breaks) == 3 and offers[0].price == "23.70"

    # no stock shown at all: no prices
    offer["qty_available"] = missing
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=BREAKS_PAGE, page_url="https://vendor.example/results", part_number="ABC-1")
    assert offers[0].price_breaks == [] and offers[0].price is None


def test_prompt_and_schema_ask_for_the_price_break_table():
    from sourcing.extract import EXTRACT_SYSTEM, OFFERS_SCHEMA

    item = OFFERS_SCHEMA["properties"]["offers"]["items"]
    assert "price_breaks" in item["required"] and "price_breaks" in item["properties"]
    assert "### 3.5. price_breaks" in EXTRACT_SYSTEM


@pytest.mark.asyncio
async def test_moq_is_kept_when_on_the_page_whatever_the_stock():
    page = "AN901-4C Out of stock\nMinimum order quantity for this part is 50 EA"
    offer = {"part_number": "AN901-4C", "price": None, "currency": None,
             "qty_available": "Out of stock", "moq": "50", "condition": None,
             "lead_time": None, "source_url": None, "listing": "requested", "warehouses": [],
             "price_breaks": []}
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=page, page_url="https://vendor.example/results", part_number="AN901-4C")
    assert offers[0].moq == "50" and offers[0].review is None

    offer["moq"] = "100"           # not on the page: removed and flagged
    _, _, offers, _ = await Extractor(_Client(
        {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
        page_text=page, page_url="https://vendor.example/results", part_number="AN901-4C")
    assert offers[0].moq is None and offers[0].review == "MOQ_NOT_ON_PAGE"


def test_prompt_and_schema_ask_for_the_moq():
    from sourcing.extract import EXTRACT_SYSTEM, OFFERS_SCHEMA

    assert "moq" in OFFERS_SCHEMA["properties"]["offers"]["items"]["required"]
    assert "### 3.3. moq" in EXTRACT_SYSTEM and "MIN ORDER $50" in EXTRACT_SYSTEM


# Proponent's certification note for a part (grid record, seen 2026-10-19).
CERT_TEXT = ("Record 1:\n  Cust_part: AF400\n  QtyAvailable: 5\n  CertNotes: ***** CERT NOTES FOR "
             "AF400 ***** Proponent CofC Manufacturer CofC Civil Authorized Release Certificate "
             "true copy e.g. FAA Form 8130, EASA Form One, etc. ATA-106 upon request\n\n"
             "Record 2:\n  Cust_part: 56270\n  QtyAvailable: 3\n  CertNotes: ***** CERT NOTES FOR "
             "56270 ***** Proponent CofC Manufacturer CofC No 8130 Available, Approved Mfr")


@pytest.mark.parametrize("pn, trace, tagged_by, review", [
    # Shortened with allowed abbreviations: every word is on the page.
    ("AF400", "Proponent CofC, Manufacturer CofC",
     "Civil Authorized Release Cert FAA Form 8130, EASA F1", None),
    ("56270", "Proponent CofC, Manufacturer CofC, Approved Mfr", None, None),
])
def test_trace_and_tagged_by_on_the_page_are_kept(pn, trace, tagged_by, review):
    from sourcing.extract import _verify, part_context
    from sourcing.models import Offer

    offer = Offer(part_number=pn, trace=trace, tagged_by=tagged_by)
    assert _verify(offer, part_context(CERT_TEXT, pn)) == review
    assert (offer.trace, offer.tagged_by) == (trace, tagged_by)


def test_trace_or_tagged_by_with_words_not_on_the_page_is_removed():
    from sourcing.extract import _verify, part_context
    from sourcing.models import Offer

    # "Dual Release" and "Boeing" are not in 56270's note: removed and flagged.
    offer = Offer(part_number="56270", trace="Boeing, MFR COC",
                  tagged_by="FAA 8130 DR")
    assert _verify(offer, part_context(CERT_TEXT, "56270")) == \
        "TRACE_NOT_ON_PAGE,TAGGED_BY_NOT_ON_PAGE"
    assert (offer.trace, offer.tagged_by) == (None, None)


def test_warehouse_cell_has_max_qty_available():
    cell = format_warehouses([{"name": "Brea, CA", "qty_on_hand": "198323",
                               "min_qty": "Not Present on website", "max_qty": "198323",
                               "incoming": []}])
    assert cell == "Warehouse: Brea, CA\nQty on hand: 198323\nMax qty available: 198323"


@pytest.mark.asyncio
async def test_description_is_read_and_checked_against_the_page():
    # Procart's new results page (2026-10-23) breaks long descriptions across lines.
    page = ("406Q80-1 $387.04 No RISING FLUI\nD FOR RAIN REPELLENT Out of Stock EA "
            "If out of stock, approximate lead time is 336 days")
    missing = "Not Present on website"
    offer = {"part_number": "406Q80-1", "price": "387.04", "currency": missing,
             "qty_available": "Out of stock", "condition": missing, "lead_time": "336 days",
             "source_url": None, "listing": "requested", "warehouses": [], "price_breaks": [],
             "moq": missing, "trace": missing, "tagged_by": missing,
             "description": "RISING FLUID FOR RAIN REPELLENT"}

    async def read(description):
        offer["description"] = description
        _, _, offers, _ = await Extractor(_Client(
            {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
            page_text=page, page_url="https://vendor.example/results", part_number="406Q80-1")
        return offers[0]

    assert (await read("RISING FLUID FOR RAIN REPELLENT")).description == \
        "RISING FLUID FOR RAIN REPELLENT"
    made_up = await read("WINDSHIELD RAIN REPELLENT KIT")      # "windshield", "kit": not on page
    assert made_up.description is None and "DESCRIPTION_NOT_ON_PAGE" in made_up.review
    assert (await read(missing)).description is None


@pytest.mark.asyncio
async def test_tag_date_is_read_and_checked_against_the_page():
    # DASI's Tag Details hover text (2026-10-30, NAS1149C0432R).
    page = ("NAS1149C0432R lot 3: Cond: NS | Qty Avail: 2 | Price: $ 10.00 | "
            "Trace: CHINA SOUTHERN AIRLINES | Tag Details: 15 OCT 1996")
    missing = "Not Present on website"
    offer = {"part_number": "NAS1149C0432R", "price": "10.00", "currency": missing,
             "qty_available": "2", "condition": "NS", "lead_time": missing,
             "source_url": None, "listing": "requested", "warehouses": [], "price_breaks": [],
             "moq": missing, "trace": missing, "tagged_by": missing, "tag_date": "15 OCT 1996",
             "description": missing}

    async def read(tag_date):
        offer["tag_date"] = tag_date
        _, _, offers, _ = await Extractor(_Client(
            {"found": True, "page_kind": "results", "offers": [offer]})).extract_offers(
            page_text=page, page_url="https://store.dasi.com/x", part_number="NAS1149C0432R")
        return offers[0]

    assert (await read("15 OCT 1996")).tag_date == "15 OCT 1996"
    made_up = await read("12 MAR 2001")
    assert made_up.tag_date is None and "TAG_DATE_NOT_ON_PAGE" in made_up.review
    assert (await read(missing)).tag_date is None


def test_prompt_and_schema_ask_for_the_description():
    from sourcing.extract import EXTRACT_SYSTEM, OFFERS_SCHEMA

    item = OFFERS_SCHEMA["properties"]["offers"]["items"]
    assert "description" in item["required"] and "### 3.13. description" in EXTRACT_SYSTEM
    assert "tag_date" in item["required"] and "`tag_date`" in EXTRACT_SYSTEM
