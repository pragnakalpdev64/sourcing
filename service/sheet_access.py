"""Which sheet a caller may name in /trigger, /sheet/pull and /sheet/ack.

1. The id's format, always: a Google Sheet id is letters, digits, "-" and "_".
   Anything else (a number, a path, "x.xlsx") is refused with 400 before it
   reaches the store or the worker, which would open a ".xlsx" id as a local file.
2. With SHEET_ACCESS_CHECK on: the caller must be able to open the sheet. The
   service can't open it (Skylink's org blocks the service account), so it asks
   Google with the caller's own add-on token (scope `spreadsheets`). Without this,
   any Skylink account that knows a sheet's id could send rows into its copy
   (the real add-on would then write their results into the sheet), or read and
   clear its waiting results. The trigger secret (Cloud Scheduler) names no sheet.
   Google's answer is kept a few minutes per person and sheet: the sidebar pulls
   every 15 s.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx
from fastapi import HTTPException

logger = logging.getLogger("sourcing.sheet_access")

SHEET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
ALLOWED_SECONDS = 600      # "may open" is kept this long
REFUSED_SECONDS = 60       # "may not" is asked again sooner (access may just have been given)

_cache: dict[tuple[str, str], tuple[bool, float]] = {}
_cache_lock = threading.Lock()


def checked_sheet_id(value) -> str:
    """The sheet id if it has a Google Sheet id's form, else 400."""
    if not isinstance(value, str) or not SHEET_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="sheet_id is not a Google Sheet id")
    return value


async def _google_says(token: str, sheet_id: str) -> bool:
    """True / False from Google; an answer that is neither is a 503 (never let through)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(SHEETS_API.format(sheet_id=sheet_id),
                                    params={"fields": "spreadsheetId"},
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="could not ask Google about the sheet") from exc
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 403:
        text = resp.text
        if "SERVICE_DISABLED" in text or "accessNotConfigured" in text:
            # Our setup, not the person: say so instead of "no access".
            logger.error("Sheet access check: the Sheets API is off in the add-on's "
                         "Google Cloud project (%s)", text[:300])
            raise HTTPException(status_code=503, detail=(
                "sheet access check: the Sheets API is not on in the add-on's Google "
                "Cloud project"))
        return False
    logger.warning("Sheet access check: Google answered %s for sheet %s", resp.status_code,
                   sheet_id)
    raise HTTPException(status_code=503, detail="could not ask Google about the sheet")


async def require_sheet_access(caller: str, token: str, sheet_id: str) -> None:
    """403 unless `caller` (an add-on user's email) can open `sheet_id`."""
    if caller == "secret" or not token:
        raise HTTPException(status_code=403,
                            detail="only the add-on, signed in as a person, may name a sheet")
    key = (caller, sheet_id)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[1] > now:
        allowed = hit[0]
    else:
        allowed = await _google_says(token, sheet_id)
        with _cache_lock:
            _cache[key] = (allowed, now + (ALLOWED_SECONDS if allowed else REFUSED_SECONDS))
    if not allowed:
        logger.warning("%s may not open sheet %s: refused", caller, sheet_id)
        raise HTTPException(status_code=403, detail="your Google account can't open this sheet")


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
