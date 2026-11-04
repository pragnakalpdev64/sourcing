from __future__ import annotations

import pyotp
import pytest

from sourcing.credentials.base import CredentialNotFound, MemoryCredentialProvider
from sourcing.models import RESULTS_HEADER, RowStatus
from sourcing.sheets import SheetsClient


class FakeWorksheet:
    def __init__(self, values):
        self.values = values
        self.updates = []
        self.appended = []

    def get_all_values(self):
        return self.values

    def row_values(self, n):
        return self.values[n - 1] if len(self.values) >= n else []

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)

    def append_row(self, row, value_input_option=None):
        self.values.append(row)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        self.appended.extend(rows)

    col_count = 26

    def add_cols(self, n):
        self.col_count += n

    def update(self, values=None, range_name=None, value_input_option=None):
        assert range_name == "A1"
        if self.values:
            self.values[0] = values[0]
        else:
            self.values.append(values[0])


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, name):
        return self.tabs[name]


class FakeGC:
    def __init__(self, book):
        self.book = book

    def open_by_key(self, key):
        return self.book


def test_reads_new_and_blank_rows_and_writes_status_by_header():
    requests = FakeWorksheet([
        ["RFQ ID", "Vendor", "Part Number", "Qty Needed", "Condition Wanted",
         "Requested By", "Status", "Run ID", "Notes"],
        ["R1", "Proponent", "35010-38", "4", "NE", "Jane@Co.com", "NEW", "", ""],
        ["R2", "Proponent", "35010-39", "", "", "", "", "", ""],       # blank status -> picked up
        ["R3", "Proponent", "35010-40", "", "", "", "DONE", "x", ""],  # already done
        ["R4", "Proponent", "", "", "", "", "", "", ""],               # no part number
    ])
    results = FakeWorksheet([])
    client = SheetsClient("id", "Requests", "Results", gc=FakeGC(FakeBook(
        {"Requests": requests, "Results": results})))

    rows = client.read_new_rows()
    assert [r.part_number for r in rows] == ["35010-38", "35010-39"]
    assert rows[0].requester == "jane@co.com" and rows[0].sheet_row == 2

    client.set_status(rows[:1], RowStatus.RUNNING, run_id="run1", note="hi")
    assert requests.updates == [
        {"range": "G2", "values": [["RUNNING"]]},
        {"range": "H2", "values": [["run1"]]},
        {"range": "I2", "values": [["hi"]]},
    ]

    client.append_results([{"RFQ ID": "R1", "Vendor": "Proponent", "Part Number": "35010-38"}])
    assert results.values[0] == RESULTS_HEADER        # header written on first use
    row = dict(zip(RESULTS_HEADER, results.appended[0]))
    assert (row["RFQ ID"], row["Vendor"], row["Part Number"]) == ("R1", "Proponent", "35010-38")


def test_results_go_under_their_headings_and_added_columns_are_left_alone():
    """A person added "Qty on website" and the tab predates "Requested Part" /
    "Warehouses": values still land under the right heading, the person's
    column stays empty, and the missing headings are added at the end."""
    old_header = ["RFQ ID", "Vendor", "Part Number", "Price", "Currency", "Qty Available",
                  "Qty on website", "Condition", "Lead Time", "Source URL", "Fetched At",
                  "Requested By"]
    results = FakeWorksheet([list(old_header)])
    client = SheetsClient("id", "Requests", "Results", gc=FakeGC(FakeBook(
        {"Requests": FakeWorksheet([["Vendor", "Part Number", "Status"]]), "Results": results})))
    client.append_results([{"RFQ ID": "R1", "Requested Part": "ACHC-3-13",
                            "Part Number": "ACHC3-13", "Qty Available": "10",
                            "Condition": "FN", "Flag": "PN_VARIANT"}])
    header = results.values[0]
    # existing columns keep their place; the old "Price" heading is renamed in place
    assert header[:len(old_header)] == [
        "Unit Price" if h == "Price" else h for h in old_header]
    assert {"Requested Part", "Warehouses", "Flag", "Approved"} <= set(header[len(old_header):])
    row = dict(zip(header, results.appended[0]))
    assert row["Part Number"] == "ACHC3-13" and row["Qty Available"] == "10"
    assert row["Qty on website"] == "" and row["Condition"] == "FN"
    assert row["Requested Part"] == "ACHC-3-13" and row["Flag"] == "PN_VARIANT"


def test_missing_required_column_is_reported():
    ws = FakeWorksheet([["Vendor", "Part Number"], ["Proponent", "X"]])
    client = SheetsClient("id", "Requests", "Results", gc=FakeGC(FakeBook({"Requests": ws})))
    with pytest.raises(ValueError, match="status"):
        client.read_new_rows()


@pytest.mark.asyncio
async def test_credential_lookup_order_and_update_merges():
    seed = pyotp.random_base32()
    creds = MemoryCredentialProvider({
        "sourcing-vendor-proponent__jane-co-com": {"username": "jane", "password": "s3cr3t-pw", "totp_secret": seed},
        "sourcing-vendor-proponent": {"username": "team", "password": "b"},
    })
    own = await creds.resolve("proponent", "Jane@Co.com")
    assert own.owner == "jane@co.com" and own.totp_now() == pyotp.TOTP(seed).now()
    assert "s3cr3t-pw" not in repr(own) and "password" not in repr(own)  # password never in repr/logs

    shared = await creds.resolve("proponent", "bob@co.com")
    assert shared.is_shared and shared.totp_now() is None

    with pytest.raises(CredentialNotFound):
        await creds.resolve("wencor", "jane@co.com")

    await creds.update("proponent", "jane@co.com", {"password": "new"})
    assert creds.secrets["sourcing-vendor-proponent__jane-co-com"] == {
        "username": "jane", "password": "new", "totp_secret": seed}

    # The login update page never creates a secret (the client does).
    with pytest.raises(CredentialNotFound):
        await creds.update("proponent", "bob@co.com", {"password": "x"}, must_exist=True)
    assert "sourcing-vendor-proponent__bob-co-com" not in creds.secrets


@pytest.mark.asyncio
async def test_results_write_is_retried_after_a_dropped_connection():
    from sourcing.models import ResultRow
    from sourcing.sinks.sheet import SheetSink

    class Flaky:
        def __init__(self, fail_times, applied_anyway=False):
            self.fail_times, self.applied_anyway, self.rows = fail_times, applied_anyway, []

        def append_results(self, records):
            if self.fail_times:
                self.fail_times -= 1
                if self.applied_anyway:
                    self.rows += records
                raise ConnectionError("Remote end closed connection without response")
            self.rows += records

        def has_results(self, records):
            return all(r in self.rows for r in records)

    row = ResultRow(rfq_id="", vendor="Proponent", requested_part="AS15001-1P",
                    part_number="AS15001-1P", fetched_at="2026-10-14 07:36:04Z")
    sheet = Flaky(1)
    assert [o.ok for o in await SheetSink(sheet, (0.01,)).write([row])] == [True]
    assert len(sheet.rows) == 1
    # the dropped call was applied after all: not written a second time
    sheet = Flaky(1, applied_anyway=True)
    assert [o.ok for o in await SheetSink(sheet, (0.01,)).write([row])] == [True]
    assert len(sheet.rows) == 1
    # still failing after every retry: FAILED with the error
    out = await SheetSink(Flaky(5), (0.01, 0.01)).write([row])
    assert not out[0].ok and "Remote end closed" in out[0].error


class CountingWorksheet(FakeWorksheet):
    """Counts the read calls (Sheets read quota, 2026-10-27); appended rows land
    in the values like on a real sheet."""

    def __init__(self, values, title):
        super().__init__(values)
        self.title, self.reads = title, 0

    def get_all_values(self):
        self.reads += 1
        return super().get_all_values()

    def row_values(self, n):
        self.reads += 1
        return super().row_values(n)

    def append_rows(self, rows, value_input_option=None, table_range=None):
        super().append_rows(rows)
        self.values.extend(rows)

    def batch_update(self, updates, value_input_option=None):
        from gspread.utils import a1_to_rowcol

        super().batch_update(updates)
        for u in updates:
            r, c = a1_to_rowcol(u["range"])
            row = self.values[r - 1]
            row.extend([""] * (c - len(row)))
            row[c - 1] = u["values"][0][0]


def test_a_run_reads_each_tab_once_not_once_per_row():
    from sourcing.models import RESULTS_HEADER, RFQ_HEADER, ResultRow

    def record(part, when):
        return ResultRow(rfq_id="", vendor="Proponent", requested_part=part, part_number=part,
                         fetched_at=when).as_sheet_record()

    # P3 was searched by an earlier run too.
    earlier = [record("P3", "2026-10-27 09:00:00Z").get(h, "") for h in RESULTS_HEADER]
    results = CountingWorksheet([list(RESULTS_HEADER), earlier], "Results")
    rfq = CountingWorksheet([list(RFQ_HEADER)], "RFQ")

    def client():
        return SheetsClient("id", "Requests", "Results", gc=FakeGC(FakeBook(
            {"Requests": FakeWorksheet([["Part Number", "Status"]]), "Results": results,
             "RFQ": rfq})))

    run = client()
    marked = 0
    for n in range(10):
        row = ResultRow(rfq_id="", vendor="Proponent", requested_part=f"P{n}",
                        part_number=f"P{n}", fetched_at=f"2026-10-28 10:00:{n:02d}Z")
        run.append_results([row.as_sheet_record()])
        marked += run.supersede_results([row.as_sheet_record()])
        run.append_rfq([row.as_rfq_record()])
    # header once each, the Results tab once to know, once more for P3: 4 reads, not ~40
    assert (results.reads, rfq.reads, marked) == (3, 1, 1)
    assert results.values[1][RESULTS_HEADER.index("Flag")].startswith("SUPERSEDED")

    # The same part again in the same run (rows 10 and 25 of 2026-10-27): both
    # sets stay current, and nothing is read to find that out.
    again = record("P3", "2026-10-28 10:05:00Z")
    run.append_results([again])
    assert run.supersede_results([again]) == 0 and results.reads == 3
    # A later run marks both of this run's P3 rows.
    later = client()
    third = record("P3", "2026-10-29 08:00:00Z")
    later.append_results([third])
    assert later.supersede_results([third]) == 2


class QuotaError(Exception):
    """gspread's APIError shows the status and message in both str and repr."""

    def __repr__(self):
        return str(self)

    def __str__(self):
        return ("APIError: [429]: Quota exceeded for quota metric 'Read requests' and limit "
                "'Read requests per minute per user'")


@pytest.mark.asyncio
async def test_over_quota_waits_a_minute_and_never_loses_the_rfq_line():
    from sourcing.models import ResultRow
    from sourcing.sinks.sheet import QUOTA_RETRIES, SheetSink

    class Sheet:
        def __init__(self, results_fail, rfq_fail):
            self.results_fail, self.rfq_fail = results_fail, rfq_fail
            self.results, self.rfq, self.checks = [], [], 0

        def append_results(self, records):
            if self.results_fail:
                self.results_fail -= 1
                raise QuotaError()
            self.results += records

        def has_results(self, records):
            self.checks += 1          # a 429 writes nothing: never checked
            return False

        def append_rfq(self, records):
            if self.rfq_fail:
                self.rfq_fail -= 1
                raise QuotaError()
            self.rfq += records

    row = ResultRow(rfq_id="", vendor="Proponent", requested_part="4900397-7",
                    part_number="4900397-7", fetched_at="2026-10-27 20:10:31Z")
    # Row 18 of 2026-10-27: Results 429 twice, RFQ 429 once -> both written.
    sheet = Sheet(2, 1)
    out = await SheetSink(sheet, (0.01, 0.01), rfq=True, quota_wait=0.01).write([row])
    assert (out[0].ok, out[0].rfq_error) == (True, None)
    assert (len(sheet.results), len(sheet.rfq), sheet.checks) == (1, 1, 0)
    # RFQ tab over quota after every wait: Results saved, the RFQ error returned.
    sheet = Sheet(0, QUOTA_RETRIES + 1)
    out = await SheetSink(sheet, (0.01,), rfq=True, quota_wait=0.01).write([row])
    assert out[0].ok and "429" in out[0].rfq_error and sheet.rfq == []
    # Results over quota every time: FAILED, kept by the pipeline as before.
    out = await SheetSink(Sheet(10, 0), (0.01,), rfq=True, quota_wait=0.01).write([row])
    assert not out[0].ok and "429" in out[0].error


@pytest.mark.asyncio
async def test_rfq_tab_alone_is_the_write_a_row_depends_on():
    from sourcing.models import ResultRow
    from sourcing.sinks.sheet import SheetSink

    class Sheet:
        """RFQ tab that drops `fail_times` calls (applied anyway or not)."""

        def __init__(self, fail_times=0, applied_anyway=False):
            self.fail_times, self.applied_anyway = fail_times, applied_anyway
            self.rfq, self.results = [], []

        def append_results(self, records):
            self.results += records

        def append_rfq(self, records):
            if self.fail_times:
                self.fail_times -= 1
                if self.applied_anyway:
                    self.rfq += records
                raise ConnectionError("Remote end closed connection without response")
            self.rfq += records

        def has_rfq(self, records):
            return all(r in self.rfq for r in records)

    row = ResultRow(rfq_id="", vendor="DASI", requested_part="MS24665-132",
                    part_number="MS24665-132", fetched_at="2026-10-29 18:28:30Z")
    sink = lambda sheet: SheetSink(sheet, (0.01,), rfq=True, results=False)  # noqa: E731
    sheet = Sheet()
    out = await sink(sheet).write([row])
    assert (out[0].ok, len(sheet.rfq), sheet.results) == (True, 1, [])     # Results untouched
    sheet = Sheet(1)
    assert (await sink(sheet).write([row]))[0].ok and len(sheet.rfq) == 1  # retried
    sheet = Sheet(1, applied_anyway=True)
    assert (await sink(sheet).write([row]))[0].ok and len(sheet.rfq) == 1  # not twice
    out = await sink(Sheet(5)).write([row])                                # kept by the pipeline
    assert not out[0].ok and "Remote end closed" in out[0].error and out[0].rfq_error is None
