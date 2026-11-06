"""IncoraAdapter: Incora's shop (shop.incora.com, Wesco Aircraft dba Incora).

From the team's screenshots (2026-11-05) and the site's own script bundle (an
Angular app; read 2026-11-05, nothing signed in):

  1 sign in   the login address is the secret's `url` (team: https://shop.incora.com/).
              The app sends the browser to Incora's Azure AD B2C page
              (login.incora.com, "Returning Customer Sign In": Email Address,
              Password, SIGN IN), then back to /home (Shop, Flash Sale, Auctions,
              Bulk Search, Order History, View Invoices). Its OAuth tokens are kept in
              the browser's localStorage (`access_token`, `expires_at`): signed in =
              a token that hasn't expired, read on one of Incora's own pages
  2 search    the Bulk Search dialog ("Format: Product Code[Tab]Quantity", at most
              500 lines) saves the parts in localStorage
              `ecom.products.bulk.search.search.code` as [{code, qty}] and opens
              /shop/products/bulk-search. The program writes that list itself and
              opens the address: same search, no dialog (a direct address, as the
              checklist asks). One part is a bulk search of one
  3 read      the results page saves its whole result in localStorage
              `ecom.products.bulk.search`: every part with every stock line (Site,
              Manufacturer, Certification Description, Revision, Shelf Life, UOM, Qty
              Available), whatever page of the table is shown. That is read, so no
              row is missed on a second or third page; the table's own "1 - 50 of N"
              is checked against it. Without it, every page of the table is read as
              text (the paginator's Next page button until it is disabled)
  4 prices    the stock lines show no price: their $ button opens a pop-up with the
              line's price breaks (Qty / Price, e.g. 1: 25.0000 USD, 25: 1.0000 USD),
              which the app loads per line from
              app.hw2.shop.incora.com/api/Customer/<customer>/stocks/<itemSpecKey>.
              The program asks that same address for each stock line (the customer
              and the headers from the page's own bulk request), 4 at a time
  5 LLM       the records, with Incora's prompt (incora_prompt.py); then the values
              the program read itself are set on each line (_finish)

Never clicked: Add To Cart, Request A Quote, the order quantity boxes. Logout =
what the app's "Log out" does: its tokens removed, then B2C's end-session address
back to /logged-out. A run with --dump-pages saves the text read and the raw data
(debug_pages/*-incora-*). Not yet tried on the live portal (2026-11-05).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote, urlsplit

from ..conditions import CONDITION_CODES
from ..extract import format_warehouses
from ..models import RequestRow, SearchOutcome
from . import web
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, InvalidCredentials, SessionExpired,
    check_rate_limited, has_real_offer, host_allowed,
)
from .generic import GenericAdapter
from .incora_prompt import INCORA_EXTRACT_SYSTEM
from .web import _settle

logger = logging.getLogger("sourcing.vendors.incora")

SHOP = "https://shop.incora.com"
SHOP_HOST = "shop.incora.com"
LOGIN_HOST = "login.incora.com"
HOME_URL = f"{SHOP}/home"
BULK_URL = f"{SHOP}/shop/products/bulk-search"
LOGGED_OUT_URL = f"{SHOP}/logged-out"
DETAIL_PAGE = SHOP + "/shop/products/detail/{item}/{spec}"
# The app's settings (main.js, 2026-11-05): portalsCustomerRootUrl + stocks.
API_ROOT = "https://app.hw2.shop.incora.com/api/Customer"
BULK_API = f"{API_ROOT}/stocks/bulk"
DETAIL_API = API_ROOT + "/{customer}/stocks/{spec}"
# B2C's end_session_endpoint (its openid-configuration, 2026-11-05).
END_SESSION_URL = ("https://login.incora.com/a42f291c-cd14-4967-9b76-39b8737608f1/"
                   "b2c_1a_signup_signin_oauth_30m/oauth2/v2.0/logout")
# Path words after /api/Customer/ that are endpoints, not a customer key (main.js).
_API_WORDS = {"carriers", "new", "existing", "carts", "usercustomers", "customer",
              "documents", "invoices", "lines", "manufacturers", "orders", "payments",
              "rfq", "stocks"}
SEARCH_CODES_KEY = "ecom.products.bulk.search.search.code"
RESULTS_KEY = "ecom.products.bulk.search"
# What the app's logOut() removes from its storage (angular-oauth2-oidc).
TOKEN_KEYS = ("access_token", "id_token", "refresh_token", "nonce", "PKCE_verifier",
              "expires_at", "id_token_claims_obj", "id_token_expires_at",
              "id_token_stored_at", "access_token_stored_at", "granted_scopes",
              "session_state")
# B2C's sign-in form: "Email Address" is #signInName or #email depending on the
# policy page (the screenshot shows the label only), SIGN IN is button#next.
# setdefault: a selector saved from an earlier run wins.
KNOWN_SELECTORS = {"username_input": "input#signInName, input#email",
                   "password_input": "input#password",
                   "submit_button": "button#next"}
# B2C's own wording for an unknown email (not in web.BAD_PASSWORD_HINTS).
BAD_LOGIN_HINTS = ("can't seem to find your account", "cannot find your account",
                   "your password is incorrect", "invalid username or password")

# The dialog's own limit ("Maximum 500 lines"); 25 per search like the other bulk
# vendors, so one LLM answer stays small.
MAX_LINES = 500
BULK_BATCH = 25
RESULTS_WAIT_SECONDS = 90
SIGN_IN_WAIT_SECONDS = 30
# A token with less than this left is refreshed (by the app) before the next page.
REFRESH_AHEAD_MS = 90_000
REFRESH_WAIT_SECONDS = 75
PRICE_FETCHES_AT_ONCE = 4
# Stock lines per LLM call; the calls run at the same time (DASI: 63 lots in one
# call ran past the output limit, run 12).
LINES_PER_CALL = 20
# Incora shows no condition (new hardware); the client's RFQ rule quotes no
# in-stock line without one (Wencor, Monroe, Aircraft Spruce): the same default.
CONDITION_NOT_SHOWN = "NE"

RECORDS_NOTE = ("--- Incora bulk search results, read by the program from the page's own "
                "data: every part and every stock line of the results table (all of its "
                "pages), with each stock line's price breaks from its $ pop-up ---")
TABLE_NOTE = ("--- Incora bulk search results table, page {n} of the table (the page's "
              "stored data couldn't be read; no price breaks) ---")

# Location fields by the column names the page shows (the app's table settings,
# main.js 2026-11-05: site, manufacturerName, certificateDescription, revision,
# shelfLifeDays, unitOfMeasure, quantityAvailable).
LOCATION_FIELDS = (("site", "Site"), ("manufacturerName", "Manufacturer"),
                   ("certificateDescription", "Certification Description"),
                   ("revision", "Revision"), ("shelfLifeDays", "Shelf Life (days)"),
                   ("unitOfMeasure", "UOM"), ("quantityAvailable", "Qty Available"))
# Fields seen in the stored data (run 2026-11-05 16:39) that the records leave out on
# purpose: keys, codes and the app's own row state.
_KNOWN_ITEM_KEYS = {"itemCode", "itemDescription", "searchQuantity", "stockFound",
                    "locations", "hideExpandedAction", "isExpanded", "selectedCount",
                    "orderQuantity", "rowStyles", "itemShortCode"}
_KNOWN_LOCATION_KEYS = {k for k, _ in LOCATION_FIELDS} | {
    "itemSpecKey", "itemKey", "isSelected", "orderQuantity", "isUserInput", "error",
    "errorMessage", "price", "orderQty", "uniqueIdentifier", "itemCode", "itemCodeShort",
    "itemDescription", "shipToAccountKey", "branchCode", "manufacturerAccountKey",
    "certificationKey", "pricingUnitOfMeasure", "itemType",
    "_breaks", "_breaks_error", "_description"}

SESSION_JS = """() => ({token: !!localStorage.getItem('access_token'),
  expires: Number(localStorage.getItem('expires_at') || 0), now: Date.now(),
  path: location.pathname})"""
START_SEARCH_JS = """([codesKey, resultsKey, codes]) => {
  localStorage.removeItem(resultsKey);
  localStorage.setItem(codesKey, JSON.stringify(codes));
  return true;
}"""
RESULTS_JS = """(key) => { const raw = localStorage.getItem(key);
  if (!raw) return null; try { return JSON.parse(raw); } catch (e) { return null; } }"""
FETCH_JS = """async ([url, headers]) => {
  try { const r = await fetch(url, {headers: Object.assign({Accept: 'application/json'}, headers)});
    let body = null; try { body = await r.json(); } catch (e) {}
    return {status: r.status, body};
  } catch (e) { return {status: 0, error: String(e)}; } }"""
LOGOUT_JS = """([keys, endSession, back]) => {
  const idToken = localStorage.getItem('id_token') || '';
  keys.forEach(k => { localStorage.removeItem(k); sessionStorage.removeItem(k); });
  const url = endSession + '?post_logout_redirect_uri=' + encodeURIComponent(back)
    + (idToken ? '&id_token_hint=' + encodeURIComponent(idToken) : '');
  location.href = url;
  return !!idToken;
}"""
# "1 – 50 of 63": the paginator's own count of summary rows.
PAGINATOR_JS = """() => { const el = document.querySelector(
  '.mat-mdc-paginator-range-label, .mat-paginator-range-label');
  return el ? el.textContent.replace(/\\s+/g, ' ').trim() : ''; }"""
NEXT_PAGE = ("button.mat-mdc-paginator-navigation-next, button.mat-paginator-navigation-next, "
             "button[aria-label='Next page']")
TABLE_JS = """() => { const t = document.querySelector('mf-table, table, [role=table]');
  return t ? t.innerText : ''; }"""
# The results page's controls (main.js 2026-11-05: Font Awesome icons drawn as
# <svg data-icon=...>; tooltips "Stock Found" / "Stock Not Found"; the price
# pop-up component's own class). Table: 10 parts per page by default.
PRICE_BUTTON = "button:has(svg[data-icon='dollar-sign'])"
PRICE_POPUP = ".ecom-price-overlay"
FOUND_ICON = "svg[data-icon='check']"
NOT_FOUND_ICON = "svg[data-icon='xmark']"
TOOLTIP = ".mat-mdc-tooltip, .mat-tooltip, .mdc-tooltip, [role='tooltip']"
ROW_OF_ICON = "xpath=ancestor::*[self::tr or @role='row' or contains(@class, 'row')][1]"
TABLE_PAGE_SIZE = 10
OPEN_WAIT_MS = 6000
# The stock lines' own table: the nearest table around a $ button.
NESTED_TEXT_JS = """b => { const t = b.closest('mf-table') || b.closest('table');
  return t ? t.innerText.replace(/[ \\t]+/g, ' ') : ''; }"""
# A pop-up row: "1  25.0000 USD".
_POPUP_ROW = re.compile(r"(?m)^\s*([\d,]+)\s+([\d,]*\.\d+)\s*([A-Z]{3})?\s*$")


def _key(value) -> str:
    """A part number without spaces, dashes, slashes or dots, upper case."""
    return re.sub(r"[\s\-/.]", "", str(value or "").upper())


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _number(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def price_text(value) -> str:
    """A price as the pop-up shows it: 4 decimals ("25.0000")."""
    n = _number(value)
    return f"{n:.4f}" if n is not None else _text(value)


def qty_text(value) -> str:
    n = _number(value)
    if n is None:
        return _text(value)
    return f"{n:g}" if not n.is_integer() else str(int(n))


def search_codes(rows: list[RequestRow]) -> list[dict]:
    """What the Bulk Search dialog saves for its lines "code[Tab]qty": the quantity
    only when it is a whole number (the dialog refuses anything else)."""
    codes = []
    for r in rows:
        code = " ".join(r.part_number.split())
        qty = re.fullmatch(r"\s*(\d+)(?:\.0+)?\s*", r.qty_needed or "")
        codes.append({"code": code, "qty": int(qty.group(1))} if qty else {"code": code})
    return codes


def break_rows(price_breaks: list[dict]) -> list[tuple[str, str, str]]:
    """The pop-up's rows (Qty / Price: the quantity each price starts at) as
    (from, to, price): a row runs up to the next row's quantity less one, the last
    has no end. 1 / 25 / 50 -> 1-24, 25-49, 50+. Read this way the pipeline prices
    30 pieces at the 25 row, as the site does."""
    rows = sorted(((_number(b.get("quantity")), b.get("price")) for b in price_breaks or []
                   if _number(b.get("quantity")) is not None and _number(b.get("price")) is not None),
                  key=lambda r: r[0])
    out = []
    for i, (qty, price) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else None
        high = qty_text(nxt - 1) if nxt is not None and nxt - 1 >= qty else ""
        out.append((qty_text(qty), high, price_text(price)))
    return out


# Incora's certification wordings (run 2026-11-05) -> the Trace column, most
# important first; each kept short so the 50-character rule (prompt 3.11) leaves
# the manufacturer's C of C and DFARS in.
_CERT_PARTS = (
    (re.compile(r"\b(mfg'?s|mfr'?s|manufacturers?'?)\s+c\s*of\s*c\b", re.I), "MFR C of C"),
    (re.compile(r"\bdfars\b", re.I), "DFARS"),
    (re.compile(r"\btest\s+reports?\b", re.I), "Test Report"),
    (re.compile(r"\b(separate\s+)?raw\s+material(\s+certs?)?\b", re.I), "Raw Material Certs"),
    (re.compile(r"\bprocess\s+certs?\b", re.I), "Process Certs"),
)
TRACE_MAX = 50


def cert_trace(description) -> Optional[str]:
    """Certification Description -> Trace, in the page's order: "Mfg's C of C, Test
    Report, Process Certs & Separate Raw Material" -> "MFR C of C, Test Report,
    Process Certs, Raw Material Certs" is 52, so the least important part goes:
    "MFR C of C, Test Report, Raw Material Certs". A wording none of the patterns
    knows is kept as written (cut to whole items when too long). Set by the
    program: the LLM's shortened wording ("Test Rpt") failed the page check and
    every Trace but one was emptied (run 2026-11-05 17:18)."""
    text = " ".join(str(description or "").split())
    if not text:
        return None
    found = []                                    # (position, priority, wording)
    rest = text
    for prio, (pattern, short) in enumerate(_CERT_PARTS):
        m = pattern.search(rest)
        if m:
            found.append((m.start(), prio, short))
            rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
    leftovers = [w.strip() for w in re.split(r"[,&]", rest) if w.strip()]
    if not found:
        items, prios = [w.strip() for w in re.split(r",", text) if w.strip()], None
    else:
        found.sort()
        items = [w for _, _, w in found] + leftovers
        prios = [p for _, p, _ in found] + [len(_CERT_PARTS)] * len(leftovers)
    while len(", ".join(items)) > TRACE_MAX and len(items) > 1:
        drop = (max(range(len(items)), key=lambda i: (prios[i], i)) if prios
                else len(items) - 1)
        items.pop(drop)
        if prios:
            prios.pop(drop)
    return ", ".join(items)[:TRACE_MAX] or None


def cheaper_break_note(breaks: list[tuple[str, str, str]], qty_needed) -> Optional[str]:
    """Incora's first row is often a small-quantity price (1-24 at $25.0000, 25+ at
    $1.0000): 10 asked cost more than 25. The Unit Price stays Incora's price for
    the quantity asked (team, 2026-11-05); this note names the cheapest larger
    break whose whole cost is lower, for the team member to decide:
    "25+ AT $1.0000 EACH: $25.00 FOR 25, LESS THAN $250.00 FOR 10"."""
    need = _number(qty_needed) or 1
    rows = [(_number(lo), _number(hi), _number(p), lo, p) for lo, hi, p in breaks or []]
    here = next((r for r in rows if r[0] is not None and r[2] is not None and r[0] <= need
                 and (r[1] is None or need <= r[1])), None)
    if here is None:
        return None
    cost = need * here[2]
    better = [(r[0] * r[2], r) for r in rows if r[0] and r[2] is not None and r[0] > need
              and r[0] * r[2] < cost]
    if not better:
        return None
    total, r = min(better)
    return (f"{r[3]}+ AT ${r[4]} EACH: ${total:,.2f} FOR {qty_text(r[0])}, LESS THAN "
            f"${cost:,.2f} FOR {qty_text(need)}")


def detail_link(location: dict) -> str:
    """The stock line's own product page (the app's route detail/:itemKey/:itemSpecKey,
    which its stock tables open on a row click); the results page when the line has
    no keys."""
    item, spec = location.get("itemKey"), location.get("itemSpecKey")
    if item and spec:
        return DETAIL_PAGE.format(item=quote(str(item), safe=""), spec=quote(str(spec), safe=""))
    return BULK_URL


class IncoraAdapter(GenericAdapter):
    vendor_id = "incora"
    display_name = "Incora"
    allowed_hosts = ("incora.com",)
    # The team's login address (2026-11-05); the secret's `url` wins.
    default_login_url = f"{SHOP}/"
    extract_system = INCORA_EXTRACT_SYSTEM
    max_parts_per_search = BULK_BATCH
    default_bulk_batch_size = BULK_BATCH
    default_bulk_min_parts = 2
    default_show_substitutes = False
    # Client, 2026-11-06: "they typically take 7 days to ship, so Delivery by default
    # should always = 7 (unless it's a No Quote)".
    default_delivery_days = "7"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        for key, selector in KNOWN_SELECTORS.items():
            self.selectors.setdefault(key, selector)
        self.bulk_unavailable: Optional[str] = None
        # Per stock line link: what the program read for it (_finish).
        self._facts: dict[str, dict] = {}
        # The page's own bulk request: customer key and the headers it was sent with.
        self._customer: Optional[str] = None
        self._api_headers: dict[str, str] = {}
        # Parts whose page showed other values than the stored data (_page_check).
        self._mismatch: set[str] = set()

    # -- signed in / out ----------------------------------------------------------
    @property
    def probe_url(self) -> str:
        return self.config.get("probe_url") or HOME_URL

    @property
    def signed_out_check_url(self) -> str:
        return LOGGED_OUT_URL

    async def _token_state(self, page) -> Optional[bool]:
        """On one of Incora's own pages: True = a token that hasn't expired, False =
        none, None = not on the shop or can't read it."""
        if urlsplit(page.url).hostname != SHOP_HOST:
            return None
        try:
            s = await page.evaluate(SESSION_JS)
        except Exception as exc:
            logger.debug("Incora token check: %r", exc)
            return None
        if not s["token"]:
            return False
        # A token within a minute of its end is as good as gone.
        return not s["expires"] or s["expires"] > s["now"] + 60_000

    async def _token_left(self, page) -> str:
        """How long the token has left, for the log ("4.6 min")."""
        try:
            s = await page.evaluate(SESSION_JS)
        except Exception:
            return "unknown"
        if not s["token"] or not s["expires"]:
            return "unknown"
        return f"{(s['expires'] - s['now']) / 60000:.1f} min"

    async def _keep_token(self, page) -> None:
        """A token near its end is refreshed by the app itself while one of its pages
        is open (angular-oauth2-oidc's automatic refresh, at 3/4 of its life); opening
        a page with an expired one sends the app to B2C's sign-in instead (run
        2026-11-05 16:42: signed out ~3.5 min after the login). So: wait on the open
        page for the refresh, up to REFRESH_WAIT_SECONDS."""
        if urlsplit(page.url).hostname != SHOP_HOST:
            return
        try:
            s = await page.evaluate(SESSION_JS)
        except Exception:
            return
        if not s["token"] or not s["expires"] or s["expires"] - s["now"] > REFRESH_AHEAD_MS:
            return
        before = s["expires"]
        logger.info("Incora: token ends in %.0fs; waiting for the app to refresh it",
                    (before - s["now"]) / 1000)
        deadline = time.time() + REFRESH_WAIT_SECONDS
        while time.time() < deadline:
            await page.wait_for_timeout(1000)
            try:
                s = await page.evaluate(SESSION_JS)
            except Exception:
                continue
            if s["expires"] > before:
                logger.info("Incora: token refreshed (valid for %.1f min)",
                            (s["expires"] - s["now"]) / 60000)
                return
        logger.warning("Incora: the app didn't refresh its token in %ss", REFRESH_WAIT_SECONDS)

    async def _session_state(self, page) -> Optional[bool]:
        """Opens /home and waits for the app to decide: back to B2C's sign-in (or its
        own log-in / logged-out page) = signed out; on the shop with a live token =
        signed in."""
        await page.goto(self.probe_url, wait_until="domcontentloaded")
        deadline = time.time() + SIGN_IN_WAIT_SECONDS
        state = None
        while time.time() < deadline:
            host = urlsplit(page.url).hostname or ""
            path = urlsplit(page.url).path.lower()
            try:
                if host == LOGIN_HOST or await self._password_field(page):
                    state = False
                    break
                if host == SHOP_HOST and path.rstrip("/") in ("/log-in", "/logged-out"):
                    state = False
                    break
                if await self._token_state(page) and not path.startswith("/auth"):
                    state = True
                    break
            except Exception as exc:          # mid-navigation: look again
                logger.debug("Incora sign-in check: %r", exc)
            await page.wait_for_timeout(500)
        logger.info("Incora sign-in check at %s: %s", page.url,
                    {True: "signed in", False: "signed out", None: "can't tell"}[state])
        return state

    def _token_proof(self, page) -> None:
        self.signed_in_proof = f"OAuth token on {page.url.split('?')[0]}"
        self.login_claimed = True
        self._login_ok = True

    async def is_authenticated(self, page) -> bool:
        self.apply_timeouts(page)
        state = await self._session_state(page)
        if state is not None:
            if state:
                self._login_ok = True
            return state
        return await super().is_authenticated(page)

    async def _open_login_page(self, page, cred) -> bool:
        """The login address loads the app, whose script then sends a signed-out
        browser on to B2C's form (after the page has "loaded", so the generic wait
        sees neither a form nor a logout link); a saved browser context that is
        still signed in stays on the shop with a token. False = signed in already."""
        url = self.login_url_candidates(cred)[0][1]
        logger.info("Login: opening %s", url)
        await page.goto(url, wait_until="domcontentloaded")
        deadline = time.time() + SIGN_IN_WAIT_SECONDS
        while time.time() < deadline:
            try:
                host = urlsplit(page.url).hostname or ""
                if host == LOGIN_HOST and await self._has_form(page):
                    self._remember_login_page(page, url)
                    return True
                if await self._token_state(page) and not urlsplit(page.url).path.startswith("/auth"):
                    logger.info("Login: %s has a live Incora token; already signed in", page.url)
                    self._token_proof(page)
                    return False
            except Exception as exc:          # mid-navigation: look again
                logger.debug("Login page wait: %r", exc)
            await page.wait_for_timeout(500)
        # Neither: the generic way (a Login button, the LLM) from here.
        return await super()._open_login_page(page, cred)

    def login_url_candidates(self, cred) -> list[tuple[str, str]]:
        """Only shop.incora.com addresses: the app makes a new B2C sign-in request
        (state, nonce, PKCE code) each time. A B2C address is one sign-in's own and
        can't be used again (run 2026-11-05 16:46: the address saved at 16:39 was
        opened, B2C took the password, the app refused the answer and showed the
        sign-in again); the secret's bare https://login.incora.com/ opens nothing
        (16:38, 31s). The adapter's default is always last."""
        out = [(source, url) for source, url in super().login_url_candidates(cred)
               if urlsplit(url).hostname == SHOP_HOST
               and source != "login page found on an earlier run"]
        dropped = [url for _, url in super().login_url_candidates(cred)
                   if urlsplit(url).hostname == LOGIN_HOST]
        if dropped:
            logger.info("Login: B2C address(es) not used as the start page (one sign-in "
                        "each): %s", ", ".join(u.split("?")[0] for u in dropped))
        if not any(url == self.default_login_url for _, url in out):
            out.append(("adapter default", self.default_login_url))
        return out

    def _remember_login_page(self, page, start_url: str) -> None:
        """Never saved: the page reached is B2C's one-time address (above)."""

    async def _after_submit(self, page, cred, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        """After SIGN IN, B2C sends the browser back to the shop (/auth-redirect, then
        /home about 2s later, the app's setting). A token there is the proof; B2C's
        own error lines are a wrong login; anything else goes to the generic checks."""
        deadline = time.time() + SIGN_IN_WAIT_SECONDS
        while time.time() < deadline:
            if await self._token_state(page):
                logger.info("Login: signed in (Incora token valid for %s; now at %s)",
                            await self._token_left(page), page.url)
                self._token_proof(page)
                if host_allowed(page.url, self.hosts):
                    self.learned["home_url"] = HOME_URL
                return
            if urlsplit(page.url).hostname == LOGIN_HOST:
                text = (await web._body_text(page)).lower()
                bad = next((h for h in BAD_LOGIN_HINTS if h in text), None)
                if bad:
                    logger.warning("Login: Incora says %r -> password needs updating", bad)
                    raise InvalidCredentials(f"Incora rejected the email or password ({bad!r})")
                if await self._mfa_input(page) or any(h in text for h in web.MFA_TEXT_HINTS):
                    break                     # a code page: the generic 2FA handling
            await page.wait_for_timeout(500)
        await super()._after_submit(page, cred, totp_tried=totp_tried, code_given=code_given)

    async def person_finished_login(self, page) -> bool:
        """Read-only: the person's page is on the shop with a token."""
        try:
            if page.is_closed() or not await self._token_state(page):
                return False
        except Exception:
            return False
        self._token_proof(page)
        return True

    async def _signed_out_here(self, page) -> bool:
        if (urlsplit(page.url).hostname or "") == LOGIN_HOST:
            return True
        if await self._password_field(page):
            return True
        return await self._token_state(page) is False

    async def logout(self, page) -> bool:
        """What the app's "Log out" does (angular-oauth2-oidc logOut): its tokens
        removed, then B2C's end-session address, which comes back to /logged-out.
        Confirmed by that page and no token, then /home asking for a sign-in."""
        try:
            if urlsplit(page.url).hostname != SHOP_HOST:
                await page.goto(LOGGED_OUT_URL, wait_until="domcontentloaded")
            had_token = await page.evaluate(
                LOGOUT_JS, [list(TOKEN_KEYS), END_SESSION_URL, LOGGED_OUT_URL])
            logger.info("Logout: tokens removed%s; B2C end-session",
                        "" if had_token else " (there was no id token)")
            try:
                await page.wait_for_url(re.compile(r"shop\.incora\.com/logged-out"),
                                        timeout=self.page_load_timeout_ms)
            except Exception:
                logger.warning("Logout: B2C didn't come back to /logged-out (at %s)", page.url)
            await _settle(page)
        except Exception as exc:
            logger.warning("Logout: %r", exc)
        if await self._token_state(page) is not False:
            logger.warning("Logout: not confirmed (a token is still there, at %s)", page.url)
            return False
        state = await self._session_state(page)
        logger.info("Logout: %s", "signed out" if state is False else "not confirmed")
        return state is False

    async def person_finished_logout(self, page) -> bool:
        """In a second tab (same storage), so the person's page is left alone."""
        tab = await page.context.new_page()
        try:
            await tab.goto(LOGGED_OUT_URL, wait_until="domcontentloaded")
            return await self._token_state(tab) is False
        except Exception as exc:
            logger.debug("Logout check: %r", exc)
            return False
        finally:
            await tab.close()

    # -- settings -----------------------------------------------------------------
    def _fill_condition(self, offers: list) -> list:
        code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN)
        for offer in offers:
            if code and (offer.condition or "").strip().upper() not in CONDITION_CODES:
                offer.condition = code
        return offers

    # -- search -------------------------------------------------------------------
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        """One part: a bulk search of one (the same page and data)."""
        outcomes, usage = await self._search_many(page, [row])
        outcome = outcomes.get(row.row_id) or SearchOutcome(found=False, page_url=BULK_URL)
        outcome.usage = usage
        return outcome

    async def search_many(self, page, rows: list[RequestRow]):
        if self.bulk_unavailable is not None:
            raise BulkSearchUnavailable(self.bulk_unavailable)
        try:
            return await self._search_many(page, rows)
        except BulkResultsIncomplete:
            raise                       # this batch only; the page itself works
        except BulkSearchUnavailable as exc:
            self.bulk_unavailable = str(exc)
            raise

    async def _search_many(self, page, rows: list[RequestRow]):
        if self.extractor is None:
            raise RuntimeError("Incora search needs an LLM to read the results")
        if len(rows) > MAX_LINES:
            raise BulkResultsIncomplete(f"{len(rows)} parts; Incora's bulk search takes "
                                        f"at most {MAX_LINES}")
        self.apply_timeouts(page)
        self._facts = {}
        items, from_data = await self._bulk_results(page, rows)
        usage = type(self.usage)()
        if from_data:
            await self._load_price_breaks(page, items)
            await self._page_check(page, items)
            text = self._records_text(items, rows)
        else:
            text = await self._table_text(page)
        if _dumping():
            _dump("bulk", text, items if from_data else None)
        if any(h in text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Incora says the session expired")
        check_rate_limited(text)

        not_found = {r.row_id for r in rows if from_data and not self._lines_for(items, r)}
        outcomes: dict[str, SearchOutcome] = {}
        # No LLM call when no part has a stock line: nothing to read.
        if not from_data or any(i.get("stockFound") and i.get("locations") for i in items):
            found = await self._read(text, rows, usage)
            for r in rows:
                offers = self._finish(found.get(r.part_number) or [], r)
                if offers:
                    outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=BULK_URL)
                    self._search_ok = self._search_ok or has_real_offer(offers)
        for r in rows:
            if r.row_id in not_found and r.row_id not in outcomes:
                # Incora's own answer, signed in: "Stock Not Found". An outcome here
                # keeps the pipeline from searching it again one by one.
                outcomes[r.row_id] = SearchOutcome(found=False, page_url=BULK_URL,
                                                   page_kind="no_results")
        if from_data:
            self._search_ok = True
        usage.add(self.usage)
        self.usage = type(self.usage)()
        logger.info("Incora bulk search: offers for %d of %d part(s)",
                    sum(1 for o in outcomes.values() if o.found), len(rows))
        return outcomes, usage

    async def _read(self, text: str, rows: list[RequestRow], usage) -> dict[str, list]:
        """The LLM reads the records, LINES_PER_CALL stock lines per call, at once."""
        parts = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                  "condition_wanted": r.condition_wanted} for r in rows]
        chunks = _record_chunks(text, LINES_PER_CALL)
        if len(text) > self.max_page_chars * len(chunks):
            raise BulkResultsIncomplete(f"Incora results are {len(text)} chars "
                                        f"(limit {self.max_page_chars} per call)")
        if len(chunks) > 1:
            logger.info("Incora: reading %d stock line(s) in %d LLM calls at once",
                        _line_count(text), len(chunks))
        answers = await asyncio.gather(*(self.extractor.extract_offers_bulk(
            page_text=chunk, page_url=BULK_URL, parts=parts, system=self.extract_system)
            for chunk in chunks))
        found: dict[str, list] = {}
        for kind, part_offers, used in answers:
            usage.add(used)
            if kind == "login_page":
                raise SessionExpired("Extractor saw a login page")
            if kind == "error":
                raise BulkResultsIncomplete("Could not read the Incora results (LLM answer "
                                            "missing or cut off)")
            for pn, offers in part_offers.items():
                found.setdefault(pn, []).extend(offers)
        return found

    async def _open_shop(self, page) -> None:
        """An Incora page with the app loaded (localStorage is per site)."""
        await self._keep_token(page)
        if urlsplit(page.url).hostname == SHOP_HOST and await self._token_state(page):
            return
        logger.info("Incora: opening %s (token at %s: %s left)", HOME_URL, page.url,
                    await self._token_left(page))
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        for _ in range(SIGN_IN_WAIT_SECONDS * 2):
            if await self._signed_out_here(page) and urlsplit(page.url).hostname == LOGIN_HOST:
                break
            if await self._token_state(page):
                return
            await page.wait_for_timeout(500)
        raise SessionExpired(f"Incora: not signed in (at {page.url})")

    async def _bulk_results(self, page, rows: list[RequestRow]) -> tuple[list[dict], bool]:
        """Step 2-3: (the items, True) from the page's stored data, else ([], False)
        with the table left open for _table_text."""
        await self._open_shop(page)
        codes = search_codes(rows)
        requests: list = []

        def on_request(request) -> None:       # Playwright wants a plain function
            url = request.url.split("?")[0]
            if request.method == "POST" and url == BULK_API:
                requests.append(request)
            elif not self._customer:
                # The app's other calls carry the customer too (.../Customer/<key>/...).
                match = re.match(re.escape(API_ROOT) + r"/([^/]+)/", url)
                if match and match.group(1).lower() not in _API_WORDS:
                    self._customer = match.group(1)

        page.on("request", on_request)
        try:
            await page.evaluate(START_SEARCH_JS, [SEARCH_CODES_KEY, RESULTS_KEY, codes])
            logger.info("Incora step 2 (search): %d part(s) on %s", len(codes), BULK_URL)
            response = await page.goto(BULK_URL, wait_until="domcontentloaded")
            status = response.status if response is not None else 0
            if status >= 400:
                raise BulkSearchUnavailable(f"Incora bulk search page gave HTTP {status}")
            items = await self._wait_for_results(page)
        finally:
            page.remove_listener("request", on_request)
        if await self._signed_out_here(page):
            raise SessionExpired(f"Signed out on the Incora bulk search page ({page.url})")
        if requests:
            await self._remember_api(requests[-1])
        if items is None:
            logger.warning("Incora: the page's stored results weren't there after %ss; "
                           "reading the table", RESULTS_WAIT_SECONDS)
            return [], False
        await self._check_count(page, items)
        lines = sum(len(i.get("locations") or []) for i in items)
        logger.info("Incora step 3 (read): %d part(s), %d with stock, %d stock line(s)",
                    len(items), sum(1 for i in items if i.get("stockFound")), lines)
        _log_new_keys(items)
        return items, True

    async def _wait_for_results(self, page) -> Optional[list]:
        """The stored result, once the page has saved it (after the bulk request)."""
        deadline = time.time() + RESULTS_WAIT_SECONDS
        while time.time() < deadline:
            if urlsplit(page.url).hostname == LOGIN_HOST:
                return None
            try:
                data = await page.evaluate(RESULTS_JS, RESULTS_KEY)
            except Exception:
                data = None                     # mid-navigation
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            await page.wait_for_timeout(500)
        return None

    async def _check_count(self, page, items: list[dict]) -> None:
        """The paginator's "1 - 50 of N" against the stored rows: fewer stored than
        the table lists would be missing parts."""
        try:
            label = await page.evaluate(PAGINATOR_JS)
        except Exception:
            label = ""
        total = re.search(r"of\s+([\d,]+)", label or "")
        if total and int(total.group(1).replace(",", "")) > len(items):
            raise BulkResultsIncomplete(f"Incora table lists {total.group(1)} parts, the "
                                        f"page's data {len(items)}")
        if label:
            logger.info("Incora results table: %s (all read from the page's data)", label)

    async def _remember_api(self, request) -> None:
        """The customer key and the headers the page sent its bulk request with:
        the same ones ask for each line's price breaks."""
        try:
            body = request.post_data_json or {}
            headers = await request.all_headers()
        except Exception as exc:
            logger.warning("Incora: bulk request not read: %r", exc)
            return
        if isinstance(body, dict) and body.get("key"):
            self._customer = str(body["key"])
        self._api_headers = {k: v for k, v in headers.items()
                             if k.lower() == "authorization" or k.lower().startswith(("x-", "ocp-"))}

    async def _load_price_breaks(self, page, items: list[dict]) -> None:
        """Step 4: each stock line's price breaks, as its $ pop-up loads them. A line
        whose breaks couldn't be read keeps `_breaks_error` (flagged on the line)."""
        lines = [loc for item in items for loc in item.get("locations") or []
                 if isinstance(loc, dict)]
        if not lines:
            return
        if not self._customer or not self._api_headers.get("authorization"):
            await self._customer_from_storage(page)
        if not self._customer:
            for loc in lines:
                loc["_breaks_error"] = "customer account not known"
            logger.warning("Incora step 4: no customer key from the page; price breaks not read")
            return
        gate = asyncio.Semaphore(PRICE_FETCHES_AT_ONCE)

        async def one(loc: dict) -> None:
            async with gate:
                loc["_breaks"], loc["_breaks_error"] = await self._price_breaks(page, loc)

        started = time.time()
        await asyncio.gather(*(one(loc) for loc in lines))
        failed = [loc for loc in lines if loc.get("_breaks_error")]
        logger.info("Incora step 4 (prices): %d stock line(s) in %.1fs, %d not read",
                    len(lines), time.time() - started, len(failed))

    async def _customer_from_storage(self, page) -> None:
        """No bulk request seen (the page used its stored result): the bearer token
        from storage; the customer key from the app's own calls is still needed."""
        if not self._api_headers.get("authorization"):
            try:
                token = await page.evaluate("() => localStorage.getItem('access_token')")
            except Exception:
                token = None
            if token:
                self._api_headers["authorization"] = f"Bearer {token}"

    async def _price_breaks(self, page, loc: dict) -> tuple[list, Optional[str]]:
        spec = loc.get("itemSpecKey")
        if not spec:
            return [], "the line has no item key"
        url = DETAIL_API.format(customer=quote(self._customer, safe=""),
                                spec=quote(str(spec), safe=""))
        try:
            # From inside the page, as the app's own $ pop-up asks (same origin, CORS
            # and headers).
            resp = await asyncio.wait_for(page.evaluate(FETCH_JS, [url, self._api_headers]),
                                          self.action_timeout_ms / 1000)
        except Exception as exc:
            return [], f"request failed: {str(exc).splitlines()[0]}"
        status = resp.get("status") or 0
        if status in (401, 403):
            raise SessionExpired(f"Incora price breaks: HTTP {status}")
        if status >= 400 or status == 0:
            return [], f"HTTP {status}" if status else f"request failed: {resp.get('error')}"
        data = resp.get("body")
        if data is None:
            return [], "not JSON"
        breaks = (data or {}).get("priceBreaks") if isinstance(data, dict) else None
        if not isinstance(breaks, list):
            return [], "no price-break list in the answer"
        if isinstance(data, dict) and data.get("itemDescription") and not loc.get("_description"):
            loc["_description"] = data["itemDescription"]
        return [b for b in breaks if isinstance(b, dict)], None

    # -- the page itself: open a part, its $ pop-up, the × hover text ------------------
    async def _open_part(self, page, code: str, row=None) -> bool:
        """A click on the part's row opens its stock lines (the row's onRowClicked,
        main.js 2026-11-05; one part open at a time). True once a $ button shows."""
        target = row if row is not None else page.get_by_text(code, exact=True).first
        try:
            await target.click(timeout=OPEN_WAIT_MS)
            await page.locator(PRICE_BUTTON).first.wait_for(state="visible",
                                                            timeout=OPEN_WAIT_MS)
            return True
        except Exception as exc:
            logger.info("Incora: %s didn't open (%s)", code or "part row",
                        str(exc).splitlines()[0][:120])
            return False

    async def _popup_breaks(self, page, button, dump: str = "") -> Optional[list[tuple[str, str, str]]]:
        """Click a stock line's $ and read its pop-up (Qty / Price rows, loaded when
        it opens); the same click closes it. None when it didn't open."""
        try:
            await button.click(timeout=OPEN_WAIT_MS)
            popup = page.locator(PRICE_POPUP).last
            await popup.wait_for(state="visible", timeout=OPEN_WAIT_MS)
            text = ""
            for _ in range(OPEN_WAIT_MS // 250):      # a spinner first, then the rows
                text = await popup.inner_text()
                if _POPUP_ROW.search(text) or "Qty" in text and "Price" in text \
                        and not await popup.locator("mat-spinner, mat-progress-spinner").count():
                    break
                await page.wait_for_timeout(250)
            rows = [(m.group(1).replace(",", ""), m.group(2).replace(",", ""), m.group(3) or "")
                    for m in _POPUP_ROW.finditer(text)]
        except Exception as exc:
            logger.info("Incora: $ pop-up not read (%s)", str(exc).splitlines()[0][:120])
            return None
        if dump:
            await _dump_html(page, dump)                      # with the pop-up shown
        try:
            await button.click(timeout=OPEN_WAIT_MS)          # closes it again
        except Exception:
            await page.keyboard.press("Escape")
        return rows

    async def _hover_text(self, page, icon) -> str:
        """The tooltip a person sees on hovering an icon (× = "Stock Not Found").
        The icon's own mf-icon carries it (mattooltip="Stock Not Found", saved page
        2026-11-05 17:18: the hover pop-up wasn't found), so that first, then the
        hover."""
        try:
            tip = await icon.evaluate(
                "i => { const t = i.closest('[mattooltip]'); return t ? t.getAttribute('mattooltip') : ''; }")
            if tip:
                return " ".join(tip.split())
        except Exception:
            pass
        try:
            await icon.hover(timeout=OPEN_WAIT_MS)
            tip = page.locator(TOOLTIP).last
            await tip.wait_for(state="visible", timeout=OPEN_WAIT_MS)
            text = " ".join((await tip.inner_text()).split())
            await page.mouse.move(0, 0)
            return text
        except Exception:
            return ""

    async def _page_check(self, page, items: list[dict]) -> None:
        """What a person does, once per search: open the first part with stock (its
        row), click its first stock line's $, hover a ×. The page's values are
        compared with the stored data the lines come from; a part whose page shows
        something else is flagged PAGE_CHECK_MISMATCH (_finish). Best effort: a
        control not found only logs (vendor config `page_check: false` turns it off)."""
        self._mismatch = set()
        if not self.config.get("page_check", True):
            return
        first = next((i for i in items[:TABLE_PAGE_SIZE] if i.get("stockFound")
                      and i.get("locations")), None)
        notes = []
        if first is not None:
            code, loc = _text(first["itemCode"]), first["locations"][0]
            if await self._open_part(page, code):
                shown = await page.locator(PRICE_BUTTON).first.evaluate(NESTED_TEXT_JS)
                qty = _number(loc.get("quantityAvailable"))
                qty_ok = qty is None or f"{int(qty):,}" in shown or str(int(qty)) in shown
                popup = await self._popup_breaks(page, page.locator(PRICE_BUTTON).first,
                                                 dump="bulk-page" if _dumping() else "")
                data = [(qty_text(b.get("quantity")), price_text(b.get("price")))
                        for b in loc.get("_breaks") or []]
                pop = [(qty_text(q), price_text(p)) for q, p, _ in popup or []]
                breaks_ok = popup is None or loc.get("_breaks_error") or pop == data
                if not qty_ok or not breaks_ok:
                    self._mismatch.add(_key(code))
                    logger.warning("Incora page check %s: page and data differ (qty %s on the "
                                   "page: %s; $ pop-up %s vs data %s)", code, qty,
                                   "yes" if qty_ok else "NO", pop, data)
                notes.append(f"{code} opened, qty {'=' if qty_ok else '!='} data, $ pop-up "
                             + ("not read" if popup is None else
                                f"{len(pop)} row(s) {'=' if breaks_ok else '!='} data"))
        cross = page.locator(NOT_FOUND_ICON).first
        if await cross.count():
            tip = await self._hover_text(page, cross)
            notes.append(f"× says {tip!r}" if tip else "× hover text not shown")
        if notes:
            logger.info("Incora page check: %s", "; ".join(notes))

    async def _table_text(self, page) -> str:
        """Fallback: the results table as a person reads it, page by page (Next page
        until it is disabled): each part with a ✓ opened by its row, every stock
        line's $ pop-up read, then the table's text."""
        pages = []
        for n in range(1, MAX_LINES // 10 + 2):
            try:
                text = await page.evaluate(TABLE_JS)
            except Exception:
                text = ""
            opened = await self._opened_parts_text(page)
            pages.append(TABLE_NOTE.format(n=n) + "\n"
                         + (text or await web.page_text(page, "incora")) + opened)
            nxt = page.locator(NEXT_PAGE).first
            try:
                if not await nxt.count() or await nxt.is_disabled():
                    break
                before = await page.evaluate(PAGINATOR_JS)
                await nxt.click(timeout=self.action_timeout_ms)
                for _ in range(20):
                    if await page.evaluate(PAGINATOR_JS) != before:
                        break
                    await page.wait_for_timeout(250)
            except Exception as exc:
                logger.warning("Incora: next table page not opened: %r", exc)
                break
        logger.warning("Incora: read %d table page(s) from the page itself", len(pages))
        return "\n\n".join(pages)

    async def _opened_parts_text(self, page) -> str:
        """Every part on this table page with a ✓: its row clicked open, its stock
        lines' text and each line's $ pop-up."""
        out = []
        found = page.locator(FOUND_ICON)
        for i in range(min(await found.count(), TABLE_PAGE_SIZE * 5)):
            row = found.nth(i).locator(ROW_OF_ICON).first
            try:
                label = " ".join((await row.inner_text()).split())[:120]
            except Exception:
                label = f"part {i + 1}"
            if not await self._open_part(page, "", row=row):
                out.append(f"\n--- {label}: stock lines didn't open ---")
                continue
            buttons = page.locator(PRICE_BUTTON)
            lines = await buttons.first.evaluate(NESTED_TEXT_JS)
            out.append(f"\n--- Stock lines of {label} ---\n{lines}")
            for k in range(await buttons.count()):
                rows = await self._popup_breaks(page, buttons.nth(k))
                out.append(f"Stock line {k + 1} price breaks (Qty: Price): " + (
                    "not read" if rows is None else "; ".join(
                        f"{q}: {p}{' ' + c if c else ''}" for q, p, c in rows) or "none shown"))
        return "\n".join(out)

    # -- records ------------------------------------------------------------------
    @staticmethod
    def _lines_for(items: list[dict], row: RequestRow) -> list[dict]:
        """The stock lines of the item answering this requested part."""
        key = _key(row.part_number)
        return [loc for item in items if _key(item.get("itemCode")) == key and item.get("stockFound")
                for loc in item.get("locations") or [] if isinstance(loc, dict)]

    def _records_text(self, items: list[dict], rows: list[RequestRow]) -> str:
        """One block per part, one record per stock line, by the page's column names,
        with its link and price breaks; the program's facts kept per link (_finish)."""
        lines = [RECORDS_NOTE]
        for item in items:
            code = _text(item.get("itemCode"))
            locs = [loc for loc in item.get("locations") or [] if isinstance(loc, dict)]
            head = [f"Part block: {code}"]
            if item.get("itemDescription"):
                head.append(f"Description: {_text(item['itemDescription'])}")
            if item.get("searchQuantity") not in (None, ""):
                head.append(f"Search Qty: {_text(item['searchQuantity'])}")
            # The page's ✓ / × icons and their hover text.
            head.append("Result: Stock Found" if item.get("stockFound") else
                        "Result: Stock Not Found")
            lines.append("\n" + " | ".join(head))
            if not item.get("stockFound") or not locs:
                lines.append(f"{code}: no stock lines")
                continue
            for n, loc in enumerate(locs, start=1):
                link = detail_link(loc)
                if link == BULK_URL:
                    link = f"{BULK_URL}#{_key(code)}-{n}"
                cells = [f"Link: {link}"]
                for field, label in LOCATION_FIELDS:
                    value = loc.get(field)
                    # Shelf Life 0 = none (every line of run 2026-11-05 16:39; the page
                    # shows the column empty).
                    if value in (None, "") or (field == "shelfLifeDays" and not _number(value)):
                        continue
                    cells.append(f"{label}: {_text(value)}")
                if _text(loc.get("pricingUnitOfMeasure")):
                    # Empty on every line of 2026-11-05; when set, the prices are per it.
                    cells.append(f"Pricing UOM: {_text(loc['pricingUnitOfMeasure'])}")
                breaks = break_rows(loc.get("_breaks") or [])
                currency = next((_text(b.get("currencyCode")) for b in loc.get("_breaks") or []
                                 if b.get("currencyCode")), "")
                if breaks:
                    cells.append("Price breaks (Qty: Price): " + "; ".join(
                        f"{low}: {price}{' ' + currency if currency else ''}"
                        for low, _, price in breaks))
                elif loc.get("_breaks_error"):
                    cells.append("Price breaks: not read")
                else:
                    cells.append("Price breaks: none shown (no price)")
                lines.append(f"{code} line {n}: " + " | ".join(cells))
                self._facts[link] = self._line_facts(item, loc, breaks, currency, n)
                self._facts[link]["mismatch"] = _key(code) in self._mismatch
        return "\n".join(lines)

    @staticmethod
    def _line_facts(item: dict, loc: dict, breaks: list, currency: str, n: int) -> dict:
        qty = _number(loc.get("quantityAvailable"))
        facts = {"part": _text(item.get("itemCode")), "n": n,
                 "qty": (qty_text(qty) if qty and qty > 0 else "Out of stock") if qty is not None
                 else None,
                 "uom": _text(loc.get("unitOfMeasure")) or None,
                 "site": _text(loc.get("site")),
                 "breaks": breaks, "currency": currency or None,
                 "breaks_error": loc.get("_breaks_error")}
        pricing_uom = _text(loc.get("pricingUnitOfMeasure"))
        notes = []
        if breaks:
            at_one = next((b for b in breaks if _number(b[0]) == 1), None)
            facts["price"] = (at_one or breaks[0])[2]
            if not at_one:
                notes.append(f"PRICE BREAKS START AT QTY {breaks[0][0]}")
        elif not loc.get("_breaks_error"):
            # An empty price list: the $ pop-up shows no prices (AN310-4, AN525-10R8,
            # MS21042L3 on 2026-11-05), i.e. a quote only.
            notes.append("NO PRICE SHOWN ON INCORA (REQUEST A QUOTE)")
        if pricing_uom and pricing_uom.upper() != (facts["uom"] or "").upper():
            facts["pricing_uom"] = pricing_uom
            notes.append(f"PRICED PER {pricing_uom.upper()}, STOCK IN {(facts['uom'] or '?').upper()}")
        for field, label in (("manufacturerName", "MANUFACTURER"), ("revision", "REVISION")):
            if _text(loc.get(field)):
                notes.append(f"{label}: {_text(loc[field]).upper()}")
        if _number(loc.get("shelfLifeDays")):
            notes.append(f"SHELF LIFE: {qty_text(loc['shelfLifeDays'])} DAYS")
        facts["notes"] = notes
        facts["trace"] = cert_trace(loc.get("certificateDescription"))
        return facts

    # -- the line: what the page states -------------------------------------------
    def _finish(self, offers: list, row: RequestRow) -> list:
        """The values the program read itself are set on each line (team, 2026-11-04:
        no value the page doesn't state): stock, unit, price at qty 1, the price-break
        table, currency, the Site as the line's warehouse. An offer for no stock line
        read is dropped; a line whose price breaks weren't read is flagged."""
        kept = []
        for offer in offers:
            facts = self._facts_for(offer, row)
            if facts is None:
                logger.warning("Incora: offer for %s matches no stock line read (%s); dropped",
                               offer.part_number, offer.source_url)
                continue
            self._match_page(offer, facts)
            note = cheaper_break_note(offer.price_breaks, row.qty_needed)
            if note:
                offer.vendor_note = "; ".join(n for n in (offer.vendor_note, note) if n)
            kept.append(offer)
        return self._fill_condition(kept)

    def _facts_for(self, offer, row: RequestRow) -> Optional[dict]:
        facts = self._facts.get((offer.source_url or "").strip())
        if facts is not None:
            return facts
        key = _key(offer.part_number) or _key(row.part_number)
        same = [f for f in self._facts.values() if _key(f["part"]) == key]
        if len(same) > 1:
            same = [f for f in same if f["qty"] == (offer.qty_available or "").replace(",", "")]
        return same[0] if len(same) == 1 else None

    @staticmethod
    def _match_page(offer, facts: dict) -> None:
        changed = []

        def put(field: str, value) -> None:
            if (getattr(offer, field) or None) != (value or None):
                changed.append(f"{field} {getattr(offer, field)!r} -> {value!r}")
                setattr(offer, field, value)

        if facts.get("qty"):
            put("qty_available", facts["qty"])
        put("uom", facts.get("uom"))
        put("price", facts.get("price"))
        put("price_breaks", list(facts.get("breaks") or []))
        if facts.get("trace"):
            put("trace", facts["trace"])
        put("currency", facts.get("currency") if facts.get("price") else None)
        put("warehouses", format_warehouses([{"name": facts["site"],
                                              "qty_on_hand": facts.get("qty") or ""}])
            if facts.get("site") else None)
        # Incora shows no lead time: in stock is all it says.
        stocked = (_number(facts.get("qty")) or 0) > 0
        put("lead_time", "In stock" if stocked else None)
        offer.vendor_note = "; ".join(facts.get("notes", [])) or None
        flags = [f for f in (offer.review or "").split(",") if f]
        if facts.get("pricing_uom"):
            # Prices per another unit than the stock: the pipeline's quantity maths
            # would mix them, so a person checks.
            flags.append("PRICING_UOM_DIFFERS")
        if changed:
            logger.warning("Incora %s line %s: set to what the page states: %s",
                           facts["part"], facts["n"], "; ".join(changed))
            fixed = {"QTY", "PRICE", "CURRENCY", "UOM", "WAREHOUSES", "LEAD_TIME",
                     "PRICE_BREAKS", "TRACE"}
            flags = [f for f in flags if f.removesuffix("_NOT_ON_PAGE") not in fixed]
        if facts.get("trace"):
            flags = [f for f in flags if f != "TRACE_NOT_ON_PAGE"]
        if facts.get("mismatch"):
            flags.append("PAGE_CHECK_MISMATCH")
        if facts.get("breaks_error"):
            # The $ pop-up's prices weren't read: a visible gap, never "no price".
            flags.append("PRICE_BREAKS_NOT_READ")
        offer.review = ",".join(dict.fromkeys(flags)) or None


_LINE = re.compile(r"^\S+ line \d+: ", re.M)


def _line_count(text: str) -> int:
    return len(_LINE.findall(text))


def _record_chunks(text: str, max_lines: int) -> list[str]:
    """Records -> pieces of whole part blocks with at most `max_lines` stock lines
    each (a bigger block stays whole), each with the note on top. Blocks without
    stock lines go with the first piece only (nothing to read in them)."""
    head, sep, body = text.partition("\nPart block: ")
    if not sep:
        return [text]
    blocks = ["Part block: " + b for b in body.split("\nPart block: ")]
    empty = [b for b in blocks if not _line_count(b)]
    full = [b for b in blocks if _line_count(b)]
    chunks, current, n = [], [], 0
    for block in full:
        count = _line_count(block)
        if current and n + count > max_lines:
            chunks.append(current)
            current, n = [], 0
        current.append(block)
        n += count
    if current or not chunks:
        chunks.append(current)
    chunks[0] = chunks[0] + empty
    return [head + "\n" + "\n".join(c) for c in chunks]


def _log_new_keys(items: list[dict]) -> None:
    """Fields the stored data has that the records don't show yet: a cue to add
    them (first live run)."""
    item_keys = {k for i in items for k in i} - _KNOWN_ITEM_KEYS
    loc_keys = {k for i in items for loc in i.get("locations") or [] if isinstance(loc, dict)
                for k in loc} - _KNOWN_LOCATION_KEYS
    if item_keys or loc_keys:
        logger.info("Incora: stored data also has part fields %s, stock line fields %s "
                    "(not read)", sorted(item_keys), sorted(loc_keys))


async def _dump_html(page, label: str) -> None:
    """With DEBUG_DUMP_PAGES=1: the results page's HTML with a part open and its $
    pop-up shown (what the clicks found, for building the next selectors)."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-incora-{label}.html"
        path.write_text(await page.content(), encoding="utf-8")
        logger.info("Saved the results page (part open, $ pop-up) to %s", path)
    except Exception as exc:
        logger.warning("Could not save the results page: %r", exc)


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _dump(label: str, text: str, items: Optional[list]) -> None:
    """With DEBUG_DUMP_PAGES=1: the text the LLM reads and the page's raw data."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        path = web.DUMP_DIR / f"{stamp}-incora-{label}-read.txt"
        path.write_text(text, encoding="utf-8")
        if items is not None:
            raw = [{k: v for k, v in i.items()} for i in items]
            (web.DUMP_DIR / f"{stamp}-incora-{label}-data.json").write_text(
                json.dumps(raw, indent=1, default=str), encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
