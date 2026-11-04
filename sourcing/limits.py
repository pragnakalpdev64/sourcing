"""Per-vendor login and request limits, so a portal never sees us as an attack
and never locks an account (client, 2026-10-21: Jamaica Bearings locks after one
failed login).

The limits for a vendor, later sources winning:
  DEFAULT_LIMITS  ←  the adapter's `default_limits`  ←  the vendor record's `limits`
  (store `vendors/<id>`, set with scripts/vendor_limits.py)  ←  `--set
  VENDOR:limits.KEY=VALUE` for one run.

Counters live in the store so they survive between runs: login failures and
logins per day on the account record, searches per day / last request / back-off
on `vendor_usage/<vendor>`. A vendor with no `limits` in its record runs on the
defaults; the pipeline saves them to the record afterwards, marked unconfirmed,
and asks in Chat for the vendor's real numbers.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_LIMITS: dict[str, Any] = {
    # --- login -----------------------------------------------------------
    # Automatic logins per account per run; a second one only after a failure
    # the portal didn't judge (auth/manager.py). None = MAX_LOGIN_ATTEMPTS (.env).
    "max_login_attempts_per_run": None,
    # Failed logins the vendor allows within the window before it locks the account.
    "vendor_lockout_after": 3,
    "lockout_window_minutes": 1440,
    # Stop this many failures before the vendor's limit: team members using the same
    # login by hand can fail too, and we can't see their attempts.
    "login_safety_margin": 1,
    # No new login for this long after a failed one.
    "login_cooldown_minutes": 60,
    # Logins per account per day, successful or not. Every run signs out at the
    # end (logout_after_run), so this is about one per run with new rows.
    "max_logins_per_day": 10,
    # --- searching --------------------------------------------------------
    # Gap between two searches on this vendor (all its accounts together).
    "min_seconds_between_requests": 5,
    "max_searches_per_run": 50,
    "max_searches_per_day": 200,
    # How a bulk search counts towards the caps: "search" (1) or "parts" (one per part).
    "count_bulk_as": "search",
    # Search only in this window, e.g. "18:00-08:00" (vendor's `timezone`). None = any time.
    "allowed_hours": None,
    # No searches for this long after the portal says we sent too many requests.
    "backoff_minutes": 120,
    # For "per day" and allowed_hours. Skylink's own zone once the client confirms it.
    "timezone": "UTC",
}

_INT_KEYS = {"vendor_lockout_after", "lockout_window_minutes", "login_safety_margin",
             "login_cooldown_minutes", "max_logins_per_day", "max_searches_per_run",
             "max_searches_per_day", "backoff_minutes"}
_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class LimitReached(Exception):
    """A vendor limit stops searching for now; the rows wait for a later run.
    kind ("hours", "backoff", "day_cap", "run_cap") keys the once-a-day Chat message."""

    def __init__(self, reason: str, kind: str = "limit") -> None:
        super().__init__(reason)
        self.kind = kind


def validate(key: str, value: Any) -> Any:
    """One limit value, converted ("5" -> 5). Raises ValueError for an unknown
    key or a bad value, so a typo never silently does nothing."""
    if key not in DEFAULT_LIMITS:
        raise ValueError(f"unknown limit {key!r} (known: {', '.join(DEFAULT_LIMITS)})")
    if key == "max_login_attempts_per_run":
        if value in (None, "", "null"):
            return None
        value = int(value)
        if value < 1:
            raise ValueError("max_login_attempts_per_run must be 1 or more")
        return value
    if key in _INT_KEYS:
        value = int(value)
        if value < 0 or (key == "vendor_lockout_after" and value < 1):
            raise ValueError(f"{key} must be {'1' if key == 'vendor_lockout_after' else '0'} or more")
        return value
    if key == "min_seconds_between_requests":
        value = float(value)
        if value < 0:
            raise ValueError("min_seconds_between_requests must be 0 or more")
        return value
    if key == "count_bulk_as":
        if value not in ("search", "parts"):
            raise ValueError("count_bulk_as must be 'search' or 'parts'")
        return value
    if key == "allowed_hours":
        if value in (None, "", "null", "any"):
            return None
        if not _HOURS.match(str(value)):
            raise ValueError("allowed_hours must look like 18:00-08:00")
        return str(value)
    if key == "timezone":
        try:
            ZoneInfo(str(value))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return str(value)
    return value


def effective_limits(config: dict, adapter_defaults: Optional[dict] = None
                     ) -> tuple[dict, bool]:
    """(limits, set_for_vendor): the defaults with the adapter's, the vendor
    record's and this run's values on top. set_for_vendor is False when the
    vendor record has no `limits`. Bad stored values are skipped (the default
    stays) rather than failing the run."""
    limits = dict(DEFAULT_LIMITS)
    stored = config.get("limits")
    layers = [adapter_defaults or {}, stored if isinstance(stored, dict) else {},
              {k[len("limits."):]: v for k, v in config.items() if k.startswith("limits.")}]
    for layer in layers:
        for key, value in layer.items():
            try:
                limits[key] = validate(key, value)
            except (ValueError, TypeError):
                pass
    return limits, isinstance(stored, dict)


def _zone(limits: dict) -> ZoneInfo:
    try:
        return ZoneInfo(limits.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def day_key(now: float, limits: dict) -> str:
    """The vendor's calendar day for `now`, e.g. "2026-10-21"."""
    return datetime.fromtimestamp(now, _zone(limits)).strftime("%Y-%m-%d")


def in_allowed_hours(now: float, limits: dict) -> bool:
    spec = limits.get("allowed_hours")
    if not spec:
        return True
    start, end = spec.split("-")
    local = datetime.fromtimestamp(now, _zone(limits)).strftime("%H:%M")
    if start <= end:
        return start <= local < end
    return local >= start or local < end      # over midnight, e.g. 18:00-08:00


# --- login policy (used by auth/manager.py) ---------------------------------

def login_block(doc: dict, limits: dict, now: float,
                retry_in_run: bool = False) -> tuple[Optional[str], bool]:
    """(reason, needs_person) when another login must not be tried now, else
    (None, False). needs_person: the failure budget is used up, so a person
    must check the account; otherwise it's a wait (cooldown, daily cap).
    retry_in_run: the one immediate retry after a failure the portal didn't
    judge (auth/manager.py); the cooldown is for the next runs, not for it."""
    window = limits["lockout_window_minutes"] * 60
    failures = [f for f in doc.get("login_failures") or []
                if now - float(f.get("at") or 0) < window]
    allowed = max(1, limits["vendor_lockout_after"] - limits["login_safety_margin"])
    if len(failures) >= allowed:
        return (f"{len(failures)} failed login(s) in the last "
                f"{limits['lockout_window_minutes'] // 60}h; the vendor locks after "
                f"{limits['vendor_lockout_after']}, so no more attempts until a person checks "
                "the account", True)
    last = float(doc.get("last_failure_at") or 0)
    cooldown = limits["login_cooldown_minutes"] * 60
    if last and now - last < cooldown and not retry_in_run:
        wait = int((cooldown - (now - last)) // 60) + 1
        return (f"last login failed {int((now - last) // 60)} min ago; next attempt allowed "
                f"in {wait} min", False)
    if doc.get("logins_day") == day_key(now, limits) and \
            int(doc.get("logins_today") or 0) >= limits["max_logins_per_day"]:
        return (f"daily login limit reached ({limits['max_logins_per_day']}); continues "
                "tomorrow", False)
    return None, False


def login_attempt_fields(doc: dict, limits: dict, now: float) -> dict:
    """Account fields after counting one more login attempt today."""
    day = day_key(now, limits)
    count = int(doc.get("logins_today") or 0) if doc.get("logins_day") == day else 0
    return {"logins_day": day, "logins_today": count + 1}


def login_failure_fields(doc: dict, limits: dict, now: float, reason: str) -> dict:
    """Account fields after a failed login the vendor saw (credentials sent)."""
    window = limits["lockout_window_minutes"] * 60
    kept = [f for f in doc.get("login_failures") or [] if now - float(f.get("at") or 0) < window]
    return {"login_failures": kept + [{"at": now, "reason": reason[:200]}],
            "last_failure_at": now}


LOGIN_OK_FIELDS = {"login_failures": [], "last_failure_at": None}


# --- requests (used by pipeline.py) ------------------------------------------

class VendorLimiter:
    """Search gap and caps per vendor for one run. The accounts of a vendor run
    in parallel, so they share one lock: the gap and the counts cover the vendor,
    not each login. Counts are saved to the store (`vendor_usage`)."""

    def __init__(self, store, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        self.store = store
        self.clock = clock
        self.sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._run_counts: dict[str, int] = {}

    def _usage(self, vendor_id: str, limits: dict, now: float) -> dict:
        usage = self.store.get_vendor_usage(vendor_id) or {}
        if usage.get("day") != day_key(now, limits):
            usage = {**usage, "day": day_key(now, limits), "searches_today": 0}
        return usage

    def block(self, vendor_id: str, limits: dict, needed: int = 1) -> Optional[LimitReached]:
        """Why this vendor can't search now (or can't fit `needed` more), else None."""
        now = self.clock()
        if not in_allowed_hours(now, limits):
            return LimitReached(f"outside the allowed search hours ({limits['allowed_hours']} "
                                f"{limits['timezone']})", "hours")
        usage = self._usage(vendor_id, limits, now)
        until = float(usage.get("backoff_until") or 0)
        if until > now:
            return LimitReached(f"the portal asked us to slow down; paused until "
                                f"{datetime.fromtimestamp(until, _zone(limits)):%H:%M} "
                                f"{limits['timezone']}", "backoff")
        today = int(usage.get("searches_today") or 0)
        if today + needed > limits["max_searches_per_day"]:
            return LimitReached(f"daily search limit reached ({today}/"
                                f"{limits['max_searches_per_day']}); continues tomorrow", "day_cap")
        done = self._run_counts.get(vendor_id, 0)
        if done + needed > limits["max_searches_per_run"]:
            return LimitReached(f"search limit for one run reached ({done}/"
                                f"{limits['max_searches_per_run']}); continues in the next run",
                                "run_cap")
        return None

    def block_reason(self, vendor_id: str, limits: dict, needed: int = 1) -> Optional[str]:
        stop = self.block(vendor_id, limits, needed)
        return str(stop) if stop else None

    async def before_request(self, vendor_id: str, limits: dict, parts: int = 1) -> None:
        """Wait for the vendor's gap, then count one search (bulk: per
        `count_bulk_as`). Raises LimitReached instead of searching over a limit."""
        needed = parts if limits["count_bulk_as"] == "parts" else 1
        lock = self._locks.setdefault(vendor_id, asyncio.Lock())
        async with lock:
            stop = self.block(vendor_id, limits, needed)
            if stop:
                raise stop
            usage = self._usage(vendor_id, limits, self.clock())
            wait = float(usage.get("last_request_at") or 0) + \
                limits["min_seconds_between_requests"] - self.clock()
            if wait > 0:
                await self.sleep(wait)
            now = self.clock()
            usage = self._usage(vendor_id, limits, now)
            self.store.update_vendor_usage(vendor_id, {
                "day": usage["day"], "searches_today": int(usage.get("searches_today") or 0) + needed,
                "last_request_at": now})
            self._run_counts[vendor_id] = self._run_counts.get(vendor_id, 0) + needed

    def back_off(self, vendor_id: str, limits: dict) -> None:
        """The portal said too many requests: no searches on it for backoff_minutes."""
        self.store.update_vendor_usage(
            vendor_id, {"backoff_until": self.clock() + limits["backoff_minutes"] * 60})

    def searches_today(self, vendor_id: str, limits: dict) -> int:
        return int(self._usage(vendor_id, limits, self.clock()).get("searches_today") or 0)

    def should_notify(self, vendor_id: str, limits: dict, kind: str) -> bool:
        """True once per vendor, kind and day: a limit message isn't repeated by
        every 15-minute sweep."""
        day = day_key(self.clock(), limits)
        usage = self.store.get_vendor_usage(vendor_id) or {}
        key = f"notified_{kind}"
        if usage.get(key) == day:
            return False
        self.store.update_vendor_usage(vendor_id, {key: day})
        return True
