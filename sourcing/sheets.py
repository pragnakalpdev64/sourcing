"""Sheet access for the `Requests` and `Results` tabs (and the optional `RFQ` tab).

Columns are found by header name (case-insensitive), so their order in the
sheet can change.

- `SheetsClient`: Google Sheets. On Cloud Run the runtime service account's
  credentials are used; the sheet must be shared with that service account.
- `ExcelSheet`: a local .xlsx with the same interface, for local testing
  without Google Sheets. `open_sheet()` picks it when the sheet id is a path
  ending in .xlsx.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .models import RENAMED_RESULTS_HEADINGS, RESULTS_HEADER, RFQ_HEADER, RequestRow, RowStatus

logger = logging.getLogger("sourcing.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# field name -> accepted header labels (lowercased). The client's RFQ headings
# (RFQ_Spreadsheet_Sample, 2026-10-19) are accepted too.
REQUEST_HEADERS = {
    "rfq_id": ["rfq id", "rfq", "rfq_id"],
    "vendor": ["vendor", "vendor name", "company", "vendor reference"],
    "part_number": ["part number", "part", "part no", "pn", "part_number"],
    "qty_needed": ["qty needed", "qty", "quantity", "quantity requested"],
    "condition_wanted": ["condition wanted", "condition", "condition code"],
    "requested_by": ["requested by", "requester"],
    "status": ["status"],
    "run_id": ["run id", "run_id"],
    "notes": ["notes", "note"],
    # Optional, copied to the RFQ tab only.
    "alternate_part_number": ["alternate part number", "alternate part", "alternate pn", "alt pn"],
    "keyword": ["keyword"],
    "part_description": ["part description", "description"],
    "serialized": ["serialized"],
    "uom": ["uom", "unit of measure"],
    "type_requested": ["type requested"],
}
RFQ_TAB = "RFQ"

PICKUP_STATUSES = {"", RowStatus.NEW.value}


def _column_map(header: list[str]) -> dict[str, int]:
    """Map field name -> 1-based column index."""
    normalized = [h.strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for field, labels in REQUEST_HEADERS.items():
        for label in labels:
            if label in normalized:
                mapping[field] = normalized.index(label) + 1
                break
    missing = [f for f in ("vendor", "part_number", "status") if f not in mapping]
    if missing:
        raise ValueError(f"Requests tab is missing required columns: {missing}")
    return mapping


def _parse_requests(values: list[list[str]], sheet_id: str,
                    include_running: bool) -> tuple[Optional[dict[str, int]], list[RequestRow]]:
    """Header row + data rows -> (column map, rows to process)."""
    if not values:
        return None, []
    pickup = PICKUP_STATUSES | ({RowStatus.RUNNING.value} if include_running else set())
    cols = _column_map(values[0])

    def cell(row: list[str], field: str) -> str:
        idx = cols.get(field)
        if not idx or idx > len(row):
            return ""
        return str(row[idx - 1]).strip()

    rows: list[RequestRow] = []
    no_part = 0
    skipped: dict[str, int] = {}
    for offset, raw in enumerate(values[1:], start=2):
        part = cell(raw, "part_number")
        status = cell(raw, "status").upper()
        if not part:
            no_part += 1
            continue
        if status not in pickup:
            skipped[status] = skipped.get(status, 0) + 1
            continue
        rows.append(RequestRow(
            sheet_row=offset,
            sheet_id=sheet_id,
            rfq_id=cell(raw, "rfq_id"),
            vendor_raw=cell(raw, "vendor"),
            part_number=part,
            qty_needed=cell(raw, "qty_needed"),
            condition_wanted=cell(raw, "condition_wanted"),
            requested_by=cell(raw, "requested_by"),
            status=status or RowStatus.NEW.value,
            run_id=cell(raw, "run_id"),
            alternate_part_number=cell(raw, "alternate_part_number"),
            keyword=cell(raw, "keyword"),
            part_description=cell(raw, "part_description"),
            serialized=cell(raw, "serialized"),
            uom=cell(raw, "uom"),
            type_requested=cell(raw, "type_requested"),
        ))
    skipped_text = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) or "none"
    logger.info("Requests in %s: %d data row(s), %d to process (Status empty/NEW%s), "
                "skipped by Status: %s, rows without a part number: %d",
                sheet_id, len(values) - 1, len(rows), "/RUNNING" if include_running else "",
                skipped_text, no_part)
    return cols, rows


def align_results(header: list[str], rows: list, columns: list[str] = RESULTS_HEADER,
                  renamed: Optional[dict[str, str]] = None
                  ) -> tuple[list[str], bool, list[list[str]]]:
    """Place result values under the tab's own column headings.

    `rows` are ResultRow.as_sheet_record() dicts (plain lists are read in
    `columns` order); `columns` is RESULTS_HEADER, or RFQ_HEADER for the RFQ tab.
    Columns are matched by heading, ignoring case, so
    columns people add or reorder keep their place and stay untouched. A
    renamed heading (RENAMED_RESULTS_HEADINGS) is renamed in place; our other
    headings missing from the tab are added at the end. Returns (header,
    header changed, value rows)."""
    if renamed is None:
        renamed = RENAMED_RESULTS_HEADINGS if columns is RESULTS_HEADER else {}
    header = [str(h or "").strip() for h in header]
    while header and not header[-1]:
        header.pop()
    changed = False
    if not header:
        header, changed = list(columns), True
    present = {h.lower() for h in header}
    for i, h in enumerate(header):
        new = renamed.get(h.lower())
        if new and new.lower() not in present:
            header[i], changed = new, True
    index = {h.lower(): i for i, h in enumerate(header) if h}
    for label in columns:
        if label.lower() not in index:
            index[label.lower()] = len(header)
            header.append(label)
            changed = True
    out = []
    for row in rows:
        record = row if isinstance(row, dict) else dict(zip(columns, row))
        values = [""] * len(header)
        for label, value in record.items():
            i = index.get(str(label).lower())
            if i is not None:
                values[i] = "" if value is None else str(value)
        out.append(values)
    return header, changed, out


# How long a tab's header read from the sheet is reused (SheetsClient._append).
HEADER_SECONDS = 60

# When a part is searched again in a later run, its older Results rows are
# marked SUPERSEDED, so the team sees which lines are current (Monday's search
# showed qty 100, Tuesday's shows 20: Monday's line is marked, nothing is
# deleted, approvals stay). Rows the same run wrote are never marked: the
# same part asked twice in one sheet gets two current sets (2026-10-28).
SUPERSEDED = "SUPERSEDED"
# Results rows of one request: a newer search of it marks the older rows.
_REQUEST_KEYS = ("RFQ ID", "Vendor", "Requested Part", "Requested By")


def unmarked_requests(values: list[list[str]]) -> dict[tuple, set]:
    """Request key (_REQUEST_KEYS) -> the Fetched At of its Results rows not yet
    marked SUPERSEDED. Lets a run skip reading the Results tab again when no
    earlier search of a request can be on it (Sheets read quota, 2026-10-27)."""
    if not values:
        return {}
    header = [str(h).strip().lower() for h in values[0]]
    try:
        cols = [header.index(k.lower()) for k in _REQUEST_KEYS]
        fetched, flag = header.index("fetched at"), header.index("flag")
    except ValueError:
        return {}
    out: dict[tuple, set] = {}
    for row in values[1:]:
        cell = [str(row[i]).strip() if i < len(row) and row[i] is not None else ""
                for i in (*cols, fetched, flag)]
        if not cell[-1].startswith(SUPERSEDED):
            out.setdefault(tuple(cell[:len(cols)]), set()).add(cell[len(cols)])
    return out


def _request_key(record: dict) -> tuple[tuple, str]:
    low = {str(k).lower(): str(v or "").strip() for k, v in record.items()}
    return tuple(low.get(k.lower(), "") for k in _REQUEST_KEYS), low.get("fetched at", "")


def superseded_flags(values: list[list[str]], records: list[dict],
                     this_run: frozenset = frozenset()) -> list[tuple[int, int, str]]:
    """Results rows an earlier search of the same request wrote (same RFQ ID,
    Vendor, Requested Part and Requested By, another Fetched At) that aren't
    marked yet -> (sheet row, Flag column, new Flag), both 1-based. A re-run or a
    resumed run then leaves one current set of rows per request, and approvals
    on the old rows stay visible. `this_run`: (request key, Fetched At) of rows
    the current run wrote, never marked (426EN10-8 in rows 10 and 25 of one
    run, 2026-10-27)."""
    if not values or not records:
        return []
    header = [str(h).strip().lower() for h in values[0]]
    try:
        cols = [header.index(k.lower()) for k in _REQUEST_KEYS]
        fetched, flag = header.index("fetched at"), header.index("flag")
    except ValueError:
        return []

    def cell(row: list, i: int) -> str:
        return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

    new = {}
    for r in records:
        low = {str(k).lower(): str(v or "").strip() for k, v in r.items()}
        new[tuple(low.get(k.lower(), "") for k in _REQUEST_KEYS)] = low.get("fetched at", "")
    out = []
    for n, row in enumerate(values[1:], start=2):
        key = tuple(cell(row, i) for i in cols)
        when = new.get(key)
        old_flag = cell(row, flag)
        if (when is None or cell(row, fetched) == when or old_flag.startswith(SUPERSEDED)
                or (key, cell(row, fetched)) in this_run):
            continue
        out.append((n, flag + 1, f"{SUPERSEDED} by search of {when}"
                    + (f"; {old_flag}" if old_flag else "")))
    return out


class SheetsClient:
    def __init__(self, sheet_id: str, requests_tab: str, results_tab: str, gc=None) -> None:
        if not sheet_id:
            raise ValueError("SHEET_ID is not set")
        if gc is None:
            import google.auth
            import gspread

            creds, _ = google.auth.default(scopes=SCOPES)
            gc = gspread.authorize(creds)
        self.sheet_id = sheet_id
        self._book = gc.open_by_key(sheet_id)
        self._requests = self._book.worksheet(requests_tab)
        self._results_tab = results_tab
        self._results = None
        self._cols: Optional[dict[str, int]] = None
        # Read once per run, not once per written row: Google allows 60 read
        # requests a minute, and a bulk run writes a row every few seconds
        # (429 "Read requests per minute per user", 2026-10-27).
        self._rfq = None
        # Header per tab and when it was read; read again after HEADER_SECONDS,
        # so a column a person adds mid-run is seen (about one read a minute).
        self._headers: dict[str, tuple[float, list[str]]] = {}
        self._open_requests: Optional[dict[tuple, set]] = None
        # (request key, Fetched At) of Results rows this run wrote: not marked
        # SUPERSEDED by the run's own later rows.
        self._written: set[tuple] = set()

    # -- Requests tab -----------------------------------------------------
    def read_new_rows(self, include_running: bool = False) -> list[RequestRow]:
        """Rows to process: Status NEW or empty. `include_running` also returns
        RUNNING rows, which the worker uses at startup to resume a run that died
        (it holds the worker lock, so no other run can own them)."""
        self._cols, rows = _parse_requests(self._requests.get_all_values(), self.sheet_id,
                                           include_running)
        return rows

    def set_status(self, rows: Iterable[RequestRow], status: RowStatus,
                   run_id: Optional[str] = None, note: Optional[str] = None) -> None:
        self.set_statuses([(rows, status, note)], run_id)

    def set_statuses(self, items: list[tuple[Iterable[RequestRow], RowStatus, Optional[str]]],
                     run_id: Optional[str] = None) -> None:
        """Several (rows, status, note) in one write request: a bulk batch's rows
        each get their own status and note (Sheets write quota, 2026-10-28)."""
        from gspread.utils import rowcol_to_a1

        if self._cols is None:
            self._cols = _column_map(self._requests.row_values(1))
        updates = []
        for rows, status, note in items:
            updates += self._status_updates(rows, status, run_id, note, rowcol_to_a1)
        if updates:
            self._requests.batch_update(updates, value_input_option="RAW")

    def _status_updates(self, rows, status: RowStatus, run_id: Optional[str],
                        note: Optional[str], rowcol_to_a1) -> list[dict]:
        updates = []
        for row in rows:
            row.status = status.value
            updates.append({"range": rowcol_to_a1(row.sheet_row, self._cols["status"]),
                            "values": [[status.value]]})
            if run_id is not None and "run_id" in self._cols:
                row.run_id = run_id
                updates.append({"range": rowcol_to_a1(row.sheet_row, self._cols["run_id"]),
                                "values": [[run_id]]})
            if note is not None and "notes" in self._cols:
                updates.append({"range": rowcol_to_a1(row.sheet_row, self._cols["notes"]),
                                "values": [[note[:500]]]})
        return updates

    # -- Results tab --------------------------------------------------------
    def _results_ws(self):
        if self._results is None:
            self._results = self._book.worksheet(self._results_tab)
        return self._results

    def append_results(self, rows: list) -> None:
        if rows:
            self._append(self._results_ws(), rows, RESULTS_HEADER)
            for record in rows:
                key, when = _request_key(record)
                self._written.add((key, when))
                if self._open_requests is not None:
                    self._open_requests.setdefault(key, set()).add(when)

    def append_rfq(self, rows: list) -> None:
        """ResultRow.as_rfq_record() dicts -> the RFQ tab, created when missing."""
        import gspread

        if not rows:
            return
        if self._rfq is None:
            try:
                self._rfq = self._book.worksheet(RFQ_TAB)
            except gspread.WorksheetNotFound:
                self._rfq = self._book.add_worksheet(RFQ_TAB, rows=1000, cols=len(RFQ_HEADER))
        self._append(self._rfq, rows, RFQ_HEADER)

    def _append(self, ws, rows: list, columns: list[str]) -> None:
        if not rows:
            return
        tab = getattr(ws, "title", None) or str(id(ws))
        read_at, known = self._headers.get(tab, (0.0, None))
        if known is None or time.time() - read_at > HEADER_SECONDS:
            read_at, known = time.time(), ws.row_values(1)
        header, changed, values = align_results(known, rows, columns)
        if changed:
            if ws.col_count < len(header):
                ws.add_cols(len(header) - ws.col_count)
            ws.update(values=[header], range_name="A1", value_input_option="RAW")
        self._headers[tab] = (read_at, header)
        # RAW so a part number like "35010-38" is never turned into a date.
        ws.append_rows(values, value_input_option="RAW", table_range="A1")

    def supersede_results(self, rows: list) -> int:
        """Mark older Results rows of the requests in `rows` (see superseded_flags).
        The tab is read in full only when an earlier search of one of these
        requests may be on it; the first call of a run reads it once to know."""
        from gspread.utils import rowcol_to_a1

        if self._open_requests is not None and not any(
                {f for f in self._open_requests.get(key, set()) - {when}
                 if (key, f) not in self._written}
                for key, when in map(_request_key, rows)):
            return 0
        values = self._results_ws().get_all_values()
        marks = superseded_flags(values, rows, frozenset(self._written))
        if marks:
            self._results_ws().batch_update(
                [{"range": rowcol_to_a1(r, c), "values": [[text]]} for r, c, text in marks],
                value_input_option="RAW")
            marked = {r for r, _, _ in marks}
            values = [v for n, v in enumerate(values, start=1) if n not in marked]
        self._open_requests = unmarked_requests(values)
        return len(marks)

    def part_at(self, sheet_row: int) -> Optional[str]:
        """Part number now in this Requests row (rows shift when some are deleted)."""
        if self._cols is None:
            self._cols = _column_map(self._requests.row_values(1))
        return self._requests.cell(sheet_row, self._cols["part_number"]).value

    def has_rfq(self, rows: list) -> bool:
        """True if every RFQ line is already on the RFQ tab (same vendor, PN,
        quantity, price, trace and quote date); used before retrying a failed
        append when the RFQ tab is the run's only output. The tab has no time
        column, so an identical line quoted earlier the same day counts too."""
        import gspread

        keys = ("Vendor Reference", "PN Quoted", "Qty Quoted", "Unit Price", "Trace",
                "Quoted Date")
        try:
            values = (self._rfq or self._book.worksheet(RFQ_TAB)).get_all_values()
        except gspread.WorksheetNotFound:
            return False
        if not values:
            return False
        cols = [values[0].index(k) if k in values[0] else None for k in keys]
        if None in cols:
            return False
        saved = {tuple(r[c] if c < len(r) else "" for c in cols) for r in values[1:]}
        return all(tuple(str(row.get(k, "")) for k in keys) in saved for row in rows)

    def has_results(self, rows: list) -> bool:
        """True if every row (same Requested Part, Part Number and Fetched At) is
        already on the Results tab; used before retrying a failed append."""
        keys = ("Requested Part", "Part Number", "Fetched At")
        values = self._results_ws().get_all_values()
        if not values:
            return False
        cols = [values[0].index(k) if k in values[0] else None for k in keys]
        if None in cols:
            return False
        saved = {tuple(r[c] if c < len(r) else "" for c in cols) for r in values[1:]}
        return all(tuple(str(row.get(k, "")) for k in keys) in saved for row in rows)


def _excel_text(value) -> str:
    """Excel cell -> the text Sheets would show (1.0 -> "1", None -> "")."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class ExcelSheet:
    """Local .xlsx with the same interface as SheetsClient. Every status
    update and result is saved to the file straight away, so you can watch
    progress by reopening it. Close the file in Excel/LibreOffice before a run:
    saving fails while another program has it open (and would overwrite edits
    made there during the run)."""

    def __init__(self, path: str, requests_tab: str, results_tab: str) -> None:
        import openpyxl

        self.path = path
        self.sheet_id = path
        self._lock = threading.Lock()  # the pipeline writes from several threads
        self._wb = openpyxl.load_workbook(path)
        if requests_tab not in self._wb.sheetnames:
            raise ValueError(f"{path}: no '{requests_tab}' tab (tabs: {self._wb.sheetnames})")
        self._requests = self._wb[requests_tab]
        self._results_tab = results_tab
        self._cols: Optional[dict[str, int]] = None
        self._written: set[tuple] = set()   # rows this run wrote (see SheetsClient)
        logger.info("Opened local Excel file %s (tabs: %s)", path, ", ".join(self._wb.sheetnames))

    def _save(self) -> None:
        self._wb.save(self.path)

    def read_new_rows(self, include_running: bool = False) -> list[RequestRow]:
        with self._lock:
            values = [[_excel_text(v) for v in row]
                      for row in self._requests.iter_rows(values_only=True)]
        self._cols, rows = _parse_requests(values, self.sheet_id, include_running)
        return rows

    def set_status(self, rows: Iterable[RequestRow], status: RowStatus,
                   run_id: Optional[str] = None, note: Optional[str] = None) -> None:
        with self._lock:
            if self._cols is None:
                header = [_excel_text(c.value) for c in self._requests[1]]
                self._cols = _column_map(header)
            changed = False
            for row in rows:
                row.status = status.value
                self._requests.cell(row=row.sheet_row, column=self._cols["status"],
                                    value=status.value)
                if run_id is not None and "run_id" in self._cols:
                    row.run_id = run_id
                    self._requests.cell(row=row.sheet_row, column=self._cols["run_id"],
                                        value=run_id)
                if note is not None and "notes" in self._cols:
                    self._requests.cell(row=row.sheet_row, column=self._cols["notes"],
                                        value=note[:500])
                changed = True
            if changed:
                self._save()

    def set_statuses(self, items: list, run_id: Optional[str] = None) -> None:
        for rows, status, note in items:
            self.set_status(rows, status, run_id, note)

    def append_results(self, rows: list) -> None:
        self._append(self._results_tab, rows, RESULTS_HEADER)
        self._written.update(_request_key(record) for record in rows)

    def append_rfq(self, rows: list) -> None:
        """ResultRow.as_rfq_record() dicts -> the RFQ tab, created when missing."""
        self._append(RFQ_TAB, rows, RFQ_HEADER)

    def _append(self, tab: str, rows: list, columns: list[str]) -> None:
        if not rows:
            return
        with self._lock:
            if tab in self._wb.sheetnames:
                ws = self._wb[tab]
            else:
                ws = self._wb.create_sheet(tab)
            current = [_excel_text(c.value) for c in ws[1]] if ws.max_row >= 1 else []
            header, changed, values = align_results(current, rows, columns)
            if changed:
                for col, label in enumerate(header, start=1):
                    ws.cell(row=1, column=col, value=label)
            for row in values:
                ws.append(row)
            self._save()

    def supersede_results(self, rows: list) -> int:
        with self._lock:
            if self._results_tab not in self._wb.sheetnames:
                return 0
            ws = self._wb[self._results_tab]
            values = [[_excel_text(v) for v in r] for r in ws.iter_rows(values_only=True)]
            marks = superseded_flags(values, rows, frozenset(self._written))
            for r, c, text in marks:
                ws.cell(row=r, column=c, value=text)
            if marks:
                self._save()
            return len(marks)

    def part_at(self, sheet_row: int) -> Optional[str]:
        with self._lock:
            if self._cols is None:
                self._cols = _column_map([_excel_text(c.value) for c in self._requests[1]])
            return _excel_text(self._requests.cell(row=sheet_row,
                                                   column=self._cols["part_number"]).value)


def open_sheet(sheet_id: str, requests_tab: str, results_tab: str):
    """Google Sheet by id, or a local .xlsx when the id is a path ending in .xlsx
    (relative paths are taken from the sourcing-proto folder)."""
    if sheet_id.lower().endswith(".xlsx"):
        path = sheet_id if os.path.isabs(sheet_id) else str(
            Path(__file__).resolve().parent.parent / sheet_id)
        return ExcelSheet(path, requests_tab, results_tab)
    return SheetsClient(sheet_id, requests_tab, results_tab)
