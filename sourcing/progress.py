"""The worker's heartbeat for a sheet: what it is doing, and that it is still alive.

Apps Script stops any one run at 6 minutes, and an AAR search of 11 parts took
15 (2026-11-17): the add-on stopped checking and the rows sat at RUNNING with no
word why. Now the worker saves, per sheet, its current step per login ("AAR:
searching 11 parts") and `alive_at` every HEARTBEAT_SECONDS; /sheet/pull returns
it (`describe`) and the add-on's sidebar shows it. A heartbeat older than
STALE_SECONDS while the run says "running" means the worker stopped (crash,
killed job): the sidebar says so instead of "still searching" for ever.

Only what the store keeps: the add-on writes the results into the sheet itself
(sheet_relay), this is just the status line shown while it waits.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
# Several missed beats: one slow Firestore write or a busy event loop is not a stop.
STALE_SECONDS = 150


class SheetProgress:
    """One sheet's progress in this run. Never raises: a store hiccup must not
    stop a search, the status line is only for show."""

    def __init__(self, store, sheet_id: str, run_id: str, clock=None) -> None:
        self.store = store
        self.sheet_id = sheet_id
        self.run_id = run_id
        self.clock = clock or time.time
        self.steps: dict[str, str] = {}
        self.rows_total = 0
        self._done: set[str] = set()
        self._task: Optional[asyncio.Task] = None

    def _save(self, fields: dict) -> None:
        try:
            self.store.set_sheet_progress(self.sheet_id, {**fields, "alive_at": self.clock()})
        except Exception as exc:
            logger.warning("Sheet %s: progress not saved (%r)", self.sheet_id, exc)

    def start(self) -> None:
        now = self.clock()
        self._save({"run_id": self.run_id, "state": "running", "started_at": now,
                    "finished_at": None, "steps": ["Reading the sheet"], "rows_total": 0,
                    "rows_done": 0})

    def rows_picked(self, n: int) -> None:
        self.rows_total += n
        self._save({"rows_total": self.rows_total, "rows_done": len(self._done)})

    def step(self, key: str, text: Optional[str]) -> None:
        """This login's current step; None removes it (its part of the run ended)."""
        if text:
            self.steps[key] = text
        else:
            self.steps.pop(key, None)
        self._save({"steps": list(self.steps.values()) or ["Working"]})

    def rows_finished(self, row_ids) -> None:
        before = len(self._done)
        self._done.update(row_ids)
        if len(self._done) != before:
            self._save({"rows_done": len(self._done), "rows_total": self.rows_total})

    def finish(self, state: str, counts: Optional[dict] = None) -> None:
        self._save({"state": state, "finished_at": self.clock(), "steps": [],
                    "rows_done": len(self._done), "counts": counts or {}})

    # -- heartbeat ---------------------------------------------------------------
    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(self._save, {})

    def __enter__(self) -> "SheetProgress":
        self.start()
        self._task = asyncio.get_running_loop().create_task(self._beat())
        return self

    def __exit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()


def describe(doc: Optional[dict], now: float) -> Optional[dict]:
    """What /sheet/pull returns to the add-on: state (running / done / failed /
    stopped), the steps, rows done of total, seconds since the start and since
    the last heartbeat."""
    if not doc:
        return None
    alive_at = float(doc.get("alive_at") or 0)
    started_at = float(doc.get("started_at") or alive_at or now)
    state = doc.get("state") or "running"
    if state == "running" and now - alive_at > STALE_SECONDS:
        state = "stopped"
    end = float(doc.get("finished_at") or now)
    return {
        "state": state,
        "steps": list(doc.get("steps") or []),
        "rows_done": int(doc.get("rows_done") or 0),
        "rows_total": int(doc.get("rows_total") or 0),
        "counts": doc.get("counts") or {},
        "elapsed_seconds": max(0, int(end - started_at)),
        "alive_seconds_ago": max(0, int(now - alive_at)),
        "finished_seconds_ago": (max(0, int(now - float(doc["finished_at"])))
                                 if doc.get("finished_at") else None),
    }
