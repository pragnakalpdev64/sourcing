"""ExcelSheet: local .xlsx with the same behaviour as the Google Sheets client."""
from __future__ import annotations

import openpyxl
import pytest

from sourcing.models import RESULTS_HEADER, RowStatus
from sourcing.sheets import ExcelSheet, open_sheet

HEADER = ["RFQ ID", "Vendor", "Part Number", "Qty Needed", "Condition Wanted",
          "Requested By", "Status", "Run ID", "Notes"]


def _book(path, rows, results_tab=True):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Requests"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    if results_tab:
        wb.create_sheet("Results")
    wb.save(path)
    return str(path)


def test_reads_new_and_empty_rows_and_formats_numbers(tmp_path):
    path = _book(tmp_path / "t.xlsx", [
        ["R1", "proponent", "BACC30M8", 1.0, None, "a@co.com", None, None, None],
        ["R1", "proponent", "NSA936830-22", 2, None, "a@co.com", "NEW", None, None],
        ["R1", "proponent", "SKIP-ME", 1, None, "a@co.com", "HOLD", None, None],
        ["R1", "proponent", None, 1, None, "a@co.com", None, None, None],
    ])
    rows = ExcelSheet(path, "Requests", "Results").read_new_rows()
    assert [(r.part_number, r.qty_needed, r.sheet_row) for r in rows] == [
        ("BACC30M8", "1", 2), ("NSA936830-22", "2", 3)]


def test_status_and_results_are_saved_to_the_file(tmp_path):
    path = _book(tmp_path / "t.xlsx", [
        ["R1", "proponent", "BACC30M8", 1, None, "a@co.com", None, None, None],
    ], results_tab=False)
    sheet = ExcelSheet(path, "Requests", "Results")
    rows = sheet.read_new_rows()
    sheet.set_status(rows, RowStatus.DONE, run_id="run-1", note="ok")
    sheet.append_results([{"RFQ ID": "R1", "Vendor": "proponent", "Requested Part": "BACC30M8",
                           "Part Number": "BACC30M8", "Unit Price": "1.23"}])

    wb = openpyxl.load_workbook(path)
    req = wb["Requests"]
    assert [req.cell(row=2, column=c).value for c in (7, 8, 9)] == ["DONE", "run-1", "ok"]
    res = wb["Results"]  # created on first write, with the header
    assert [c.value for c in res[1]] == RESULTS_HEADER
    row = dict(zip(RESULTS_HEADER, [c.value for c in res[2]]))
    assert (row["RFQ ID"], row["Part Number"], row["Unit Price"]) == ("R1", "BACC30M8", "1.23")
    assert ExcelSheet(path, "Requests", "Results").read_new_rows() == []


def test_open_sheet_picks_excel_for_xlsx_paths(tmp_path):
    path = _book(tmp_path / "t.xlsx", [])
    assert isinstance(open_sheet(path, "Requests", "Results"), ExcelSheet)


def test_rfq_tab_is_created_with_the_clients_columns(tmp_path):
    from sourcing.models import RFQ_HEADER, ResultRow

    path = _book(tmp_path / "t.xlsx", [])
    sheet = ExcelSheet(path, "Requests", "Results")
    row = ResultRow(rfq_id="R1", vendor="Proponent", requested_part="AS1895/7-300",
                    part_number="2873-300", price="69.03", qty_available="420",
                    condition="FN", qty_needed="300", fetched_at="2026-10-19 08:25:01Z",
                    condition_wanted="NE", part_description="SEAL", uom="EA",
                    type_requested="Outright", trace="Proponent CofC, Manufacturer CofC",
                    internal_comments="Approved substitute part for part #AS1895/7-300")
    sheet.append_rfq([row.as_rfq_record()])

    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Requests", "Results", "RFQ"]
    rfq = wb["RFQ"]
    assert [c.value for c in rfq[1]] == RFQ_HEADER and len(RFQ_HEADER) == 24
    got = dict(zip(RFQ_HEADER, [c.value for c in rfq[2]]))
    assert got["Part Number"] == "AS1895/7-300" and got["PN Quoted"] == "2873-300"
    # In stock: Delivery 0 days; Qty Quoted is the stock (420; client, 2026-10-23).
    assert (got["Quoted Date"], got["Unit Price"], got["Delivery"]) == ("2026-10-19", "69.03",
                                                                        "0")
    assert (got["Quantity Requested"], got["Qty Quoted"], got["Condition Quoted"]) == (
        "420", "420", "FN")
    assert (got["Type Quoted"], got["Type Requested"]) == ("Outright", "Outright")
    assert (got["Part Description"], got["UOM"]) == ("SEAL", "EA")
    # Condition Code is the condition quoted (FN), not the one asked (NE) (client, 2026-10-26).
    assert (got["Condition Code"], got["Serialized"]) == ("FN", "N")
    assert (got["Trace"], got["Tagged By"]) == ("Proponent CofC, Manufacturer CofC", None)
    assert got["External Comments"] is None and got["Validity"] is None


def test_rfq_line_without_price_is_no_quote():
    from sourcing.models import ResultRow

    # Out of stock: no price or condition -> the client's No Quote values; no
    # Delivery even with a lead time on the page (client, 2026-10-26).
    got = ResultRow(rfq_id="", vendor="Proponent", part_number="4066-0014", qty_available="0",
                    lead_time="132", qty_needed="2").as_rfq_record()
    assert (got["Type Quoted"], got["Condition Quoted"], got["Qty Quoted"],
            got["Unit Price"], got["Delivery"]) == ("No Quote", "-", "0", "0", "")
    assert got["Condition Code"] == "-"

    # Tag Date: the vendor's, when the page gives one (DASI, 2026-10-30).
    got = ResultRow(rfq_id="", vendor="DASI", part_number="NAS1149C0432R", qty_available="2",
                    condition="NS", price="10.00", tag_date="15 OCT 1996").as_rfq_record()
    assert got["Tag Date"] == "1996-10-15"          # the client's YYYY-MM-DD
    from sourcing.models import rfq_tag_date
    assert rfq_tag_date("15 OCT 1996 (1 of 2 lots)") == "1996-10-15 (1 of 2 lots)"
    assert rfq_tag_date("SOMETIME 1996") == "SOMETIME 1996" and rfq_tag_date("") == ""

    # DASI SV lot with "Send RFQ" instead of a price: No Quote, but its condition
    # is shown (2026-10-30, MS20470AD3-4).
    got = ResultRow(rfq_id="", vendor="DASI", part_number="MS20470AD3-4", qty_available="3233",
                    condition="SV", lead_time="In stock (0 days)", qty_needed="1").as_rfq_record()
    assert (got["Type Quoted"], got["Condition Quoted"], got["Condition Code"],
            got["Unit Price"], got["Delivery"]) == ("No Quote", "SV", "SV", "0", "")


def test_out_of_stock_line_quotes_the_qty_due_in():
    from sourcing.models import ResultRow

    # 418-09104 (client, 2026-10-26): out of stock, 460 days, 7 due in -> 7 / 7.
    def rfq(warehouses):
        return ResultRow(rfq_id="", vendor="Proponent", part_number="418-09104",
                         price="22,579.01", qty_available="Out of Stock", condition="FN",
                         lead_time="460", qty_needed="1", warehouses=warehouses).as_rfq_record()

    got = rfq("Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Jan 30, 2028\nQty due in: 7")
    assert (got["Quantity Requested"], got["Qty Quoted"], got["Type Quoted"], got["Delivery"]) == (
        "7", "7", "Outright", "460")
    # Different dates: only the soonest, every warehouse due that day added up
    # (client, 2026-10-27); a quantity without a date doesn't count then.
    two = ("Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Mar 1, 2028\nQty due in: 1,200\n"
           "Next day in: Jan 30, 2028\nQty due in: 7\n\nWarehouse: UK\nQty on hand: 0\n"
           "Next day in: JAN 30, 2028\nQty due in: 2\n\nWarehouse: SG\nQty due in: 3")
    assert rfq(two)["Qty Quoted"] == "9"
    # No date at all: everything added up, as before (Flag NO_DUE_DATE in the pipeline).
    assert rfq("Warehouse: Brea, CA\nQty on hand: 0\nQty due in: 7\nQty due in: 3")[
        "Qty Quoted"] == "10"
    # Nothing due in: the customer's quantity in both, Delivery the lead time
    # (2026-10-27, 406Q80-1).
    got = rfq("Warehouse: Brea, CA\nQty on hand: 0")
    assert (got["Quantity Requested"], got["Qty Quoted"], got["Delivery"]) == ("1", "1", "460")


def test_out_of_stock_delivery_counts_days_to_the_soonest_due_date():
    from sourcing.models import ResultRow

    # 4140073-1 (client, 2026-10-27): quoted Oct 26, 75 due Dec 7, 200 Jul 26 -> 75 / 75, 42.
    def rfq(fetched_at):
        return ResultRow(rfq_id="", vendor="Proponent", part_number="4140073-1", price="105.74",
                         qty_available="Out of stock", condition="FN", lead_time="264",
                         delivery="264", qty_needed="1", fetched_at=fetched_at,
                         warehouses="Warehouse: Brea, CA\nQty on hand: 0\nNext day in: Dec 7, 2026\n"
                                    "Qty due in: 75\nNext day in: Jul 26, 2027\nQty due in: 200"
                         ).as_rfq_record()

    got = rfq("2026-10-26 12:34:31Z")
    assert (got["Quantity Requested"], got["Qty Quoted"], got["Delivery"]) == ("75", "75", "42")
    # Soonest date already past, or no quote date: the lead time.
    assert rfq("2026-12-08 09:00:00Z")["Delivery"] == "264"
    assert rfq("")["Delivery"] == "264"


def test_out_of_stock_without_lead_time_or_due_in_is_no_quote():
    from sourcing.models import ResultRow

    # A price but no lead time and nothing due in: can't be quoted (client,
    # 2026-10-27: "so long as there is price and a lead time").
    got = ResultRow(rfq_id="", vendor="V", part_number="P", price="5.00", condition="FN",
                    qty_available="Out of stock", lead_time="Not shown by V", qty_needed="3",
                    warehouses="Warehouse: Brea, CA\nQty on hand: 0").as_rfq_record()
    assert (got["Type Quoted"], got["Qty Quoted"], got["Quantity Requested"], got["Delivery"]) == (
        "No Quote", "0", "3", "")


def test_no_quote_line_out_of_stock_shows_the_qty_due_in():
    from sourcing.models import ResultRow

    # No price: No Quote, and still 7 / 7 (client, 2026-10-26). Price, condition
    # and Delivery stay No Quote's.
    got = ResultRow(rfq_id="", vendor="Proponent", part_number="418-09104",
                    qty_available="Out of Stock", lead_time="460", qty_needed="1",
                    warehouses="Warehouse: Brea, CA\nQty on hand: 0\nQty due in: 7").as_rfq_record()
    assert (got["Type Quoted"], got["Condition Quoted"], got["Unit Price"], got["Delivery"]) == (
        "No Quote", "-", "0", "")
    assert (got["Quantity Requested"], got["Qty Quoted"]) == ("7", "7")
    # A price and a lead time but no condition code: quoted, condition left empty
    # (client, 2026-10-27, 426EN9-8).
    got = ResultRow(rfq_id="", vendor="Proponent", part_number="418-09104", price="22,579.01",
                    qty_available="Out of Stock", lead_time="460", qty_needed="1",
                    warehouses="Warehouse: Brea, CA\nQty on hand: 0\nQty due in: 7").as_rfq_record()
    assert (got["Type Quoted"], got["Condition Quoted"], got["Unit Price"], got["Delivery"],
            got["Qty Quoted"]) == ("Outright", "", "22579.01", "460", "7")
    # In stock without a condition: No Quote with 0, as before.
    got = ResultRow(rfq_id="", vendor="Proponent", part_number="P1", price="5.00",
                    qty_available="12", qty_needed="1",
                    warehouses="Warehouse: Brea, CA\nQty on hand: 12\nQty due in: 7").as_rfq_record()
    assert (got["Quantity Requested"], got["Qty Quoted"]) == ("1", "0")


def test_rfq_fixed_values_whatever_the_request_says():
    from sourcing.models import ResultRow

    # AvSight import (client, 2026-10-26): Serialized N, Type Requested Outright
    # and Condition Code = Condition Quoted, always; UOM the request's (2026-10-30).
    got = ResultRow(rfq_id="", vendor="Proponent", part_number="P1", price="5.00",
                    qty_available="3", condition="OH", condition_wanted="NE", serialized="Y",
                    uom="FT", type_requested="Exchange").as_rfq_record()
    assert (got["Serialized"], got["UOM"], got["Type Requested"], got["Condition Code"]) == (
        "N", "FT", "Outright", "OH")


@pytest.mark.parametrize("stock, need, qty", [("12", "50", "12"), ("147,282", "50", "147282"),
                                               ("5", "", "5"), ("Not shown by V", "50", "50")])
def test_rfq_qty_quoted_is_the_stock(stock, need, qty):
    from sourcing.models import ResultRow

    got = ResultRow(rfq_id="", vendor="V", part_number="P", price="1,250.00", condition="NE",
                    qty_available=stock, qty_needed=need).as_rfq_record()
    assert (got["Qty Quoted"], got["Unit Price"], got["Type Quoted"]) == (qty, "1250.00",
                                                                         "Outright")


def test_rfq_qty_quoted_is_the_moq_when_less_is_needed_and_no_stock_is_shown():
    # Rows 28 and 30 of the test sheet (2026-10-21): 1 asked, MOQ 50 EA -> quote 50.
    # With stock shown, the stock is quoted (client, 2026-10-23).
    from sourcing.models import ResultRow

    got = ResultRow(rfq_id="", vendor="V", part_number="ASNA2397-10", price="0.21", condition="FN",
                    qty_available="", qty_needed="1", moq="50").as_rfq_record()
    assert got["Qty Quoted"] == "50"
    got = ResultRow(rfq_id="", vendor="V", part_number="AN901-4C", price="0.56", condition="FN",
                    qty_available="", qty_needed="500", moq="50").as_rfq_record()
    assert got["Qty Quoted"] == "500"
    # No MOQ stated, price breaks from 50: 50 quoted. Stock under it: the stock.
    got = ResultRow(rfq_id="", vendor="V", part_number="P", price="0.27", condition="FN",
                    qty_available="", qty_needed="1", min_order_qty="50").as_rfq_record()
    assert got["Qty Quoted"] == "50"
    got = ResultRow(rfq_id="", vendor="V", part_number="P", price="0.27", condition="FN",
                    qty_available="30", qty_needed="1", moq="50", min_order_qty="50").as_rfq_record()
    assert got["Qty Quoted"] == "30"


def test_rfq_delivery_for_an_in_stock_line_is_its_lead_time():
    from sourcing.models import rfq_delivery, ResultRow

    row = ResultRow(rfq_id="", vendor="V", part_number="P", qty_available="12", delivery="59")
    assert rfq_delivery(row) == "59"
    row.delivery = ""                                        # vendor shows no lead time
    assert rfq_delivery(row) == "0"


@pytest.mark.parametrize("lead, days", [("129", "129"), ("2 weeks", "14"), ("3-5 weeks", "35"),
                                        ("4-5 days", "5"), ("2-4 months", "120"),
                                        ("Not shown by Proponent", "")])
def test_rfq_delivery_is_days(lead, days):
    from sourcing.models import rfq_delivery, ResultRow

    assert rfq_delivery(ResultRow(rfq_id="", vendor="V", part_number="P", qty_available="0",
                                  lead_time=lead)) == days


def test_requests_accept_the_clients_rfq_headings(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Requests"
    ws.append(["Vendor Reference", "Part Number", "Alternate Part Number", "Keyword",
               "Part Description", "Serialized", "UOM", "Condition Code",
               "Quantity Requested", "Requested By", "Status"])
    ws.append(["proponent", "AS1895/7-300", "M83248/1-010", "seal", "SEAL, O-RING", "N", "EA",
               "NE", 300, "a@co.com", None])
    wb.save(tmp_path / "t.xlsx")
    (row,) = ExcelSheet(str(tmp_path / "t.xlsx"), "Requests", "Results").read_new_rows()
    assert (row.vendor_raw, row.part_number, row.qty_needed, row.condition_wanted) == (
        "proponent", "AS1895/7-300", "300", "NE")
    assert (row.alternate_part_number, row.keyword, row.part_description, row.serialized,
            row.uom) == ("M83248/1-010", "seal", "SEAL, O-RING", "N", "EA")
