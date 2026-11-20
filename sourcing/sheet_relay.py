"""The worker's sheet access when the service may not open the Sheet.

Client, 2026-11-13: Skylink's org blocks the service account from opening their
Sheets; "the script does every Sheet read and write, and the service never
touches the Sheet". So:

  add-on  -> /trigger      sends a copy of the sheet (Requests tab, RFQ / Results headers)
  worker  -> this module   reads that copy; each write becomes a pending write in the store
  add-on  -> /sheet/pull   collects the pending writes, writes them into the sheet itself
  add-on  -> /sheet/ack    confirms; they are removed from the store

`RelayClient` stands in for a gspread client, so `sheets.SheetsClient` (column
mapping, status notes, RFQ alignment, SUPERSEDED marks) works unchanged:
`SheetsClient(sheet_id, tabs..., gc=RelayClient(store, sheet_id))`.

Writes not yet confirmed are applied on top of the copy when it's opened, so a
second pass doesn't search rows the add-on hasn't marked RUNNING / DONE yet.

Pending writes (JSON, applied by apps_script/addon/Code.gs, applyOps_):
  {"kind": "cells", "tab": t, "updates": [{"range": "C5", "values": [["DONE"]]}],
   "guard": {"col": 1, "rows": {"5": "PN-1"}}}   # Requests: part number expected in that row
  {"kind": "append", "tab": t, "rows": [[...], ...]}
  {"kind": "add_sheet", "tab": t}
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)


class SheetNotSent(RuntimeError):
    """No copy of this sheet in the store: the add-on sends one with each search."""


def _same_status(a: str, b: str) -> bool:
    # Empty and NEW both mean "search this row".
    a, b = (str(a or "").strip().upper(), str(b or "").strip().upper())
    return (a or "NEW") == (b or "NEW")


def stale_rows(op: dict, get) -> set[int]:
    """Rows of a pending status write whose Status isn't what the worker saw when it
    wrote: a person changed it since (e.g. FAILED -> NEW to search again; seen
    2026-11-16: an unconfirmed NEEDS_ATTENTION was laid over the cleared rows and
    the new search found nothing). Their whole write is dropped, so the edit wins.
    `get(row, col)` reads the sheet (here: the copy). Same rule in Code.gs, writeCells_."""
    guard = op.get("guard") or {}
    col, seen = guard.get("status_col"), guard.get("status")
    if not col or not isinstance(seen, dict):
        return set()           # written before 2026-11-16: no status recorded
    return {int(r) for r, before in seen.items() if not _same_status(get(int(r), col), before)}


def _a1(range_name: str) -> tuple[int, int]:
    from gspread.utils import a1_to_rowcol

    return a1_to_rowcol(range_name.split(":")[0])


class RelayWorksheet:
    def __init__(self, book: "RelayBook", title: str, values: list[list]) -> None:
        self._book = book
        self.title = title
        self._grid = [[("" if v is None else str(v)) for v in row] for row in values]
        self._min_cols = 0

    # -- reads (from the copy) -------------------------------------------------
    @property
    def col_count(self) -> int:
        return max([self._min_cols] + [len(r) for r in self._grid])

    def get_all_values(self) -> list[list[str]]:
        width = max([0] + [len(r) for r in self._grid])
        return [r + [""] * (width - len(r)) for r in self._grid]

    def row_values(self, row: int) -> list[str]:
        if row > len(self._grid):
            return []
        values = list(self._grid[row - 1])
        while values and values[-1] == "":        # gspread drops trailing blanks
            values.pop()
        return values

    def cell(self, row: int, col: int):
        return SimpleNamespace(value=self._get(row, col) or None)

    def _get(self, row: int, col: int) -> str:
        if row > len(self._grid) or col > len(self._grid[row - 1]):
            return ""
        return self._grid[row - 1][col - 1]

    # -- writes (to the copy, and queued for the add-on) -------------------------
    def _set(self, row: int, col: int, value) -> None:
        while len(self._grid) < row:
            self._grid.append([])
        line = self._grid[row - 1]
        while len(line) < col:
            line.append("")
        line[col - 1] = "" if value is None else str(value)

    def _put(self, range_name: str, values: list[list]) -> None:
        row, col = _a1(range_name)
        for i, line in enumerate(values):
            for j, value in enumerate(line):
                self._set(row + i, col + j, value)

    def batch_update(self, updates: list[dict], value_input_option: str = "RAW") -> None:
        op = {"kind": "cells", "tab": self.title,
              "updates": [{"range": u["range"], "values": u["values"]} for u in updates]}
        guard = self._book.guard_for(self, [_a1(u["range"])[0] for u in updates])
        if guard:
            op["guard"] = guard
        for u in updates:
            self._put(u["range"], u["values"])
        self._book.queue(op)

    def update(self, values: list[list], range_name: str = "A1",
               value_input_option: str = "RAW") -> None:
        self.batch_update([{"range": range_name, "values": values}], value_input_option)

    def insert_cols(self, values: list[list], col: int = 1,
                    value_input_option: str = "RAW") -> None:
        """A new column at `col` (1 = A); `values` its cells from row 1. Everything to
        its right moves one place, in the copy as in the sheet."""
        self._insert(col, [str(v[0]) if v else "" for v in values])
        self._book.queue({"kind": "insert_cols", "tab": self.title, "at": col,
                          "values": [[str(v[0]) if v else ""] for v in values]})

    def _insert(self, col: int, cells: list[str]) -> None:
        for i, row in enumerate(self._grid):
            row.insert(col - 1, cells[i] if i < len(cells) else "")

    def add_cols(self, n: int) -> None:
        # The add-on adds columns itself when a write needs them.
        self._min_cols = self.col_count + n

    def append_rows(self, values: list[list], value_input_option: str = "RAW",
                    table_range: Optional[str] = None) -> None:
        rows = [[("" if v is None else v) for v in line] for line in values]
        self._grid.extend([[str(v) for v in line] for line in rows])
        self._book.queue({"kind": "append", "tab": self.title, "rows": rows})


class RelayBook:
    def __init__(self, store, sheet_id: str, tabs: dict, requests_tab: Optional[str]) -> None:
        self._store = store
        self.sheet_id = sheet_id
        self._requests_tab = requests_tab
        self._tabs = {name: RelayWorksheet(self, name, values or [])
                      for name, values in tabs.items()}
        self._replaying = False

    def worksheet(self, title: str) -> RelayWorksheet:
        import gspread

        if title not in self._tabs:
            raise gspread.WorksheetNotFound(title)
        return self._tabs[title]

    def add_worksheet(self, title: str, rows: int = 1000, cols: int = 26) -> RelayWorksheet:
        self._tabs.setdefault(title, RelayWorksheet(self, title, []))
        self.queue({"kind": "add_sheet", "tab": title})
        return self._tabs[title]

    def guard_for(self, ws: RelayWorksheet, rows: list[int]) -> Optional[dict]:
        """Part number now in each written Requests row, so the add-on doesn't
        write a status onto another part if rows moved in the meantime; and the
        Status the row had before this write, so a person's later edit wins."""
        if ws.title != self._requests_tab:
            return None
        from .sheets import _column_map

        try:
            cols = _column_map(ws.row_values(1))
        except Exception:
            cols = {}
        col = cols.get("part_number")
        if not col:
            return None
        rows = [r for r in sorted(set(rows)) if r > 1]
        guard = {"col": col, "rows": {str(r): ws._get(r, col) for r in rows}}
        status_col = cols.get("status")
        if status_col:
            guard["status_col"] = status_col
            guard["status"] = {str(r): ws._get(r, status_col) for r in rows}
        return guard

    def queue(self, op: dict) -> None:
        if not self._replaying:
            self._store.add_sheet_ops(self.sheet_id, [op])

    def replay(self, ops: list[dict]) -> None:
        """Writes the add-on hasn't confirmed yet, applied to this copy."""
        self._replaying = True
        try:
            for item in ops:
                op = item["op"]
                if op["kind"] == "add_sheet":
                    self._tabs.setdefault(op["tab"], RelayWorksheet(self, op["tab"], []))
                    continue
                ws = self._tabs.get(op["tab"])
                if ws is None:
                    continue
                if op["kind"] == "cells":
                    stale = stale_rows(op, ws._get)
                    for u in op["updates"]:
                        if _a1(u["range"])[0] in stale:
                            continue
                        ws._put(u["range"], u["values"])
                    if stale:
                        logger.info("Sheet %s: rows %s changed by a person since the "
                                    "worker's write; their Status kept",
                                    self.sheet_id, sorted(stale))
                elif op["kind"] == "append":
                    ws._grid.extend([[str(v) for v in line] for line in op["rows"]])
                elif op["kind"] == "insert_cols":
                    ws._insert(op.get("at", 1), [str(v[0]) if v else "" for v in op["values"]])
        finally:
            self._replaying = False


class RelayClient:
    """Stands in for `gspread.authorize(...)`: open_by_key gives the add-on's copy."""

    def __init__(self, store, requests_tab: Optional[str] = None) -> None:
        self._store = store
        self._requests_tab = requests_tab

    def open_by_key(self, sheet_id: str) -> RelayBook:
        snap = self._store.get_sheet_snapshot(sheet_id)
        if not snap or not snap.get("tabs"):
            raise SheetNotSent(f"no copy of sheet {sheet_id}: the add-on sends one with "
                               "each search")
        book = RelayBook(self._store, sheet_id, snap["tabs"], self._requests_tab)
        book.title = snap.get("title") or ""          # the spreadsheet's name, from the add-on
        pending = self._store.sheet_ops(sheet_id)
        if pending:
            logger.info("Sheet %s: %d write(s) not yet confirmed by the add-on, applied to "
                        "its copy", sheet_id, len(pending))
            book.replay(pending)
        return book


def keep_copy_current(store, sheet_id: str, ids: list[str]) -> None:
    """Apply the writes the add-on just confirmed to the stored copy before they
    leave the queue. Without it the copy kept the Status the sheet had when it
    was sent: 2026-11-19 a second pass of the same sheet read all 40 rows as NEW
    again (their DONE / NOT_FOUND were written and confirmed, so no longer in
    the queue) and searched every part a second time. Status / header writes
    only: appended RFQ / Results rows aren't kept in the copy (headers only)."""
    snap = store.get_sheet_snapshot(sheet_id)
    if not snap or not snap.get("tabs"):
        return
    wanted = set(ids)
    done = [o for o in store.sheet_ops(sheet_id)
            if o["id"] in wanted and o["op"].get("kind") in ("cells", "add_sheet", "insert_cols")]
    if not done:
        return
    book = RelayBook(store, sheet_id, snap["tabs"], None)
    book.replay(done)
    tabs = {name: ws.get_all_values() for name, ws in book._tabs.items()}
    store.put_sheet_snapshot(sheet_id, tabs, snap.get("title") or "")


def open_relay_sheet(store, sheet_id: str, requests_tab: str, results_tab: str):
    from .sheets import SheetsClient

    return SheetsClient(sheet_id, requests_tab, results_tab,
                        gc=RelayClient(store, requests_tab))
