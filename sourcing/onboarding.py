"""Onboarding run for a vendor (docs/GENERIC_VENDOR_PLAN.md, phase 5).

Logs in with the vendor's login secret, checks the sign-in, searches 1-3 parts a
team member knows, logs out, and reports what was found and how. It writes nothing to
the sheet and saves nothing unless asked: a person compares the offers with the
portal before the vendor is enabled. scripts/onboard_vendor.py is the command.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from .auth.hitl import (CodeNotGiven, hold_until_logged_out, login_with_handoff,
                         logout_with_handoff)
from .models import LlmUsage, RequestRow
from .vendors.base import (
    InvalidCredentials, LoginBlocked, LoginFailed, MfaRequired, RateLimited, SessionExpired,
    VendorAdapter, may_be_signed_in,
)

logger = logging.getLogger("sourcing.onboarding")

# Few searches on a live portal (client rule): an onboarding run checks the
# login and the results reading, it isn't a sourcing run.
MAX_PARTS = 3

# Why a run stopped, in the words the report uses.
STOP_REASONS = {
    LoginBlocked: "blocked (lockout / bot check): stop and tell the client",
    CodeNotGiven: "2FA: nobody finished the login (Live View) or sent an accepted code in time",
    MfaRequired: "2FA the code can't answer: needs a person",
    InvalidCredentials: "the portal rejected the username or password",
    LoginFailed: "the login could not be completed",
    SessionExpired: "signed out during the run",
    RateLimited: "the portal says we sent too many requests: no more searches for now",
}


@dataclass
class PartResult:
    part_number: str
    status: str                  # found / no results / not sold / error
    seconds: float
    offers: list = field(default_factory=list)
    note: str = ""
    page_url: str = ""


@dataclass
class OnboardingReport:
    vendor_id: str
    adapter: str
    signed_in: bool = False
    login_seconds: float = 0.0
    signed_in_check: Optional[bool] = None
    parts: list = field(default_factory=list)
    logged_out: Optional[bool] = None
    stopped: str = ""            # empty = the run finished
    login_only: bool = False     # no parts: only the login (and its 2FA) was checked
    learned: dict = field(default_factory=dict)      # confirmed: what --save saves
    not_confirmed: list = field(default_factory=list)  # learned, but nothing proved them
    found_by: dict = field(default_factory=dict)
    llm_discovery_calls: int = 0
    llm: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Signed in and (unless the run only checked the login) found offers."""
        return (not self.stopped and self.signed_in and bool(self.signed_in_check)
                and (self.login_only or any(p.status == "found" for p in self.parts)))

    @property
    def to_check(self) -> list[str]:
        """What a person should look at before enabling the vendor (seen in run 11:
        a not-sold page read as "found" until not_sold_phrases was set)."""
        notes = []
        for p in self.parts:
            fields = ("price", "qty_available", "condition", "lead_time")
            if p.status == "found" and p.offers and all(
                    not any(o.get(k) for k in fields) for o in p.offers):
                notes.append(f"{p.part_number}: found, but every line is empty. If the portal "
                             "says it doesn't sell this part, add its wording to the record: "
                             "manage_vendors.py set <vendor> not_sold_phrases='[\"...\"]'")
            stocked = [o for o in p.offers if o.get("price")]
            if stocked and not any(o.get("condition") for o in stocked):
                notes.append(f"{p.part_number}: in-stock lines have no condition. If the portal "
                             "never shows one and sells one kind only, set default_condition "
                             "on the record (after the client confirms)")
        llm_found = sorted(k for k, v in self.found_by.items() if v == "LLM")
        if llm_found:
            notes.append("found by the LLM (check them, then keep them with --save): "
                         + ", ".join(llm_found))
        if self.parts and "part_url_template" not in self.learned:
            notes.append("no direct results address learned: every search goes through "
                         "the search box (slower)")
        return notes

    def as_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok, "to_check": self.to_check}


def _offer_dict(offer) -> dict:
    return {k: getattr(offer, k, None) for k in
            ("part_number", "price", "currency", "qty_available", "condition", "lead_time")}


async def onboard(vendor_id: str, adapter: VendorAdapter, cred, browser,
                  parts: list[str], *, handoff=None, people: tuple = ()) -> OnboardingReport:
    """One onboarding run on a fresh browser context. Never raises for a portal
    problem: the reason is in `report.stopped`. No parts = check the login only.
    handoff: asks `people` over Chat to finish a 2FA step (auth/hitl.py)."""
    if len(parts) > MAX_PARTS:
        raise ValueError(f"Give at most {MAX_PARTS} part numbers a team member knows")
    report = OnboardingReport(vendor_id, type(adapter).__name__, login_only=not parts)
    started = time.time()
    usage = LlmUsage()
    handle = None
    maybe_signed_in = False   # login failed, but the portal may have signed us in
    at_2fa = False            # stopped on a 2FA step: the portal hasn't signed us in yet
    try:
        handle = await browser.open(await browser.create_context(),
                                    **getattr(adapter, "browser_options", {}))
        page = handle.page

        t = time.time()
        session_id = handle.session_id
        try:
            await login_with_handoff(adapter, page, cred, handoff,
                                     account=f"onboarding:{vendor_id}", people=people,
                                     live_view_url=lambda: browser.live_view_url(session_id))
        except BaseException as exc:
            maybe_signed_in = may_be_signed_in(exc)
            at_2fa = isinstance(exc, MfaRequired)
            raise
        report.login_seconds = round(time.time() - t, 1)
        report.signed_in = True
        # A login that already saw the proof (a logout link on the vendor's page)
        # isn't checked a second time, as in the worker (auth/manager.py); the
        # check cost ~20s on Wencor (2026-11-03).
        proof = getattr(adapter, "signed_in_proof", None)
        if proof:
            logger.info("Sign-in seen by the login (%s); no separate check", proof)
        report.signed_in_check = bool(proof) or await adapter.is_authenticated(page)
        if not report.signed_in_check:
            report.stopped = "logged in, but the sign-in check on the next page failed"
            return report

        for i, pn in enumerate(parts):
            row = RequestRow(sheet_row=i + 2, rfq_id="ONBOARDING", vendor_raw=report.vendor_id,
                             part_number=pn, qty_needed="1")
            t = time.time()
            try:
                outcome = await adapter.search(page, row)
            except (SessionExpired, RateLimited):
                raise
            except Exception as exc:
                report.parts.append(PartResult(pn, "error", round(time.time() - t, 1),
                                               note=f"{type(exc).__name__}: {exc}"[:300]))
                continue
            usage.add(outcome.usage)
            status = ("not sold" if outcome.not_sold else
                      "found" if outcome.found else "no results")
            report.parts.append(PartResult(
                pn, status, round(time.time() - t, 1),
                offers=[_offer_dict(o) for o in outcome.offers],
                note=outcome.not_sold or "", page_url=outcome.page_url or ""))
    except tuple(STOP_REASONS) as exc:
        reason = next(text for cls, text in STOP_REASONS.items() if isinstance(exc, cls)
                      and not (cls is CodeNotGiven and not exc.asked))
        report.stopped = f"{reason} ({exc})"
        logger.warning("Onboarding stopped: %s", report.stopped)
    except Exception as exc:
        report.stopped = f"unexpected error: {type(exc).__name__}: {exc}"[:400]
        logger.exception("Onboarding stopped")
    finally:
        if handle is not None:
            # Also after a login that failed but may have gone through: the portal
            # may allow only a few sessions at once.
            # Two tries, then a person through Live View, as after a run: a
            # session left open (e.g. after a timeout) blocks the team's sign-in.
            if report.signed_in or maybe_signed_in:
                session = handle.session_id
                try:
                    report.logged_out = await logout_with_handoff(
                        adapter, handle.page, handoff, account=f"onboarding:{vendor_id}",
                        people=people, live_view_url=lambda: browser.live_view_url(session))
                except Exception as exc:
                    logger.warning("Logout failed: %r", exc)
                    report.logged_out = False
                if not report.logged_out and (report.signed_in or not at_2fa):
                    report.logged_out = await hold_until_logged_out(
                        f"onboarding:{vendor_id}", adapter, handle, browser)
            await browser.close(handle)
        usage.add(adapter.usage)      # discovery calls not yet charged to a part
        report.llm = usage.as_dict()
        report.llm_discovery_calls = getattr(adapter, "discovery_calls", 0)
        report.learned = adapter.confirmed_learned()
        report.not_confirmed = sorted(set(adapter.learned) - set(report.learned))
        report.found_by = dict(getattr(adapter, "found_by", {}))
        report.seconds = round(time.time() - started, 1)
    return report


def report_text(report: OnboardingReport) -> str:
    """The report as plain lines for the terminal (and the run's log)."""
    lines = [f"Onboarding {report.vendor_id} ({report.adapter}): "
             + ("OK" if report.ok else "NOT OK"),
             f"  login: {'signed in' if report.signed_in else 'not signed in'}"
             + (f" in {report.login_seconds}s" if report.signed_in else ""),
             f"  sign-in check on the next page: {report.signed_in_check}"]
    for p in report.parts:
        lines.append(f"  part {p.part_number}: {p.status}, {len(p.offers)} offer(s), {p.seconds}s"
                     + (f" ({p.note})" if p.note else ""))
        for o in p.offers:
            lines.append(f"      {o.get('part_number')}  price={o.get('price')} "
                         f"{o.get('currency') or ''}  qty={o.get('qty_available')}  "
                         f"cond={o.get('condition')}  lead={o.get('lead_time')}")
    lines.append(f"  logout: {report.logged_out}")
    if report.stopped:
        lines.append(f"  STOPPED: {report.stopped}")
    lines.append("  found by: " + (", ".join(f"{k}={v}" for k, v in sorted(report.found_by.items()))
                                   or "-"))
    lines.append("  learned selectors (confirmed; --save saves these):")
    for k, v in sorted(report.learned.items()):
        lines.append(f"      {k}: {v}")
    if report.not_confirmed:
        lines.append("  not saved (no sign-in or search confirmed them): "
                     + ", ".join(report.not_confirmed))
    if report.to_check:
        lines.append("  TO CHECK:")
        lines += [f"    - {note}" for note in report.to_check]
    lines.append(f"  LLM: {report.llm.get('calls', 0)} call(s) "
                 f"({report.llm_discovery_calls} to find elements), "
                 f"${report.llm.get('cost_usd', 0):.4f}; whole run {report.seconds}s")
    return "\n".join(lines)
