"""MonroeAdapter: Monroe Aerospace (monroeaerospace.com, a BigCommerce store).

Seen on the public pages 2026-11-04 (search and product pages open without a
login; what a signed-in page shows differently is checked on the first run):

  1 sign in   the login page is the secret's `url` (no address in the code; its
              `#/login` form is what the site's Sign In link opens, team
              2026-11-04); email + password in the form (login_email, login_pass). TOTP when the secret has one; else a person through
              Live View. Signed in or not is told by /account.php (200 vs a
              redirect to the login page); logout is /login.php?action=logout
  2 search    no search box is used: the results address
                one part   search.php?search_query_adv=<part>
                several    search.php?search_query_adv=<A>%2C+<B>%2C+<C>
              (spaces as +, parts joined by ", ", "/" as %2F: the team's
              format, 2026-11-04). The search matches WORDS: three parts gave
              "8898 results", the exact part numbers among thousands of others,
              12 cards a page
  3 pick      the cards (li.product > article.productCard) whose part number is a
              requested one (spaces, dashes, slashes, dots aside); the others
              are dropped, they are only look-alikes. Next result pages are read
              until every requested part has a card (vendor config
              `results_pages_max`, default 3). A part still without a card in a
              multi-part search is searched alone (its exact card comes first
              there), at most `missing_single_max` (5) per batch
  4 open      each picked card's product page (at most `product_pages_max`, 3,
              per part): the search card lacks the minimum purchase ("Minimum
              Purchase: 5 units"), the unit and the notes
  5 read      card + product page per part by Monroe's own prompt
              (monroe_prompt.py), every value checked
  6 finish    the generic logout, after the account check says signed out

Team's note (2026-11-04): where the product type is known, a category address
(monroeaerospace.com/aeroduct-products/scat-4/) lists the part more
directly. Not used yet: the sheet has no product-type column; the search
address finds the same product.

Never opened: cart, checkout, quote, wishlist, compare, account links, "View
Similar Products". A run with --dump-pages saves each results page and the text
read (debug_pages/*-monroe-*).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote_plus, urljoin, urlsplit

from ..conditions import CONDITION_CODES
from ..models import RequestRow, SearchOutcome
from . import web
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, SessionExpired,
    check_rate_limited, has_real_offer, host_allowed,
)
from .generic import GenericAdapter
from .monroe_prompt import MONROE_EXTRACT_SYSTEM
from .web import _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.monroe")

SEARCH_URL = "https://monroeaerospace.com/search.php?search_query_adv={part}"
# Signed in or not without touching the open page (BigCommerce): /account.php
# with the browser's cookies answers 200 when signed in, a redirect to the login
# page when not. The sign-out link sits in a menu, so the generic check can't
# tell on every page.
ACCOUNT_URL = "https://monroeaerospace.com/account.php"
LOGOUT_URL = "https://monroeaerospace.com/login.php?action=logout"
# The sign-in form (seen 2026-11-04). Saved like selectors from an earlier run:
# one that doesn't fit is skipped. The header's search form is not it.
KNOWN_SELECTORS = {"username_input": "form.login-form input#login_email",
                   "password_input": "form.login-form input#login_pass",
                   "submit_button": "form.login-form input[type='submit']"}
# Most parts in one search: the exact cards of 10 parts rarely all fit on one
# page of 12, hence the paging below.
MAX_PARTS = 10
RESULTS_PAGES_MAX = 3
PRODUCT_PAGES_MAX = 3
MISSING_SINGLE_MAX = 5
RESULTS_WAIT_SECONDS = 15
CARD_SELECTORS = ("li.product article.productCard", "article.productCard")
NEXT_PAGE = ".pagination-item--next a[href], a[rel='next'][href]"
_NO_RESULTS = r"no products|0 results|no results|did not match|nothing found"
# Links the program never follows ("next page" can't be one, but checked).
_NEVER_OPEN = re.compile(r"cart|checkout|quote|order|wishlist|compare|account|logout", re.I)
# The page after the product's own section.
PRODUCT_END = "View Similar Products"
# The product page's link to its bulk discount rates (price breaks).
BULK_LINK_TEXT = "Bulk Discounts"
PRODUCT_TEXT_MAX = 6000
# Product pages open at once, each in its own tab (few requests to a live portal
# at a time).
PRODUCT_TABS_PARALLEL = 3
# The variants table is drawn by the page's script after load (its stock query):
# seen ~2-4s on a local browser, 2026-11-04.
VARIANTS_WAIT_SECONDS = 15
# Resources a product tab never loads (only the page's text is read).
_SKIP_RESOURCES = ("image", "font", "media")
# Monroe shows no condition; the client's RFQ rule quotes no in-stock line
# without one (Wencor, run 2026-11-03: "No Quote"), so the same default.
# Vendor config `condition_not_shown` changes it ("" leaves it empty).
CONDITION_NOT_SHOWN = "NE"

HEADER = ("--- Monroe search for {query}: {total}; {picked} ({pages}) ---")
NO_CARD = "--- Monroe search for {query}: no card is for {wanted} ({total}; {pages}) ---"
CARD_HEADER = "--- Card {n}: {link} ---"
PRODUCT_HEADER = "--- Product page of card {n}: {link} ---"
VARIANTS_HEADER = ("--- Stock table of card {n} (the product's variants table, all variants; "
                   "read by the program) ---")
BULK_HEADER = "--- Bulk discount rates of card {n} (the \"Bulk Discounts\" pop-up) ---"

# One card: the program writes its fields instead of the page's innerText (the
# hidden "$0.00" of a Request Quote card, the menu and the share links stay out).
CARDS_JS = """([selectors, max]) => {
  let cards = [];
  for (const s of selectors) {
    cards = [...document.querySelectorAll(s)].filter(el => !el.closest('[data-similar-products]'));
    if (cards.length) break;
  }
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0; };
  return cards.slice(0, max).map(card => {
    const sku = card.querySelector('.productCard-sku a, .productCard-sku');
    // In this order: a list in one querySelector gives the first in page order, and
    // the share menu's mailto: link comes before the product link.
    let link = null;
    for (const sel of ['.productCard-sku a[href]', 'a.productCard-imageLink[href]',
                       'a[href^="http"]']) {
      link = card.querySelector(sel);
      if (link) break;
    }
    const price = card.querySelector('.productCard-imagePrice');
    const stock = card.querySelector('.productCard-stockText, .productCard-stock');
    const quote = card.querySelector('.productCard-requestQuote');
    const cart = card.querySelector('.productCard-addToCart');
    return {sku: clean(sku && sku.textContent),
            name: clean((card.querySelector('.productCard-name') || {}).textContent),
            price: shown(price) ? clean(price.textContent) : '',
            stock: clean(stock && stock.textContent),
            quote: !!quote, cart: !!cart,
            href: link ? link.href : ''};
  });
}"""
# The product's own section (title, price, stock, minimum purchase, notes). Not
# .productView alone: the Specifications section (note-Minimum Order Note) is
# outside it, in #main-content (seen 2026-11-04).
PRODUCT_JS = """() => {
  const root = document.querySelector('#main-content') || document.querySelector('.productView')
    || document.body;
  // The variants table is read as records (VARIANTS_JS); its cells as plain text are
  // a column of "Any" choices.
  document.querySelectorAll('[data-variant-filter-wrapper]').forEach(e => e.style.display = 'none');
  const text = (root.innerText || '').replace(/\\n{2,}/g, '\\n').trim();
  document.querySelectorAll('[data-variant-filter-wrapper]').forEach(e => e.style.display = '');
  return text;
}"""
# The variants table (seen 2026-11-04): one row of filter drop-downs (Raw Material,
# Test Reports, Trace To MFR, MFR Certificate, DFAR, Revision, Manufacturer), then
# Available (all variants matching the filters), Unit of Measure, Unit Price.
VARIANTS_READY_JS = """() => {
  const t = document.querySelector('[data-variant-filter-total-row]');
  return !!(t && /\\d/.test(t.textContent));
}"""
VARIANTS_JS = """() => {
  const table = document.querySelector('[data-variant-filter-wrapper] table, table.variant-filter-table');
  if (!table) return null;
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const heads = [...table.querySelectorAll('thead th')].map(th => clean(th.textContent));
  const cells = [...table.querySelectorAll('tbody tr:first-child td')];
  const choices = {}, values = {};
  cells.forEach((td, i) => {
    const name = heads[i] || td.dataset.label || ('column ' + i);
    const sel = td.querySelector('select');
    if (sel) {
      choices[name] = [...sel.options].map(o => clean(o.textContent)).filter(v => v && v !== 'Any');
    } else {
      values[name] = clean(td.textContent);
    }
  });
  return {choices, values};
}"""
BULK_JS = """() => [...document.querySelectorAll('#modal-bulk-pricing .bulk-discount-list li, '
  + '#modal-bulk-pricing li')].map(li => (li.textContent || '').replace(/\\s+/g, ' ').trim())
  .filter((t, i, all) => t && all.indexOf(t) === i)"""


def _product_section(text: str) -> str:
    """The product's own part: up to "View Similar Products", at most PRODUCT_TEXT_MAX."""
    cut = text.find(PRODUCT_END)
    return (text[:cut] if cut > 0 else text)[:PRODUCT_TEXT_MAX].strip()


# The same section from fetched HTML (nothing laid out, so no innerText), a line
# break at each block as innerText would give. Parts hidden by an inline style are
# KEPT: the server sends "Minimum Purchase: 5 units" in a dl with display:none that
# the page's script shows (seen 2026-11-04); a hidden "$0.00" is no price (prompt).
PRODUCT_HTML_JS = """(html) => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const root = doc.querySelector('#main-content') || doc.querySelector('.productView') || doc.body;
  // Pop-ups go, except the bulk discount rates (the price breaks).
  root.querySelectorAll('script, style, svg, noscript, template, [data-similar-products], '
    + '.modal:not(#modal-bulk-pricing), [data-variant-filter-wrapper]').forEach(e => e.remove());
  const BLOCK = new Set(['P','DIV','LI','TR','TD','TH','H1','H2','H3','H4','H5','H6','DT','DD',
                         'SECTION','ARTICLE','TABLE','UL','OL','DL','FORM','LABEL','BR','ASIDE','NAV']);
  const out = [];
  const walk = node => {
    if (node.nodeType === 3) { out.push(node.textContent); return; }
    if (node.nodeType !== 1) return;
    const block = BLOCK.has(node.tagName);
    if (block) out.push('\\n');
    node.childNodes.forEach(walk);
    if (block) out.push('\\n');
  };
  walk(root);
  return out.join('').split('\\n').map(l => l.replace(/\\s+/g, ' ').trim()).filter(Boolean).join('\\n');
}"""


# What each product's own text states, read by the program (2026-11-04, team: no
# value the page doesn't state). Labels as the browser shows them; the fetched
# HTML writes some with a "note-" / "spec-" prefix.
_MIN_ORDER = re.compile(r"Minimum order is ([\d,]+)", re.I)
_MIN_PURCHASE = re.compile(r"Minimum Purchase:\s*([\d,]+)\s*units?", re.I)
_INDUSTRY = re.compile(r"(?:note-|spec-)?Industry Standards/Certification:\s*([^\n]+)", re.I)
_STOCK_LINE = re.compile(r"In Stock - ([\d,]+) units? available", re.I)
_OUT_OF_STOCK = re.compile(r"\bOut of Stock\b|\bSold Out\b", re.I)
_TABLE = re.compile(r"Available:\s*([\d,]+)(?:;.*?Unit Price:\s*\$([\d,.]+))?", re.I)
_PAGE_PRICE = re.compile(r"(?:US)?\$([\d,]+\.\d{2})\s*/\s*Unit", re.I)
# The page's own currency setting (#main-content data-currency-code="USD", seen
# 2026-11-04): the signed-in page writes "$0.27" only, and "$" alone is no currency
# (prompt 3.4); the 17:26 run's price breaks went out without "$".
_CURRENCY = re.compile(r"^Currency code of the page: ([A-Z]{3})$", re.M)
CURRENCY_LINE = "Currency code of the page: {code}"
_CURRENCY_JS = """() => (document.querySelector('#main-content[data-currency-code]')
  || document.querySelector('[data-currency-code]') || {dataset: {}}).dataset.currencyCode || ''"""
_CURRENCY_HTML = re.compile(r'data-currency-code="([A-Z]{3})"')
_INCREMENT = re.compile(r"(?:Must order|Sold) in increments of ([\d,]+)", re.I)
_BULK_RATE = re.compile(r"Buy ([\d,]+)\s*(?:-\s*([\d,]+)|\+|or more)?\s.*?\$([\d,]+\.\d+)", re.I)


def _plain(number: str) -> str:
    return number.replace(",", "")


def page_facts(product: dict) -> dict:
    """The values one product's page states: moq, tagged_by, qty, price, breaks.
    A key is missing when the page doesn't state it (then the offer has none)."""
    text = product.get("text") or ""
    table = "\n".join(product.get("variants") or [])
    facts: dict = {"breaks": []}
    moq = _MIN_ORDER.search(text) or _MIN_PURCHASE.search(text)
    if moq:
        facts["moq"] = _plain(moq.group(1))
    cert = _INDUSTRY.search(text)
    value = cert.group(1).strip() if cert else ""
    if value and not value.endswith(":"):         # an empty label: not the next label
        facts["tagged_by"] = value
    row = _TABLE.search(table)
    stock = _STOCK_LINE.search(text)
    if row:
        facts["qty"] = _plain(row.group(1)) if _plain(row.group(1)) != "0" else "Out of stock"
        if row.group(2):
            facts["price"] = _plain(row.group(2))
    elif stock:
        facts["qty"] = _plain(stock.group(1))
    elif _OUT_OF_STOCK.search(text):
        facts["qty"] = "Out of stock"
    if "price" not in facts:
        price = _PAGE_PRICE.search(text)
        if price and float(_plain(price.group(1))) > 0:
            facts["price"] = _plain(price.group(1))
    currency = _CURRENCY.search(text)
    if currency:
        facts["currency"] = currency.group(1)
    step = _INCREMENT.search(text)
    if step and int(_plain(step.group(1))) > 1:
        facts["increment"] = _plain(step.group(1))
    facts["bulk_shown"] = BULK_LINK_TEXT in text
    for line in product.get("bulk") or []:
        rate = _BULK_RATE.search(line)
        if rate:
            facts["breaks"].append((_plain(rate.group(1)), _plain(rate.group(2) or ""),
                                    _plain(rate.group(3))))
    return facts


class SearchAddressFailed(RuntimeError):
    """The search address didn't open a page on Monroe's site."""


def search_query(part_numbers: list[str]) -> str:
    """The search_query_adv value: parts joined by ', ', spaces as + and the
    comma as %2C, "/" as %2F (team's example: twd-4%2C+twd-5%2C+3M-2090-3%2F4)."""
    return quote_plus(", ".join(" ".join(p.split()) for p in part_numbers), safe="-")


def _key(value: str) -> str:
    """A part number without spaces, dashes, slashes or dots, upper case."""
    return re.sub(r"[\s\-/.]", "", (value or "").upper())


def _clean_url(url: str) -> str:
    """The product address without the search's tracking query (searchuuid)."""
    return (url or "").split("#")[0].split("?")[0]


class MonroeAdapter(GenericAdapter):
    vendor_id = "monroe"
    display_name = "Monroe Aerospace"
    allowed_hosts = ("monroeaerospace.com",)
    # No built-in login address (team, 2026-11-04): the login page is the `url` in
    # the vendor's secret (sourcing-vendor-monroe), as for the generic vendors.
    default_login_url = ""
    default_part_url_template = SEARCH_URL
    extract_system = MONROE_EXTRACT_SYSTEM
    max_parts_per_search = MAX_PARTS
    default_bulk_batch_size = MAX_PARTS
    default_bulk_min_parts = 2
    # No substitutes (not asked for; Monroe lists none, "Alternate Part Number:
    # Possible" is no part number). show_substitutes: true in the config is ignored
    # by the prompt, which never reads the similar-products section.
    default_show_substitutes = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        for key, selector in KNOWN_SELECTORS.items():
            self.selectors.setdefault(key, selector)
        self.bulk_unavailable: Optional[str] = None
        self._product_text: Optional[str] = None
        # Parts the last search found nowhere, not even searched alone.
        self._absent: set[str] = set()
        # Per product address: what its page states (page_facts), checked against
        # every offer the LLM returns for it (_finish).
        self._facts: dict[str, dict] = {}

    # -- signed in / out ------------------------------------------------------
    async def _account_state(self, page) -> Optional[bool]:
        """True signed in, False signed out, None can't tell. A request with the
        browser's cookies, so the open page (or a person's Live View) is left alone."""
        try:
            resp = await page.request.get(ACCOUNT_URL, max_redirects=0,
                                          timeout=self.action_timeout_ms)
        except Exception as exc:
            logger.info("Monroe account check failed: %r", exc)
            return None
        location = resp.headers.get("location", "")
        redirect = resp.status in (301, 302, 303, 307)
        if redirect and "login.php" in location:
            state = False
        elif resp.status == 200 or (redirect and "account.php" in location):
            # Signed in, BigCommerce sends account.php on to the customer's own page:
            # 302 to account.php?action=order_status (run 2026-11-04 16:20, read as
            # "can't tell", so a good login was tried again and the account suspended).
            state = True
        else:
            state = None
        logger.info("Monroe account check: %s%s -> %s", resp.status,
                    f" to {location}" if location else "",
                    {True: "signed in", False: "signed out", None: "can't tell"}[state])
        return state

    async def _open_login_page(self, page, cred) -> bool:
        """Already signed in (the saved browser context): no login page. Run
        2026-11-04: attempt 2 opened the home page while signed in, followed a link
        to the account's orders page and found no form."""
        if await self._account_state(page) is True:
            logger.info("Login: Monroe account page opens; already signed in")
            self._account_proof()
            return False
        return await super()._open_login_page(page, cred)

    def _account_proof(self) -> None:
        self.signed_in_proof = f"account page {ACCOUNT_URL}"
        self._login_ok = True

    async def _after_submit(self, page, cred, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        """The generic checks (CAPTCHA, lockout, wrong password, 2FA) first; the page
        after a good sign-in is the account page, which they don't know, so the
        account check decides instead of the LLM's "unknown"."""
        text = await self._stop_if_blocked(page)
        if (not await self._password_field(page) and not await self._mfa_input(page)
                and not any(h in text for h in web.MFA_TEXT_HINTS)
                and await self._account_state(page) is True):
            logger.info("Login: signed in (Monroe account page opens; now at %s)", page.url)
            self._account_proof()
            return
        await super()._after_submit(page, cred, totp_tried=totp_tried, code_given=code_given)

    async def is_authenticated(self, page) -> bool:
        state = await self._account_state(page)
        if state is not None:
            return state
        return await super().is_authenticated(page)

    async def logout(self, page) -> bool:
        """BigCommerce's logout address, then the account check confirms it."""
        logger.info("Logout: opening %s", LOGOUT_URL)
        try:
            await page.goto(LOGOUT_URL, wait_until="domcontentloaded")
            await _settle(page)
        except Exception as exc:
            logger.warning("Logout: %s didn't open: %r", LOGOUT_URL, exc)
        state = await self._account_state(page)
        if state is False:
            logger.info("Logout: signed out (now at %s)", page.url)
            return True
        logger.warning("Logout: not confirmed (account check: %s)", state)
        return False

    async def person_finished_logout(self, page) -> bool:
        return await self._account_state(page) is False

    # -- settings ---------------------------------------------------------------
    @property
    def part_url(self) -> str:
        """Vendor record `part_url_template`, else Monroe's search address (never one
        learned from a search box)."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return SEARCH_URL

    @property
    def max_pages(self) -> int:
        return int(self.config.get("results_pages_max") or RESULTS_PAGES_MAX)

    @property
    def product_pages_max(self) -> int:
        return int(self.config.get("product_pages_max") or PRODUCT_PAGES_MAX)

    @property
    def missing_single_max(self) -> int:
        return int(self.config.get("missing_single_max") or MISSING_SINGLE_MAX)

    def _search_url(self, part_numbers: list[str]) -> str:
        template = self.part_url or SEARCH_URL
        return template.replace("{part}", search_query(part_numbers))

    def _fill_condition(self, offers: list) -> list:
        code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN)
        for offer in offers:
            if code and (offer.condition or "").strip().upper() not in CONDITION_CODES:
                offer.condition = code
        return offers

    # -- one part -----------------------------------------------------------
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        if self.extractor is None:
            raise RuntimeError("Monroe search needs an LLM to read the pages")
        self.apply_timeouts(page)
        self._product_text = None
        text, url = await self._gather(page, [row.part_number])
        self._product_text = text
        outcome = await self._read_results(page, row, url)
        self._finish(outcome.offers or [])
        return outcome

    def _finish(self, offers: list) -> list:
        """Every value checked against what that product's page states (team,
        2026-11-04: no random values). The run of 16:53 wrote MOQ 500 from the
        bulk rates, "MFR Certificate" / "Trace To MFR" from the variants filters
        and a per-manufacturer note; the page states MOQ 100 (MS28773-04: 10) and
        "Industry Standards/Certification: MS25036". So: MOQ, tagged_by, stock,
        unit price and price breaks are the page's own; trace, warehouses and a
        lead time other than "In stock" are cleared (Monroe shows none). Then the
        condition Monroe doesn't show (condition_not_shown)."""
        kept = []
        for offer in offers:
            facts = self._facts_for(offer)
            if facts is None:
                logger.warning("Monroe: offer for %s matches no product read (%s); dropped",
                               offer.part_number, offer.source_url)
                continue
            self._match_page(offer, facts)
            kept.append(offer)
        return self._fill_condition(kept)

    def _facts_for(self, offer) -> Optional[dict]:
        """The product an offer is for: its product link, else the only product
        read with its part number."""
        facts = self._facts.get(_clean_url(offer.source_url or ""))
        if facts is not None:
            return facts
        same = [f for f in self._facts.values() if f["sku"] == _key(offer.part_number)]
        return same[0] if len(same) == 1 else None

    @staticmethod
    def _match_page(offer, facts: dict) -> None:
        changed = []

        def put(field: str, value) -> None:
            if (getattr(offer, field) or None) != (value or None):
                changed.append(f"{field} {getattr(offer, field)!r} -> {value!r}")
                setattr(offer, field, value)

        put("moq", facts.get("moq"))
        put("tagged_by", facts.get("tagged_by"))
        put("trace", None)
        put("warehouses", None)
        if (offer.lead_time or "").strip().lower() != "in stock":
            put("lead_time", None)
        if facts.get("qty"):
            put("qty_available", facts["qty"])
        if offer.price and facts.get("price"):
            put("price", facts["price"])           # never a price for an RFQ-only line
        if offer.price and facts.get("currency"):
            put("currency", facts["currency"])
        if offer.price or facts.get("breaks"):
            put("price_breaks", list(facts.get("breaks") or []))
        if facts.get("bulk_shown") and not facts.get("breaks") and offer.price:
            # The page offers bulk rates and none were read: a visible gap.
            flags = [f for f in (offer.review or "").split(",") if f]
            offer.review = ",".join(flags + ["PRICE_BREAKS_NOT_READ"])
        if facts.get("increment"):
            # "Must order in increments of 100": 150 can't be bought (Internal Comments).
            offer.vendor_note = f"SOLD IN INCREMENTS OF {facts['increment']}"
        if changed:
            logger.warning("Monroe %s: set to what the page states: %s", offer.part_number,
                           "; ".join(changed))
            # The values are now the page's own: their "not on page" flags go.
            fixed = {"MOQ", "TAGGED_BY", "TRACE", "WAREHOUSES", "LEAD_TIME", "QTY", "PRICE",
                     "PRICE_BREAKS"}
            flags = [f for f in (offer.review or "").split(",")
                     if f and f.removesuffix("_NOT_ON_PAGE") not in fixed]
            offer.review = ",".join(flags) or None

    async def _results_text(self, page, label: str) -> str:
        if self._product_text is not None:
            text, self._product_text = self._product_text, None
            return text
        return await super()._results_text(page, label)

    # -- search + pick + open -----------------------------------------------------
    async def _open_results(self, page, part_numbers: list[str]) -> str:
        """Opens the search address. Raises SearchAddressFailed when it doesn't open
        a page on Monroe's site."""
        url = self._search_url(part_numbers)
        if not host_allowed(url, self.hosts):
            raise SearchAddressFailed(f"Monroe search address {url} is not on "
                                      f"{', '.join(self.hosts)}")
        logger.info("Monroe step 2 (search): %d part(s): %s", len(part_numbers), url)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        status = response.status if response is not None else 0
        if status >= 400 or not host_allowed(page.url, self.hosts):
            raise SearchAddressFailed(f"Monroe search gave HTTP {status} at {page.url}")
        # The search opens without a login too, with public prices: a session that
        # ran out must not be read as if it were Skylink's.
        if await self._account_state(page) is False:
            raise SessionExpired("Signed out when opening the Monroe search address")
        return page.url.split("#")[0]

    async def _gather(self, page, part_numbers: list[str]) -> tuple[str, str]:
        """Steps 2-4 for these parts: (the text the LLM reads, the search address).
        Sets self._absent: parts no search found, not even searched alone."""
        self._absent = set()
        self._facts = {}
        url = await self._open_results(page, part_numbers)
        picked, total, pages = await self._pick_cards(page, part_numbers)
        found = {_key(c["sku"]) for c in picked}
        missing = [p for p in part_numbers if _key(p) not in found]
        if missing and len(part_numbers) > 1:
            # The exact cards of a part may sit on a page past the ones read: its
            # own search lists it first.
            for part in missing[: self.missing_single_max]:
                logger.info("Monroe: no card for %s in the multi-part search; searching it alone",
                            part)
                await self._open_results(page, [part])
                more, _, _ = await self._pick_cards(page, [part])
                picked += more
                if not more:
                    # Searched on its own and still not listed: not on Monroe. Recorded
                    # as not found, never searched again (no ghost retries).
                    self._absent.add(part)
        elif missing:
            self._absent.update(missing)
        picked = _unique(picked)
        query = ", ".join(part_numbers)
        if not picked:
            return NO_CARD.format(query=query, wanted=", ".join(part_numbers), total=total,
                                  pages=pages), url
        by_part = {p: [c for c in picked if _key(c["sku"]) == _key(p)][: self.product_pages_max]
                   for p in part_numbers}
        products = await self._product_texts(
            page, [c["href"] for cards in by_part.values() for c in cards])
        lines = [HEADER.format(query=query, total=total, pages=pages,
                               picked=f"{len(picked)} card(s) are for a requested part number")]
        n = 0
        for part, cards in by_part.items():
            if not cards:
                lines.append(NO_CARD.format(query=part, wanted=part, total=total, pages=pages))
                continue
            for card in cards:
                n += 1
                link = _clean_url(card["href"])
                lines += [CARD_HEADER.format(n=n, link=link), *_card_lines(card)]
                product = products.get(card["href"]) or {}
                self._facts[link] = {**page_facts(product), "sku": _key(card["sku"])}
                if product.get("text"):
                    lines += [PRODUCT_HEADER.format(n=n, link=link), product["text"]]
                if product.get("variants"):
                    lines += [VARIANTS_HEADER.format(n=n), *product["variants"]]
                if product.get("bulk"):
                    lines += [BULK_HEADER.format(n=n), *product["bulk"]]
        text = "\n".join(lines)
        logger.info("Monroe step 5 (read): %d product(s) for %d part(s), %d chars", n,
                    len(part_numbers), len(text))
        if _dumping():
            _dump("-".join(_key(p) for p in part_numbers)[:60] or "search", text)
        return text, url

    async def _pick_cards(self, page, part_numbers: list[str]) -> tuple[list[dict], str, str]:
        """The cards whose part number is a requested one, over result pages until
        every requested part has one (or max_pages): (cards, the site's count,
        what was read)."""
        wanted = {_key(p) for p in part_numbers if _key(p)}
        picked: list[dict] = []
        total = ""
        pages = 0
        while True:
            pages += 1
            await self._wait_for_results(page)
            total = total or await self._result_count(page)
            picked += [c for c in await self._cards(page) if _key(c["sku"]) in wanted]
            if wanted <= {_key(c["sku"]) for c in picked} or pages >= self.max_pages:
                break
            next_url = await self._next_page(page)
            if not next_url:
                break
            await page.goto(next_url, wait_until="domcontentloaded")
            await _settle(page)
        return picked, total or "result count not shown", f"{pages} results page(s) read"

    async def _result_count(self, page) -> str:
        try:
            heading = await page.locator("h1.page-heading, h1").first.inner_text(timeout=3000)
            return " ".join(heading.split())
        except Exception:
            return ""

    async def _product_texts(self, page, hrefs: list[str]) -> dict[str, dict]:
        """Each product page, by address: {"text", "variants", "bulk"}.

        Opened by its address in its own tab, a few at once (never by clicking a
        card), images and fonts not loaded. The tab waits for the variants table,
        which the page's script draws with the stock (Available 44,350 for
        MS24693-C274, 2026-11-04: not in the HTML), reads its totals, and reads
        the "Bulk Discounts" pop-up (the price breaks). A page whose table didn't
        show gets one more try on its own; then the fetched HTML (no stock number,
        but price, minimum purchase and price breaks) is used."""
        hrefs = [h for h in dict.fromkeys(hrefs) if h and host_allowed(h, self.hosts)
                 and not _NEVER_OPEN.search(urlsplit(h).path)]
        gate = asyncio.Semaphore(PRODUCT_TABS_PARALLEL)

        async def in_tab(href: str) -> Optional[dict]:
            async with gate:
                return await self._product_in_tab(page, href)

        started = time.time()
        read = dict(zip(hrefs, await asyncio.gather(*(in_tab(h) for h in hrefs))))
        again = fetched = 0
        for href in hrefs:
            if not (read[href] or {}).get("variants"):
                # Second try, alone: a slow page under three tabs (no ghost retry:
                # once, and only for a part that IS listed).
                again += 1
                logger.info("Monroe: no variants table read at %s; trying it once more", href)
                read[href] = await self._product_in_tab(page, href) or read[href]
            if not (read[href] or {}).get("text"):
                fetched += 1
                read[href] = {"text": await self._product_from_html(page, href)}
        logger.info("Monroe step 4 (open): %d product page(s) in %.1fs (%d tried again, %d from "
                    "the fetched HTML)", len(hrefs), time.time() - started, again, fetched)
        for value in read.values():
            value["text"] = _product_section(value.get("text") or "")
        return read

    async def _product_in_tab(self, page, href: str) -> Optional[dict]:
        tab = None
        try:
            tab = await page.context.new_page()

            async def skip(route):
                if route.request.resource_type in _SKIP_RESOURCES:
                    await route.abort()
                else:
                    await route.fallback()    # on to any other route, else the network
            await tab.route("**/*", skip)
            self.apply_timeouts(tab)
            await tab.goto(href, wait_until="domcontentloaded")
            if not host_allowed(tab.url, self.hosts):
                logger.warning("Monroe: %s went to %s; not read", href, tab.url)
                return None
            try:
                await tab.wait_for_function(VARIANTS_READY_JS,
                                            timeout=VARIANTS_WAIT_SECONDS * 1000)
            except Exception:
                logger.info("Monroe: variants table not drawn at %s after %ss", href,
                            VARIANTS_WAIT_SECONDS)
            variants, _ = await self._variants(tab)
            text = await asyncio.wait_for(tab.evaluate(PRODUCT_JS), web.PAGE_SCRIPT_TIMEOUT)
            code = await asyncio.wait_for(tab.evaluate(_CURRENCY_JS), web.PAGE_SCRIPT_TIMEOUT)
            if re.fullmatch(r"[A-Z]{3}", code or ""):
                text = CURRENCY_LINE.format(code=code) + "\n" + text
            bulk = await asyncio.wait_for(tab.evaluate(BULK_JS), web.PAGE_SCRIPT_TIMEOUT)
            if not bulk and BULK_LINK_TEXT in text:
                # The page offers bulk rates but the coded read found none (pop-up
                # changed?): the LLM opens it, the same read checks it (assist.py).
                async def rates_shown() -> bool:
                    return bool(await tab.evaluate(BULK_JS))
                if await self.assist.act(tab, key="monroe_bulk_popup", check=rates_shown,
                                         goal="open the 'Bulk Discounts' pop-up that lists the "
                                              "bulk discount rates (Buy N - M and pay only ...)"):
                    bulk = await tab.evaluate(BULK_JS)
            return {"text": text, "variants": variants, "bulk": bulk}
        except Exception as exc:
            logger.warning("Monroe: product page %s not read in a tab: %r", href, exc)
            return None
        finally:
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass

    async def _variants(self, tab) -> tuple[list[str], str]:
        """The variants table's totals for the whole product (every filter at Any):
        Available, Unit of Measure, Unit Price. Its filter choices (Manufacturer,
        Revision, MFR Certificate...) are neither selected nor passed on: the team
        (2026-11-04) saw them turn into values that weren't the product's."""
        table = await asyncio.wait_for(tab.evaluate(VARIANTS_JS), web.PAGE_SCRIPT_TIMEOUT)
        values = (table or {}).get("values") or {}
        if not any(re.search(r"\d", v) for v in values.values()):
            return [], ""
        return ["; ".join(f"{k}: {v}" for k, v in values.items() if v)], ""

    async def _product_from_html(self, page, href: str) -> str:
        """Last way: the page's HTML fetched with the browser's cookies (no stock
        number: the variants table isn't in it), else the page opened in the browser."""
        html = await self._fetch_html(page, href)
        if html:
            try:
                text = await asyncio.wait_for(page.evaluate(PRODUCT_HTML_JS, html),
                                              web.PAGE_SCRIPT_TIMEOUT)
                code = _CURRENCY_HTML.search(html)
                return (CURRENCY_LINE.format(code=code.group(1)) + "\n" + text) if code else text
            except Exception as exc:
                logger.warning("Monroe: product page %s not read from its HTML: %r", href, exc)
        return await self._open_product(page, href)

    async def _fetch_html(self, page, href: str) -> Optional[str]:
        """The page's HTML through the browser's own session (its cookies, so the
        signed-in prices); None when it doesn't come back as a page on Monroe's site."""
        try:
            resp = await page.request.get(href, timeout=self.page_load_timeout_ms)
        except Exception as exc:
            logger.info("Monroe: %s not fetched: %r", href, exc)
            return None
        if resp.status != 200 or not host_allowed(resp.url, self.hosts):
            logger.info("Monroe: %s answered %s at %s", href, resp.status, resp.url)
            return None
        return await resp.text()

    async def _open_product(self, page, href: str) -> str:
        """The slow way: the product page opened in the browser."""
        await page.goto(href, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        try:
            return await asyncio.wait_for(page.evaluate(PRODUCT_JS), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Monroe: product page %s not read: %r", href, exc)
            return ""

    async def _wait_for_results(self, page) -> None:
        try:
            await page.wait_for_function(
                """([sels, none]) => sels.some(s => document.querySelector(s))
                     || new RegExp(none, 'i').test(document.body.innerText)""",
                arg=[list(CARD_SELECTORS), _NO_RESULTS],
                timeout=RESULTS_WAIT_SECONDS * 1000)
        except Exception:
            logger.info("Monroe: no cards and no 'no results' line at %s after %ss", page.url,
                        RESULTS_WAIT_SECONDS)

    async def _cards(self, page) -> list[dict]:
        try:
            return await asyncio.wait_for(
                page.evaluate(CARDS_JS, [list(CARD_SELECTORS), 60]), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Monroe: product cards not read: %r", exc)
            return []

    async def _next_page(self, page) -> Optional[str]:
        try:
            link = page.locator(NEXT_PAGE).first
            if not await link.count():
                return None
            href = urljoin(page.url, await link.get_attribute("href") or "")
        except Exception:
            return None
        if (not href or not host_allowed(href, self.hosts)
                or _NEVER_OPEN.search(urlsplit(href).path)
                or href.split("#")[0] == page.url.split("#")[0]):
            return None
        return href

    # -- several parts --------------------------------------------------------
    async def search_many(self, page, rows: list[RequestRow]):
        if self.bulk_unavailable is not None:
            raise BulkSearchUnavailable(self.bulk_unavailable)
        try:
            return await self._search_many(page, rows)
        except BulkResultsIncomplete:
            raise                       # this batch only; the address itself works
        except BulkSearchUnavailable as exc:
            self.bulk_unavailable = str(exc)
            raise

    async def _search_many(self, page, rows: list[RequestRow]):
        if self.extractor is None:
            raise RuntimeError("Monroe search needs an LLM to read the pages")
        self.apply_timeouts(page)
        try:
            text, url = await self._gather(page, [r.part_number for r in rows])
        except SearchAddressFailed as exc:
            raise BulkSearchUnavailable(str(exc)) from exc
        if any(h in text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(text)
        if len(text) > self.max_page_chars:
            # The pipeline tries half the batch next.
            raise BulkResultsIncomplete(f"Monroe results are {len(text)} chars "
                                        f"(limit {self.max_page_chars})")
        parts = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                  "condition_wanted": r.condition_wanted} for r in rows]
        kind, found, usage = await self.extractor.extract_offers_bulk(
            page_text=text, page_url=url, parts=parts, system=self.extract_system)
        usage.add(self.usage)
        self.usage = type(self.usage)()
        if kind == "login_page":
            raise SessionExpired("Extractor saw a login page")
        if kind == "error":
            raise BulkResultsIncomplete("Could not read the Monroe results (LLM answer "
                                        "missing or cut off)")
        outcomes = {}
        for r in rows:
            offers = self._finish(found.get(r.part_number) or [])
            if offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=url)
                self._search_ok = self._search_ok or has_real_offer(offers)
            elif r.part_number in self._absent:
                # Searched alone too, signed in (checked on every search address):
                # not on Monroe. An outcome here keeps the pipeline from searching it
                # a third time one by one.
                outcomes[r.row_id] = SearchOutcome(found=False, page_url=url)
        logger.info("Monroe multi-part search: offers for %d of %d parts", len(outcomes), len(rows))
        return outcomes, usage


def _unique(cards: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for card in cards:
        key = _clean_url(card["href"]) or card["sku"]
        if key not in seen:
            seen.add(key)
            out.append(card)
    return out


def _card_lines(card: dict) -> list[str]:
    if card["price"] and card["price"].replace("$", "").replace("0", "").strip(" .,") != "":
        price = card["price"]
    else:
        price = "none shown"                  # a hidden or $0.00 price is no price
    lines = [f"Part number: {card['sku']}", f"Name: {card['name']}", f"Price: {price}"]
    if card["stock"]:
        lines.append(f"Stock: {card['stock']}")
    if card["quote"]:
        lines.append("Action: Request Quote")
    elif card["cart"]:
        lines.append("Action: Add to Cart")
    return lines


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _dump(label: str, text: str) -> None:
    """With DEBUG_DUMP_PAGES=1, the whole text the LLM reads."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-monroe-{label}-read.txt"
        path.write_text(text, encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
