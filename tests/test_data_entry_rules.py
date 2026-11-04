"""The client's data entry rules (docs/DATA_ENTRY_RULES.md), one section per
rule, checked end to end: offers as the portal gives them -> pipeline -> the
RFQ tab's values. A failing test names the rule a change broke; fix the code,
not the test, unless the client changed the rule (then change the doc too).

Rules the code doesn't meet yet are marked GAP (xfail, strict): they pass as
"expected to fail", and fail once the code is fixed, so the marker is removed
and the rule is kept from then on."""
from __future__ import annotations

import re
from datetime import date

import pytest

from sourcing.models import Offer

from .test_pipeline import AS3491_BREAKS, _offer_rows, run

GAP = pytest.mark.xfail(strict=True, reason="rule not met yet: docs/DATA_ENTRY_RULES.md")

# A date far ahead, so "days until it" never falls back to the lead time.
FAR = "Dec 6, 2099"


async def _lines(offers, qty="1"):
    """(ResultRow, RFQ record) for every line written for request P1."""
    deps, _ = _offer_rows(offers, qty=qty)
    await run(deps)
    return [(r, r.as_rfq_record()) for r in deps.sink.rows]


async def _line(offer, qty="1"):
    [(row, rfq)] = await _lines([offer], qty)
    return row, rfq


def _days_to(when: date, row) -> str:
    return str((when - date.fromisoformat(row.fetched_at[:10])).days)


IN_STOCK = Offer(part_number="P1", price="15.64", currency="USD", qty_available="7915",
                 condition="NE", price_breaks=AS3491_BREAKS,
                 warehouses="Warehouse: Brea, CA\nQty on hand: 7915")
SHORT = Offer(part_number="P1", price="15.64", currency="USD", qty_available="8",
              condition="NE", price_breaks=AS3491_BREAKS)
INCOMING = Offer(part_number="P1", price="105.74", currency="USD", qty_available="Out of stock",
                 condition="FN", lead_time="264 days",
                 warehouses=f"Warehouse: Brea, CA\nQty on hand: 0\nNext day in: {FAR}\n"
                            "Qty due in: 75\nNext day in: Jul 26, 2100\nQty due in: 200")
NOTHING_DUE = Offer(part_number="P1", price="387.04", currency="USD", qty_available="Out of stock",
                    condition="FN", lead_time="336 days",
                    warehouses="Warehouse: Netherlands\nQty on hand: 0")


# 1. Quantity: Quantity Requested = Qty Quoted; the full stock.

@pytest.mark.asyncio
@pytest.mark.parametrize("offer, qty, expected", [
    (IN_STOCK, "50", "7915"),        # whole stock, not the 50 asked
    (SHORT, "50", "8"),              # less than asked: what the site has
    (INCOMING, "1", "75"),           # out of stock: what comes in first
    (NOTHING_DUE, "3", "3"),         # nothing coming: the customer's quantity
])
async def test_rule_1_quantity_requested_equals_quantity_quoted(offer, qty, expected):
    _, rfq = await _line(offer, qty)
    assert rfq["Quantity Requested"] == rfq["Qty Quoted"] == expected


@GAP
@pytest.mark.asyncio
async def test_rule_1_quantity_requested_equals_quantity_quoted_on_no_quote_lines():
    # In stock without a price: No Quote, Qty Quoted 0, Quantity Requested the 5 asked.
    _, rfq = await _line(Offer(part_number="P1", qty_available="12", condition="NE"), "5")
    assert rfq["Type Quoted"] == "No Quote"
    assert rfq["Quantity Requested"] == rfq["Qty Quoted"]


# 2. Incoming stock: same Date Next In added up; different dates, the soonest only.

@pytest.mark.asyncio
async def test_rule_2_incoming_on_the_same_date_is_combined():
    # 418-09104 (client, 2026-10-26): 1 + 3 + 3 on one date -> 7, across warehouses too.
    row, rfq = await _line(Offer(
        part_number="P1", price="50.00", qty_available="Out of stock", condition="FN",
        lead_time="40 days",
        warehouses=f"Warehouse: Brea, CA\nQty on hand: 0\nNext day in: {FAR}\nQty due in: 1\n"
                   f"Next day in: {FAR}\nQty due in: 3\n\n"
                   f"Warehouse: UK\nQty on hand: 0\nNext day in: {FAR}\nQty due in: 3"))
    assert rfq["Qty Quoted"] == rfq["Quantity Requested"] == "7"


@pytest.mark.asyncio
async def test_rule_2_incoming_on_different_dates_uses_the_soonest_only():
    # 4140073-1 (client, 2026-10-27): 75 on the first date, 200 later -> 75.
    _, rfq = await _line(INCOMING)
    assert rfq["Qty Quoted"] == "75"


# 3. MOQ and pricing: unit price from the quantity asked, the stock and the MOQ;
#    the quantity asked, the MOQ and every price break in Internal Comments.

@pytest.mark.asyncio
async def test_rule_3_unit_price_follows_quantity_asked_stock_and_moq():
    _, rfq = await _line(IN_STOCK, "50")
    assert rfq["Unit Price"] == "2.15"                  # 50 asked: the 22-172 break
    _, rfq = await _line(SHORT, "50")
    assert rfq["Unit Price"] == "6.26"                  # only 8 in stock: the 5-10 break
    row, rfq = await _line(Offer(part_number="P1", price="3.00", qty_available="100",
                                 condition="NE", moq="50"), "10")
    assert rfq["Unit Price"] == "3.00"
    assert "MOQ 50, PRICED AT 50 (ASKED 10)" in row.internal_comments


@pytest.mark.asyncio
async def test_rule_3_internal_comments_hold_the_moq_and_every_price_break():
    row, _ = await _line(IN_STOCK, "50")
    assert "MOQ 22 FOR THIS PRICE BREAK" in row.internal_comments
    assert row.internal_comments.endswith(
        "• PRICE BREAKS:\n"
        "    ◦ 1-4 $15.64\n    ◦ 5-10 $6.26\n    ◦ 11-21 $3.45\n    ◦ 22-172 $2.15\n"
        "    ◦ 173+ $1.06")


@GAP
@pytest.mark.asyncio
async def test_rule_3_internal_comments_hold_the_quantity_asked():
    # Only said today when less than the MOQ is asked ("(ASKED 10)").
    row, _ = await _line(IN_STOCK, "50")
    assert re.search(r"\b50\b", row.internal_comments.split("• PRICE BREAKS")[0])


# 4. Delivery: in stock 0; incoming, the days to the soonest date; nothing
#    coming, the lead time, with "X DAY LEAD TIME, FOB: [WAREHOUSE]" in comments.

@pytest.mark.asyncio
async def test_rule_4_in_stock_delivery_is_0():
    _, rfq = await _line(IN_STOCK, "50")
    assert rfq["Delivery"] == "0"


@pytest.mark.asyncio
async def test_rule_4_incoming_delivery_is_the_soonest_date():
    row, rfq = await _line(INCOMING)
    assert rfq["Delivery"] == _days_to(date(2099, 12, 6), row)


@pytest.mark.asyncio
async def test_rule_4_nothing_incoming_uses_the_lead_time_and_says_fob():
    # 406Q80-1 (client, 2026-10-27).
    row, rfq = await _line(NOTHING_DUE)
    assert rfq["Delivery"] == "336"
    assert row.internal_comments == "• 336 DAY LEAD TIME, FOB: NETHERLANDS"


# 5. No Quote: a price and a lead time -> quoted, out of stock too; No Quote only
#    when the supplier won't sell it; No Quote lines have no Delivery.

@pytest.mark.asyncio
@pytest.mark.parametrize("offer", [INCOMING, NOTHING_DUE])
async def test_rule_5_out_of_stock_with_price_and_lead_time_is_quoted(offer):
    _, rfq = await _line(offer)
    assert rfq["Type Quoted"] == "Outright"


@pytest.mark.asyncio
async def test_rule_5_no_quote_lines_have_no_delivery():
    _, rfq = await _line(Offer(part_number="P1", qty_available="Out of stock",
                               lead_time="30 days", condition="NE"))       # no price
    assert (rfq["Type Quoted"], rfq["Delivery"], rfq["Unit Price"]) == ("No Quote", "", "0")


@GAP
@pytest.mark.asyncio
async def test_rule_5_in_stock_with_a_price_is_quoted_without_a_condition_shown():
    # Today an in-stock line whose page shows no condition is "No Quote".
    _, rfq = await _line(Offer(part_number="P1", price="5.00", qty_available="3"))
    assert rfq["Type Quoted"] == "Outright"


# 6. Approved substitutes: their own line, never in the requested part's PN
#    Quoted; out of stock, their lead time in the requested part's comments.

SUB_OUT = Offer(part_number="S9", price="3,650.89", qty_available="Out of stock",
                listing="substitute", lead_time="154 days",
                warehouses="Warehouse: Brea, CA\nQty on hand: 0")
SUB_IN = Offer(part_number="S8", price="9.00", qty_available="4", condition="FN",
               listing="substitute")


@pytest.mark.asyncio
async def test_rule_6_substitutes_get_their_own_line():
    lines = await _lines([Offer(part_number="P1", price="3.00", qty_available="10",
                                condition="FN", listing="requested"), SUB_IN, SUB_OUT])
    assert [(rfq["Part Number"], rfq["PN Quoted"]) for _, rfq in lines] == [
        ("P1", "P1"), ("S8", "S8"), ("S9", "S9")]


@pytest.mark.asyncio
async def test_rule_6_substitute_only_listed_never_goes_in_the_requested_pn_quoted():
    lines = await _lines([SUB_IN])
    assert [(rfq["Part Number"], rfq["PN Quoted"]) for _, rfq in lines] == [
        ("P1", "P1"), ("S8", "S8")]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [
    Offer(part_number="P1", price="3.00", qty_available="10", condition="FN",
          listing="requested"),
    Offer(part_number="P1", price="3.00", qty_available="Out of stock", condition="FN",
          lead_time="60 days", listing="requested"),
], ids=["requested-in-stock", "requested-out-of-stock"])
async def test_rule_6_out_of_stock_substitute_lead_time_in_requested_comments(requested):
    lines = await _lines([requested, SUB_IN, SUB_OUT])
    comments = lines[0][0].internal_comments
    assert "• APPROVED SUBSTITUTE ALSO OFFERED: PN S8" in comments
    assert "• APPROVED SUBSTITUTE ALSO OFFERED WITH 154 DAY LEAD TIME: PN S9" in comments


# 7. AvSight fields: always Serialized N, Type Requested Outright, Condition
#    Code = Condition Quoted, whatever the request says. UOM: the request's
#    unit, else EA (changed 2026-10-30 for LB requests; PC counts as EA).

@pytest.mark.asyncio
async def test_rule_7_avsight_fields_are_fixed_on_every_line():
    deps, sheet = _offer_rows([
        IN_STOCK, SUB_OUT,
        Offer(part_number="P1", qty_available="Out of stock", condition="NE"),   # No Quote
    ], qty="50")
    req = sheet.rows[0]
    req.serialized, req.uom, req.type_requested = "Y", "PC", "Exchange"
    await run(deps)
    rfqs = [r.as_rfq_record() for r in deps.sink.rows]
    assert len(rfqs) == 3
    for rfq in rfqs:
        assert (rfq["Serialized"], rfq["UOM"], rfq["Type Requested"]) == ("N", "EA", "Outright")
        assert rfq["Condition Code"] == rfq["Condition Quoted"]


@pytest.mark.asyncio
# A line without a unit counts as EA (most portals show none for pieces).
@pytest.mark.parametrize("asked, kept", [("LB", ["LB"]), ("EA", ["EA", None]), ("", ["EA", "LB", None])])
async def test_rule_7_uom_lines_match_the_request_and_say_their_unit(asked, kept):
    # MS20470AD3-4 on DASI (2026-10-30): EA and LB lots of one part.
    lines = [Offer(part_number="P1", qty_available="4038", condition="NS", price="0.02", uom="EA"),
             Offer(part_number="P1", qty_available="200", condition="NS", price="0.10", uom="LB"),
             Offer(part_number="P1", qty_available="5", condition="NS", price="1.00")]
    deps, sheet = _offer_rows(lines, qty="1")
    sheet.rows[0].uom = asked
    await run(deps)
    rfqs = [r.as_rfq_record() for r in deps.sink.rows]
    assert sorted(r["Qty Quoted"] for r in rfqs) == sorted(
        o.qty_available for o in lines if o.uom in kept)
    for rfq, row in zip(rfqs, deps.sink.rows):
        assert rfq["UOM"] == (asked or row.uom or "EA")


@pytest.mark.asyncio
async def test_rule_7_no_line_in_the_unit_asked_is_not_found():
    deps, sheet = _offer_rows([Offer(part_number="P1", qty_available="9", condition="NS",
                                     price="2.23", uom="EA")], qty="1")
    sheet.rows[0].uom = "LB"
    await run(deps)
    assert deps.sink.rows == [] and sheet.status_of("P1") == "NOT_FOUND"


# -- DASI rules (client, 2026-10-29) ----------------------------------------
# Applied while DASI's grid is read (vendors/dasi.py), before the LLM sees it.

def _dasi_lot(cond="NS", where="DASI Miami HQ (FL)", trace="FAA 129", qty="138", moq="134",
              price="$ 0.15", lead="0", uom="EA", notes=()):
    cells = [("", ""), ("Action", "View"), ("Cond", cond), ("Ship From", where),
             ("Lead (Days)", lead), ("Trace/Tag", trace), ("UOM", uom), ("Qty Avail", qty),
             ("MOQ", moq), ("Price", price), ("Cart Qty", ""), ("Select Item", "Add to Cart")]
    return {"cells": cells, "notes": list(notes)}


def _dasi_text(*lots):
    from sourcing.vendors.dasi import DasiAdapter
    return DasiAdapter()._records_text(
        [{"header": "MS24665-132 (Quantity: 976) | PIN-COTTER", "lots": list(lots)}])


def test_rule_dasi_1_same_price_location_and_moq_is_one_line_with_every_trace():
    text = _dasi_text(_dasi_lot(trace="FAA 121", qty="138", notes=["Tag Details: OEM C OF C ONLY"]),
                      _dasi_lot(trace="FAA 129", qty="135"),
                      _dasi_lot(trace="FAA 129", qty="30", moq="30", price="$ 0.67"))
    assert ("MS24665-132 lot 1 (2 lots combined): Cond: NS | Ship From: DASI Miami HQ (FL) | "
            "Lead (Days): 0 | Trace/Tag: FAA 121, FAA 129 | UOM: EA | Qty Avail: 273 (138 + 135) | "
            "MOQ: 134 | Price: $ 0.15 | Tag Details: OEM C OF C ONLY (1 of 2 lots)") in text
    assert "MS24665-132 lot 2: " in text and "lot 3" not in text      # other price: own line


def test_rule_dasi_1_combined_trace_keeps_each_lots_own_source():
    # Run 12, MS24665-132: the source in brackets is the lot's own hover text
    # (client, 2026-10-30), not every source pooled after all the traces.
    text = _dasi_text(
        _dasi_lot(trace="FAA 129", qty="138", notes=["Trace: CHINA SOUTHERN AIRLINES",
                                                     "Tag Details: OEM C OF C ONLY"]),
        _dasi_lot(trace="FAA 121", qty="135", notes=["Trace: REPUBLIC AIRWAYS"]),
        _dasi_lot(trace="FAA 129", qty="10", notes=["Trace: CHINA EASTERN AIRLINES"]))
    assert ("Trace/Tag: FAA 129 (CHINA SOUTHERN AIRLINES, CHINA EASTERN AIRLINES), "
            "FAA 121 (REPUBLIC AIRWAYS) | UOM: EA | Qty Avail: 283 (138 + 135 + 10)") in text
    assert "| Trace: " not in text
    assert text.rstrip().endswith("Tag Details: OEM C OF C ONLY (1 of 3 lots)")


def test_rule_dasi_1_trace_is_whole_with_every_source_of_the_same_trace():
    # MS20470AD3-4 (2026-10-30): two FAA 129 lots with different hover text; the
    # written Trace was "DISTRIBUTOR (Aero Inventory), FAA 129", sources dropped
    # to fit 50 characters. The program keeps both, and DASI's prompt says never
    # to shorten.
    from sourcing.vendors.dasi_prompt import DASI_EXTRACT_SYSTEM

    text = _dasi_text(
        _dasi_lot(trace="DISTRIBUTOR", qty="1988", moq="1000", notes=["Trace: Aero Inventory"]),
        _dasi_lot(trace="FAA 129", qty="1000", moq="1000", notes=["Trace: CHINA EASTERN AIRLINES"]),
        _dasi_lot(trace="FAA 129", qty="1050", moq="1000",
                  notes=["Trace: CHINA SOUTHERN AIRLINES"]))
    assert ("Trace/Tag: DISTRIBUTOR (Aero Inventory), FAA 129 (CHINA EASTERN AIRLINES, "
            "CHINA SOUTHERN AIRLINES) |") in text
    assert "no 50-character limit" in DASI_EXTRACT_SYSTEM and "never shorten" in DASI_EXTRACT_SYSTEM


def test_rule_dasi_1_lots_with_another_condition_are_not_combined():
    text = _dasi_text(_dasi_lot(cond="NS"), _dasi_lot(cond="NE"))
    assert "combined" not in text and "lot 2: Cond: NE" in text


def test_rule_dasi_1_lots_with_another_uom_are_not_combined():
    # 138 EA + 5 LB is no quantity at all (2026-10-30).
    text = _dasi_text(_dasi_lot(qty="138"), _dasi_lot(qty="5", uom="LB"), _dasi_lot(qty="135"))
    assert "lot 1 (2 lots combined)" in text and "UOM: EA | Qty Avail: 273 (138 + 135)" in text
    assert "lot 2: " in text and "UOM: LB | Qty Avail: 5 |" in text


def test_rule_dasi_2_only_dasi_miami_hq_lines():
    text = _dasi_text(_dasi_lot(where="Plant City (FL)", qty="30"), _dasi_lot(qty="138"))
    assert "Plant City" not in text and "MS24665-132 lot 1: Cond: NS" in text
    assert "lot 2" not in text
    only_elsewhere = _dasi_text(_dasi_lot(where="Plant City (FL)"))
    assert "MS24665-132: no lots shipped from DASI Miami HQ (FL)" in only_elsewhere


def test_dasi_hover_text_with_and_without_tag_details():
    text = _dasi_text(
        _dasi_lot(qty="138", notes=["Trace: CHINA SOUTHERN AIRLINES", "Tag Details: OEM C OF C ONLY"]),
        _dasi_lot(qty="54", moq="53", price="$ 0.38", notes=["Trace: AIR ONE"]),
        _dasi_lot(qty="30", moq="30", price="$ 0.67", notes=["WESTERN WIRE"]))
    assert ("lot 1: Cond: NS | Ship From: DASI Miami HQ (FL) | Lead (Days): 0 | Trace/Tag: FAA 129 | "
            "UOM: EA | Qty Avail: 138 | MOQ: 134 | Price: $ 0.15 | Trace: CHINA SOUTHERN AIRLINES | "
            "Tag Details: OEM C OF C ONLY") in text
    assert "Price: $ 0.38 | Trace: AIR ONE" in text and "lot 2" in text and "Tag Details" not in \
        text.split("lot 2")[1].split("\n")[0]
    assert "Price: $ 0.67 | Trace/Tag note: WESTERN WIRE" in text
