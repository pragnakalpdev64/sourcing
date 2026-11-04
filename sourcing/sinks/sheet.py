from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..models import ResultRow
from .base import WriteOutcome

logger = logging.getLogger("sourcing.sinks.sheet")

# Google sometimes drops the connection mid-request ("Remote end closed connection
# without response"); a second try a few seconds later goes through.
RETRY_DELAYS = (3.0, 10.0)
# Over the Sheets quota (429, "Read/Write requests per minute per user", seen
# 2026-10-27 in a 24-part bulk run) a retry seconds later fails again: wait for
# the next minute, up to QUOTA_RETRIES times.
QUOTA_WAIT = 65.0
QUOTA_RETRIES = 3


def over_quota(exc: Exception) -> bool:
    code = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "code", None)
    return code == 429 or "[429]" in str(exc) or "Quota exceeded" in str(exc)


class SheetSink:
    """Appends offers to the `Results` tab and/or, in the client's RFQ layout, to
    the `RFQ` tab. A worker run writes only the RFQ tab, and Results too with
    job_main --results (client, 2026-10-29). With Results off the RFQ write is the
    one a row depends on: it gets the retries, and its rows are kept for the
    next run when it fails."""

    def __init__(self, sheets, retry_delays: tuple[float, ...] = RETRY_DELAYS,
                 rfq: bool = False, quota_wait: float = QUOTA_WAIT,
                 results: bool = True) -> None:
        self._sheets = sheets
        self._retry_delays = retry_delays
        self._rfq = rfq
        self._results = results
        self._quota_wait = quota_wait

    async def _wait(self, exc: Exception, delays: list[float], quota_waits: list[int],
                    what: str) -> bool:
        """Sleep before the next try; False when there are no tries left."""
        if over_quota(exc) and quota_waits[0] < QUOTA_RETRIES:
            quota_waits[0] += 1
            logger.warning("%s: over the Google Sheets quota; waiting %.0fs (%d/%d)", what,
                           self._quota_wait, quota_waits[0], QUOTA_RETRIES)
            await asyncio.sleep(self._quota_wait)
            return True
        if delays:
            await asyncio.sleep(delays.pop(0))
            return True
        return False

    async def write(self, rows: list[ResultRow]) -> list[WriteOutcome]:
        if not rows:
            return []
        if not self._results:
            error = await self._write_rfq_only(rows) if self._rfq else None
            return [WriteOutcome(ok=error is None, error=error) for _ in rows]
        records = [r.as_sheet_record() for r in rows]
        delays, quota_waits, attempt = list(self._retry_delays), [0], 0
        while True:
            attempt += 1
            try:
                await asyncio.to_thread(self._sheets.append_results, records)
                break
            except Exception as exc:  # one append call: all rows share the outcome
                error = repr(exc)[:300]
                logger.warning("Results write attempt %d failed: %s", attempt, error)
                if not await self._wait(exc, delays, quota_waits, "Results write"):
                    return [WriteOutcome(ok=False, error=error) for _ in rows]
                # A dropped call may still have been applied; don't add the rows
                # twice. A 429 is refused before anything is written: no check.
                if not over_quota(exc) and await self._saved_already(records):
                    logger.info("Results write: rows were saved by the failed attempt")
                    break
        await self._supersede(records)
        rfq_error = await self.write_rfq(rows)
        return [WriteOutcome(ok=True, rfq_error=rfq_error) for _ in rows]

    async def _write_rfq_only(self, rows: list[ResultRow]) -> Optional[str]:
        """The RFQ tab as the run's only output: retried like the Results write
        (a dropped call is checked for saved lines first). The error, or None."""
        append = getattr(self._sheets, "append_rfq", None)
        if append is None:
            return "this sheet backend has no RFQ tab"
        records = [r.as_rfq_record() for r in rows]
        delays, quota_waits, attempt = list(self._retry_delays), [0], 0
        while True:
            attempt += 1
            try:
                await asyncio.to_thread(append, records)
                return None
            except Exception as exc:
                error = repr(exc)[:300]
                logger.warning("RFQ write attempt %d failed: %s", attempt, error)
                if not await self._wait(exc, delays, quota_waits, "RFQ write"):
                    return error
                if not over_quota(exc) and await self._saved_already(records, "has_rfq"):
                    logger.info("RFQ write: lines were saved by the failed attempt")
                    return None

    async def _saved_already(self, records: list[dict], check: str = "has_results") -> bool:
        has_rows = getattr(self._sheets, check, None)
        if has_rows is None:
            return False
        try:
            return await asyncio.to_thread(has_rows, records)
        except Exception as exc:
            logger.warning("Sheet write: could not check for saved rows: %r", exc)
            return False

    async def write_rfq(self, rows: list[ResultRow]) -> Optional[str]:
        """The same rows in the RFQ layout; the error when they couldn't be
        written (Results, the tab the run depends on, is already saved), so the
        pipeline can keep them for the next run instead of losing them."""
        append = getattr(self._sheets, "append_rfq", None)
        if not self._rfq or append is None or not rows:
            return None
        records = [r.as_rfq_record() for r in rows]
        # Only a 429 is retried here: it is refused before anything is written,
        # while a dropped call may have been applied (no check on this tab).
        delays, quota_waits = [], [0]
        while True:
            try:
                await asyncio.to_thread(append, records)
                return None
            except Exception as exc:
                error = repr(exc)[:300]
                logger.warning("RFQ tab: could not write %d row(s): %s", len(rows), error)
                if not await self._wait(exc, delays, quota_waits, "RFQ tab write"):
                    return error

    async def _supersede(self, records: list[dict]) -> None:
        """Mark this request's rows from an earlier search. Never fails the write:
        the new rows are already saved."""
        supersede = getattr(self._sheets, "supersede_results", None)
        if supersede is None:
            return
        try:
            marked = await asyncio.to_thread(supersede, records)
            if marked:
                logger.info("Results: marked %d older row(s) of the same request SUPERSEDED",
                            marked)
        except Exception as exc:
            logger.warning("Results: could not mark older rows of the same request: %r", exc)
