"""WencorAdapter: Wencor's portal.

The job on Wencor, from the team's description (2026-11-03; the program
hasn't seen the pages yet, so everything below is checked on the first run):

  1 sign in   the login secret's url (fly.wencor.com/log-in): the email and
              password go into the page's own form, then its Log in button.
              The header's Log in button (a drop-down with a second form) is
              never clicked (team, 2026-11-03). TOTP when the secret has one;
              else a person through Live View (sourcing/auth/hitl.py)
  2 search    the dashboard's search bar: type the part number; the parts
              found are listed (a results page, or a list under the bar)
  3 open      every listed part whose part number is the requested one is
              opened: its own link, or a click on the entry when it has none.
              Exactly the same number first; only when none is listed, the
              number written with other spaces, dashes, slashes or dots
              (shared prompt 3.9 counts those as the requested part)
  4 read      per product page: its entry in the search list (the list shows
              values the product page may not, e.g. MOQ), the product page, and
              what its stock link opens (stock per warehouse), all read
              together by Wencor's own prompt (wencor_prompt.py), every value
              checked
  5 finish    the generic logout

Bulk + product pages (team, 2026-11-03): the bulk search shows few values but
it is where Wencor states each part's minimum order quantity ("Quantity entered
is below minimum allowed quantity: 10"; MS9385-09, AS3209-120, NAS1611-121 and
AA55549-13 of the 10-part run; two of them have no minimum anywhere on their
product page). So search_many first runs the batch through the dashboard's
PowerSearch (/search: a Copy and Paste box, one "<part> 1" per line, then
"Click Here to Add To Quick Order Entry"; each row then shows the part, stock,
price and that warning), then reads each part's product page as above; the
part's PowerSearch row goes into its block. Never ADD TO CART. A run with one
part (no bulk) has only the product page's own minimum, when it has one.

"Ships from <warehouse> on <date>" on the product page goes to the line's
Internal Comments (team, 2026-11-03), not the lead time.

Substitutes: OFF for now (team, 2026-11-03, `show_substitutes`). When on: the product page's "Interchanges: A / B / ..."
links (Wencor: "Interchanges / Substitutes will appear below.") are opened,
up to `substitutes_max` (default 10) per part, and each is read as its own
"Substitute product page" block; the LLM marks those lines `substitute` and
the pipeline writes them with the client's substitute comments
(pipeline.alternate_notes). Their stock links aren't opened (time).

Never opened: cart, checkout, quote, order, account or logout links. A run
with --dump-pages saves each product page read (debug_pages/*-wencor-*).
Vendor config `product_pages_max` (default 3) caps the product pages per part.
"""
from __future__ import annotations

import asyncio
import time
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin

from ..conditions import CONDITION_CODES
from ..models import LlmUsage, RequestRow, SearchOutcome
from . import web
from .base import BulkSearchUnavailable, LoginFailed, SessionExpired, host_allowed
from .generic import GenericAdapter
from .web import _settle, accept_cookies
from .wencor_prompt import WENCOR_EXTRACT_SYSTEM

logger = logging.getLogger("sourcing.vendors.wencor")

PRODUCT_PAGES_MAX = 3
# fly.wencor.com/log-in has two login forms (Liferay, seen 2026-11-03): one in
# the header's Log in drop-down (portlet INSTANCE_1234, hidden until opened)
# and the page's own. The header one comes first in the page, so the generic
# lists saw only it (hidden), said "no form", opened the drop-down and signed
# in there; the portal answered with an error the rules didn't know. The
# page's own form is used. Saved like selectors from an earlier run: one that
# stops fitting is skipped.
_PAGE_FORM = "form.login-form:not([id*='_INSTANCE_1234_'])"
# How long the page's own form may take to show (Liferay draws it after load).
FORM_WAIT_SECONDS = 30
KNOWN_SELECTORS = {"username_input": f"{_PAGE_FORM} input[id$='_username']",
                   "password_input": f"{_PAGE_FORM} input[type='password']",
                   "submit_button": f"{_PAGE_FORM} button[type='submit']"}
PAGE_HEADER = "--- Wencor product page {n} of {m}: {url} (listed as {text}) ---"
LIST_ENTRY_HEADER = "--- Its entry in the search list (the list the search showed) ---"
PRODUCT_HEADER = "--- The product page ---"
STOCK_HEADER = ("--- Opened by the program from the product page's stock link '{text}', with "
                "{qty} in the quantity box ---")
ORDER_RULES_HEADER = ("--- The product's order quantity rules (the quantity box's own settings, "
                      "not shown as text; read by the program) ---")
# The minimum and step of a product's quantity box: Wencor shows the minimum
# only there (2026-11-03: AA55549-13 data-min="5", no wording on the page;
# AS3209-120 data-min="100" data-step="100", shown as "Sold in multiples of
# 100."). The product's own box carries data-item = its item number.
ORDER_RULES_JS = """(item) => {
  const box = document.querySelector(`input[data-item='${item}'][data-min]`)
    || document.querySelector('input[data-min]');
  if (!box) return null;
  const n = v => { const x = parseInt(v || '0', 10); return isNaN(x) ? 0 : x; };
  return {min: n(box.dataset.min), step: n(box.dataset.step),
          default: n(box.dataset.defaultquantity)};
}"""
# The product's quantity box next to ADD TO CART (only typed into, never sent).
QTY_BOX = "input#qty"
# Stock links read per product page, how long what they open may take to show.
STOCK_LINKS_MAX = 2
# What the stock link opens is loaded after the click ("Availability for All
# Warehouses", a.availability-trigger, href="#"): 2.5s showed nothing new on
# any page of the 2026-11-03 run, so the program waits for it.
STOCK_WAIT_SECONDS = 10
# The search list is drawn after the page loads: read at once it had no
# entries (local run 2026-11-03, 21FA407G "no listed part" after 0.3s).
LIST_WAIT_SECONDS = 15
# Wencor shows no condition; the client's RFQ rule quotes no in-stock line
# without one (run 2026-11-03: every in-stock Wencor line was "No Quote"), as
# on Aviall. Vendor config `condition_not_shown` changes it ("" leaves it empty).
CONDITION_NOT_SHOWN = "NE"
# The same product listed under two addresses (2026-11-03: its link and
# "product-details#ecom-item/10013439") is one product: keyed by its item number.
_ITEM = re.compile(r"ecom-item/(\d+)")
# The dashboard the login lands on holds PowerSearch (vendor config
# `powersearch_url` overrides).
POWERSEARCH_URL = "https://fly.wencor.com/search"
POWERSEARCH_BOX = "textarea"
POWERSEARCH_BUTTON = ":text('Add To Quick Order Entry')"
# The rows fill in one by one and their minimum-quantity warnings after them:
# at 15s one of the four warnings was still missing (2026-11-03, MS9385-09).
# Read once nothing changed for POWERSEARCH_STABLE_SECONDS, at most this long.
POWERSEARCH_WAIT_SECONDS = 60
POWERSEARCH_STABLE_SECONDS = 6
POWERSEARCH_HEADER = "--- Its row in PowerSearch (the bulk search; read by the program) ---"
# Each PowerSearch row (li.field, saved page 2026-11-03): the part as found
# (h4), Wencor's item number (hidden li.item-number), its warnings
# (.helper-msg.error .error-text) and the preview (stock, price).
POWERSEARCH_ROWS_JS = """() => [...document.querySelectorAll('li.field')].map(li => {
  const h = li.querySelector('.item-preview h4');
  if (!h) return null;
  const num = li.querySelector('li.item-number span:last-child');
  const warnings = [...li.querySelectorAll('.helper-msg .error-text')]
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  const preview = li.querySelector('.item-details-preview');
  return {pn: (h.textContent || '').trim(), item: num ? num.textContent.trim() : '',
          warnings, text: preview ? (preview.innerText || '').trim() : ''};
}).filter(Boolean)"""
_MIN_ALLOWED = re.compile(r"minimum allowed quantity:\s*([\d,]+)", re.I)
_SHIPS_FROM = re.compile(r"^Ships from .+ on .+$", re.M)
# Product pages per bulk batch: PowerSearch once, then each product page
# (~30-50s each, 2026-11-03), inside the pipeline's 600s bulk limit.
BULK_BATCH = 5
# Substitute pages make a part slower (~10-15s each): a bigger limit than the
# pipeline's 600s for one bulk batch (vendor config `bulk_timeout_seconds`).
BULK_TIMEOUT_SECONDS = 900
SUBSTITUTES_MAX = 10
SUBSTITUTE_HEADER = ("--- Substitute product page {n} of {m}: {url} (listed as {text} under "
                     "'Interchanges' on the product page of {asked}; opened by the program) ---")
# The product's own interchange links (.product-information, saved page
# 2026-11-03); the mini cart's and the SPECIFICATIONS table's are plain text.
INTERCHANGES_JS = """() => [...document.querySelectorAll(
    ".product-information .related-items a[href*='ecom-item/']")]
  .map(a => ({href: a.href, text: (a.innerText || '').trim()}))"""
# Visible pop-ups / dialogs, read whole when the stock link opened one.
_POPUPS_JS = """() => [...document.querySelectorAll(
    "[role='dialog'], [aria-modal='true'], .modal, .popover, [class*='popup' i], [class*='tooltip' i]")]
  .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(el => (el.innerText || '').trim()).filter(Boolean)"""
MARK = "data-sourcing-wencor"
# Links that order, change or leave something: never opened from the list.
_NEVER_OPEN = re.compile(r"cart|checkout|quote|order|basket|account|logout|log-out|signout|"
                         r"sign-out|wishlist|compare|delete|remove", re.I)
# List entries whose part number is the requested one. Links first (opened by
# their address); other entries (a drop-down under the search bar may list
# plain items) are marked for a click. loose = ignore spaces, dashes, slashes,
# dots. An entry counts when its whole text, or one of its lines, is the part
# number: a description that only mentions it doesn't.
MATCHES_JS = """([pn, loose, mark]) => {
  const norm = s => (s || '').toUpperCase().replace(/\\s+/g, ' ').trim();
  const key = s => loose ? norm(s).replace(/[\\s\\-\\/\\.]/g, '') : norm(s);
  const want = key(pn);
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const fits = el => {
    const lines = (el.innerText || '').split('\\n').map(x => x.trim()).filter(Boolean);
    return [el.innerText, ...lines, el.title, el.getAttribute('aria-label')]
      .some(c => c && key(c) === want);
  };
  const out = [], seen = new Set();
  let i = 0;
  for (const el of document.querySelectorAll(
      "a[href], [role='option'], [role='link'], li, button")) {
    if (!shown(el) || !fits(el)) continue;
    const link = el.closest('a[href]');
    const href = link ? link.href : '';
    const item = (href.match(/ecom-item\\/(\\d+)/) || [])[1] || href;
    if (item && seen.has(item)) continue;
    if (item) seen.add(item);
    el.setAttribute(mark, String(i));
    // The entry's card: the largest block around it that is about this one
    // product (one product link, not the whole list).
    let card = el;
    for (let up = 0; up < 8 && card.parentElement; up++) {
      const p = card.parentElement;
      const links = new Set([...p.querySelectorAll("a[href*='product-details']")].map(a => a.href));
      if (links.size > 1 || (p.innerText || '').length > 1500) break;
      card = p;
    }
    out.push({i: i++, href, text: (el.innerText || '').split('\\n')[0].trim(),
              entry: (card.innerText || '').replace(/\\n{2,}/g, '\\n').trim().slice(0, 1500)});
  }
  return out;
}"""


# The product page's stock link (team, 2026-11-03: the stock has a link with
# the warehouses): a link or button in the page's main part whose words are
# about stock, or a bare quantity next to such words. Never one that orders,
# changes or leaves something (_NEVER_OPEN), never Log in.
STOCK_LINKS_JS = """([mark, never, max]) => {
  const bad = new RegExp(never, 'i');
  const stocky = /stock|availab|warehouse|location|on hand/i;
  const root = document.querySelector('#main-content') || document.body;
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = [];
  for (const el of root.querySelectorAll("a, button, [role='button']")) {
    if (out.length >= max || !shown(el)) continue;
    const t = (el.innerText || el.title || el.getAttribute('aria-label') || '').trim();
    const href = el.getAttribute('href') || '';
    if (!t || bad.test(t) || bad.test(href) || /log ?in|sign ?in/i.test(t)) continue;
    const around = ((el.parentElement && el.parentElement.innerText) || '').slice(0, 300);
    if (!(stocky.test(t) || (/^[\\d,]+(\\s*[A-Z]{1,3})?$/.test(t) && stocky.test(around)))) continue;
    const id = 's' + out.length;
    el.setAttribute(mark, id);
    out.push({i: id, text: t.slice(0, 60), href});
  }
  return out;
}"""
# The whole page: the warehouse pop-up is added at the end of the body, outside
# #main-content (2026-11-03: "Availability By Warehouse" seen only there).
_LINES_JS = """() => (document.body.innerText || '')
  .split('\\n').map(x => x.trim()).filter(Boolean)"""


class WencorAdapter(GenericAdapter):
    vendor_id = "wencor"
    display_name = "Wencor"
    allowed_hosts = ("wencor.com",)
    extract_system = WENCOR_EXTRACT_SYSTEM
    default_bulk_min_parts = None

    max_parts_per_search = 25
    default_bulk_batch_size = BULK_BATCH
    # No substitutes for Wencor for now (team, 2026-11-03): its interchange pages
    # aren't opened and no substitute line is written. show_substitutes: true
    # in the vendor config turns them back on.
    default_show_substitutes = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        for key, selector in KNOWN_SELECTORS.items():
            self.selectors.setdefault(key, selector)
        self._product_text: Optional[str] = None
        self._dashboard_saved = False
        # PowerSearch rows of the current batch, by part number (upper case).
        self._bulk: dict[str, dict] = {}
        # "Ships from ..." per product page address, for Internal Comments.
        self._ships_from: dict[str, str] = {}
        # Interchange numbers of the part just read, by part number (upper case).
        self._interchanges: dict[str, list[str]] = {}
        self.config.setdefault("bulk_timeout_seconds", BULK_TIMEOUT_SECONDS)

    @property
    def product_pages_max(self) -> int:
        return int(self.config.get("product_pages_max") or PRODUCT_PAGES_MAX)

    async def _open_login_page(self, page, cred) -> bool:
        """The login URL, then a wait for the page's own form. Never the header's
        Log in drop-down: the generic way clicks a Log in button when it sees no
        form, and on Wencor that opens the drop-down's form (2026-11-03)."""
        candidates = self.login_url_candidates(cred)
        if not candidates:
            raise LoginFailed("No usable login URL for Wencor: set the url in its login secret",
                              retryable=False, credentials_sent=False)
        source, url = candidates[0]
        logger.info("Login: opening %s (from %s)", url, source)
        await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if not host_allowed(page.url, self.hosts):
            raise LoginFailed(f"Login URL {url} went to {page.url}, outside "
                              f"{', '.join(self.hosts)}", credentials_sent=False)
        form = f"{_PAGE_FORM} input[type='password']"
        try:
            await page.locator(form).first.wait_for(state="visible",
                                                    timeout=FORM_WAIT_SECONDS * 1000)
        except Exception:
            if await self._logout_link(page):
                logger.info("Login: %s shows a logout link; already signed in", page.url)
                return False
            raise LoginFailed(f"The login page's own form didn't show within "
                              f"{FORM_WAIT_SECONDS}s at {page.url} (the header's Log in "
                              "drop-down is not used)", credentials_sent=False)
        logger.info("Wencor step 1 (sign in): the page's own login form at %s", page.url)
        self._remember_login_page(page, url)
        return True

    async def search_many(self, page, rows: list[RequestRow]):
        """PowerSearch for the batch (each part's minimum order quantity), then each
        part's product page. A part whose search fails is left out: the pipeline
        searches it again one by one."""
        if self.extractor is None:
            raise BulkSearchUnavailable("Wencor search needs an LLM to read the product pages")
        self.apply_timeouts(page)
        self._bulk = await self._powersearch(page, rows)
        outcomes: dict[str, SearchOutcome] = {}
        usage = LlmUsage()
        try:
            for row in rows:
                try:
                    outcome = await self.search(page, row)
                except SessionExpired:
                    raise
                except Exception as exc:
                    logger.warning("Wencor: %s not read in the batch (%s); searched again "
                                   "on its own", row.part_number, str(exc).splitlines()[0][:200])
                    continue
                usage.add(outcome.usage)
                outcomes[row.row_id] = outcome
        finally:
            self._bulk = {}
        return outcomes, usage

    async def _powersearch(self, page, rows: list[RequestRow]) -> dict[str, dict]:
        """The batch in PowerSearch: {part number upper case: row}. Empty when it
        couldn't run (the product pages are still read; only the minimum may be
        missing)."""
        start = (self.config.get("powersearch_url") or self._saved("home_url")
                 or POWERSEARCH_URL)
        if not host_allowed(start, self.hosts):
            return {}
        try:
            await page.goto(start, wait_until="domcontentloaded")
            await _settle(page)
            box = page.locator(POWERSEARCH_BOX).first
            await box.wait_for(state="visible", timeout=LIST_WAIT_SECONDS * 1000)
            # Quantity 1 on every line: Wencor warns "below minimum allowed quantity: N"
            # only when the quantity typed is below N (2026-11-03: MS9385-09 typed with
            # its 20 needed showed nothing; with 1 it shows minimum 10).
            lines = [f"{r.part_number} 1" for r in rows]
            await box.fill("\n".join(lines))
            await page.click(POWERSEARCH_BUTTON, no_wait_after=True,
                             timeout=self.action_timeout_ms)
            logger.info("Wencor step 2 (bulk): %d line(s) in PowerSearch", len(lines))
            found = await self._powersearch_rows(page, len(lines))
            if _dumping():
                await web.page_text(page, "wencor-powersearch")
        except Exception as exc:
            logger.warning("Wencor PowerSearch didn't run (%r); minimums only from the "
                           "product pages", exc)
            return {}
        out = {r["pn"].upper(): r for r in found}
        logger.info("Wencor PowerSearch: %d of %d part(s) listed; minimum order quantity for "
                    "%s", len(out), len(rows), ", ".join(
                        f"{r['pn']} {m.group(1)}" for r in found
                        for m in [_MIN_ALLOWED.search(" ".join(r["warnings"]))] if m) or "none")
        return out

    async def _powersearch_rows(self, page, expected: int) -> list[dict]:
        """The rows once every line has one and nothing changed for a while."""
        deadline = time.monotonic() + POWERSEARCH_WAIT_SECONDS
        last, since = None, time.monotonic()
        rows: list[dict] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            rows = await page.evaluate(POWERSEARCH_ROWS_JS)
            now = repr(rows)
            if now != last:
                last, since = now, time.monotonic()
            elif len(rows) >= expected and time.monotonic() - since >= POWERSEARCH_STABLE_SECONDS:
                break
        return rows

    async def search(self, page, row: RequestRow) -> SearchOutcome:
        """Steps 2-4: the search bar, the listed parts with this part number, their
        product pages. No such part listed: the list itself is read."""
        if self.extractor is None:
            raise RuntimeError("Wencor search needs an LLM to read the product pages")
        self.apply_timeouts(page)
        self._product_text = None
        self._interchanges.pop(row.part_number.strip().upper(), None)
        await self._save_dashboard(page)
        results_url = await self._open_results_by_url(page, row)
        if results_url is None:
            results_url = await self._open_results_by_form(page, row)
        matches = await self._matches(page, row.part_number)
        read_url = results_url
        if matches:
            read_url = await self._read_product_pages(page, row, matches[: self.product_pages_max])
        else:
            logger.info("Wencor: no listed part is %s at %s; reading the list", row.part_number,
                        page.url)
        outcome = await self._read_results(page, row, read_url)
        self._fill_condition(outcome.offers or [])
        for offer in outcome.offers or []:
            note = self._ships_from.get((offer.source_url or read_url).split("#")[0])
            if note:
                offer.vendor_note = note
        return outcome

    def _fill_condition(self, offers: list) -> list:
        code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN)
        for offer in offers:
            if code and (offer.condition or "").strip().upper() not in CONDITION_CODES:
                offer.condition = code
        return offers

    async def _save_dashboard(self, page) -> None:
        """With --dump-pages, the page the login left us on (the dashboard), once:
        to find the bulk search on the signed-in pages."""
        if self._dashboard_saved or not _dumping():
            return
        self._dashboard_saved = True
        try:
            await web.page_text(page, "wencor-dashboard")
        except Exception as exc:
            logger.debug("Dashboard not saved: %r", exc)

    async def _matches(self, page, pn: str) -> list[dict]:
        deadline = asyncio.get_running_loop().time() + LIST_WAIT_SECONDS
        while True:
            found = await self._find_matches(page, pn)
            if found or asyncio.get_running_loop().time() > deadline:
                return found
            await asyncio.sleep(1)

    async def _find_matches(self, page, pn: str) -> list[dict]:
        for loose in (False, True):
            try:
                found = await page.evaluate(MATCHES_JS, [pn, loose, MARK])
            except Exception as exc:
                logger.warning("Wencor: list entries not read: %r", exc)
                return []
            items: set[str] = set()
            unique = []
            for m in found:
                key = (_ITEM.search(m["href"]) or [None, m["href"] or m["i"]])[1]
                if key not in items:
                    items.add(key)
                    unique.append(m)
            found = unique
            found = [m for m in found if not (m["href"] and (
                _NEVER_OPEN.search(m["href"]) or not host_allowed(m["href"], self.hosts)))]
            if found:
                logger.info("Wencor step 3 (open): %d listed part(s) %s %s: %s", len(found),
                            "are" if not loose else "match (spaces/dashes/slashes/dots aside)",
                            pn, "; ".join(m["href"] or f"entry '{m['text']}'" for m in found))
                return found
        return []

    async def _read_product_pages(self, page, row: RequestRow, matches: list[dict]) -> str:
        """Opens each match and keeps its text under its own header; returns the
        first product page's address."""
        texts: list[str] = []
        urls: list[str] = []
        interchanges: list[dict] = []
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", f"wencor-{row.part_number}")
        # An entry without a link is clicked on the list page itself, so it can
        # only be the first one opened; the others need their own address.
        clickable = [m for m in matches if m["href"]] or matches[:1]
        for n, match in enumerate(clickable, 1):
            if match["href"]:
                await page.goto(match["href"], wait_until="domcontentloaded")
            else:
                logger.info("Wencor: clicking list entry '%s'", match["text"])
                await self._submit_and_wait(
                    page, lambda: page.click(f"[{MARK}='{match['i']}']", no_wait_after=True),
                    f"Wencor list entry {match['text']}")
            await _settle(page)
            await accept_cookies(page)
            if await self._signed_out_here(page):
                raise SessionExpired("Signed out when opening a product page")
            url = page.url.split("#")[0]
            urls.append(url)
            rules = await self._order_rules(page, url)
            try:
                interchanges += await page.evaluate(INTERCHANGES_JS)
            except Exception as exc:
                logger.debug("Interchanges not read: %r", exc)
            stock = await self._stock_links(page, url, self._order_qty(row, rules))
            text = await web.page_text(page, f"{label}-{n}")
            block = [PAGE_HEADER.format(n=n, m=len(clickable), url=url,
                                        text=match["text"] or row.part_number)]
            if match.get("entry"):
                block += [LIST_ENTRY_HEADER, match["entry"]]
            bulk = self._bulk.get(row.part_number.strip().upper())
            page_item = (_ITEM.search(url) or [None, ""])[1]
            if bulk and (not bulk["item"] or not page_item or bulk["item"] == page_item):
                block += [POWERSEARCH_HEADER, "\n".join([*bulk["warnings"], bulk["text"]])]
            ships = _SHIPS_FROM.search(text)
            if ships:
                self._ships_from[url] = ships.group(0).strip()
            block += [PRODUCT_HEADER, text]
            if rules:
                block += [ORDER_RULES_HEADER, rules["line"]]
            block += stock
            texts.append("\n".join(block))
            logger.info("Wencor step 4 (read): product page %d of %d at %s", n, len(clickable),
                        url)
        self._interchanges[row.part_number.strip().upper()] = [
            " ".join(link["text"].split()) for link in interchanges if link["text"].strip()]
        if self.show_substitutes:
            texts += await self._substitute_pages(page, row, interchanges, urls, label)
        self._product_text = "\n\n".join(texts)
        return urls[0]

    def listed_alternates(self, row: RequestRow) -> list[str]:
        """The product page's "Interchanges: A / B / ..." as plain numbers for the
        requested part's Internal Comments (substitutes off, team 2026-11-04): no
        page opened, no entry of their own. At most `substitutes_max`."""
        if self.show_substitutes:
            return []
        own = web.loose_key(row.part_number)
        found = [pn for pn in dict.fromkeys(
            self._interchanges.get(row.part_number.strip().upper(), [])) if web.loose_key(pn) != own]
        return found[: int(self.config.get("substitutes_max", SUBSTITUTES_MAX))]

    async def _substitute_pages(self, page, row: RequestRow, links: list[dict],
                                read: list[str], label: str) -> list[str]:
        """Each interchange the product pages link to, once, as its own block."""
        seen = {(_ITEM.search(u) or [None, u])[1] for u in read}
        todo = []
        for link in links:
            item = (_ITEM.search(link["href"]) or [None, link["href"]])[1]
            if item in seen or not host_allowed(link["href"], self.hosts):
                continue
            seen.add(item)
            todo.append(link)
        cap = int(self.config.get("substitutes_max", SUBSTITUTES_MAX))
        if len(todo) > cap:
            logger.info("Wencor: %d interchange(s) of %s; reading the first %d (substitutes_max)",
                        len(todo), row.part_number, cap)
            todo = todo[:cap]
        out = []
        for n, link in enumerate(todo, 1):
            try:
                await page.goto(link["href"], wait_until="domcontentloaded")
                await _settle(page)
                if await self._signed_out_here(page):
                    raise SessionExpired("Signed out when opening a substitute's product page")
                url = page.url.split("#")[0]
                rules = await self._order_rules(page, url)
                text = await web.page_text(page, f"{label}-sub{n}")
            except SessionExpired:
                raise
            except Exception as exc:
                logger.warning("Wencor: substitute %s not read: %r", link["text"], exc)
                continue
            ships = _SHIPS_FROM.search(text)
            if ships:
                self._ships_from[url] = ships.group(0).strip()
            block = [SUBSTITUTE_HEADER.format(n=n, m=len(todo), url=url, text=link["text"],
                                              asked=row.part_number), PRODUCT_HEADER, text]
            if rules:
                block += [ORDER_RULES_HEADER, rules["line"]]
            out.append("\n".join(block))
            logger.info("Wencor: substitute %s of %s read (%s)", link["text"], row.part_number,
                        url)
        return out

    async def _order_rules(self, page, url: str) -> Optional[dict]:
        """The quantity box's minimum and step, as a line for the LLM. None when the
        box sets neither (min / step 0: Wencor's "no rule")."""
        item = (_ITEM.search(url) or [None, ""])[1]
        try:
            box = await page.evaluate(ORDER_RULES_JS, item)
        except Exception as exc:
            logger.debug("Order rules not read: %r", exc)
            return None
        if not box or not (box["min"] > 0 or box["step"] > 1):
            return None
        parts = []
        if box["min"] > 0:
            parts.append(f"Minimum order quantity: {box['min']}")
        if box["step"] > 1:
            parts.append(f"Ordered in multiples of: {box['step']}")
        logger.info("Wencor: order rules on %s: %s", url, "; ".join(parts))
        return {**box, "line": "\n".join(parts)}

    @staticmethod
    def _order_qty(row: RequestRow, rules: Optional[dict]) -> int:
        """The quantity the warehouse pop-up is asked about: the quantity needed, at
        least the product's minimum, rounded up to its multiple."""
        qty = _qty(row.qty_needed)
        if rules:
            qty = max(qty, rules["min"])
            if rules["step"] > 1 and qty % rules["step"]:
                qty += rules["step"] - qty % rules["step"]
        return qty

    async def _stock_links(self, page, url: str, qty: int = 1) -> list[str]:
        """Opens the product page's stock link(s) and returns what each showed
        (the lines that weren't on the page before), under its own header. A link
        that opens another page: that page's text, then back."""
        try:
            links = await page.evaluate(STOCK_LINKS_JS, [MARK, _NEVER_OPEN.pattern,
                                                         STOCK_LINKS_MAX])
        except Exception as exc:
            logger.warning("Wencor: stock links not read: %r", exc)
            return []
        if not links:
            logger.info("Wencor: no stock link on %s", url)
            return []
        out: list[str] = []
        # The pop-up shows each warehouse's availability for the quantity in the
        # box ("1 available on November 3, 2026" with the default 1, 2026-11-03):
        # set to the quantity needed, it says where and when that is available.
        try:
            await page.fill(QTY_BOX, str(qty), timeout=5000)
        except Exception as exc:
            logger.info("Wencor: quantity box not set (%r); the pop-up shows the default", exc)
            qty = await page.input_value(QTY_BOX) if await page.locator(QTY_BOX).count() else 1
        for link in links:
            href = link["href"]
            if href and not href.startswith(("#", "javascript")) and not host_allowed(
                    urljoin(url, href), self.hosts):
                continue
            try:
                before = set(await page.evaluate(_LINES_JS))
                popups_before = set(await page.evaluate(_POPUPS_JS))
                await page.click(f"[{MARK}='{link['i']}']", no_wait_after=True,
                                 timeout=self.action_timeout_ms)
                shown = await self._wait_stock_shown(page, url, before, popups_before)
                if _dumping():
                    await web.page_text(page, "wencor-stock-link")   # saved with it open
                if page.url.split("#")[0] != url:
                    await page.goto(url, wait_until="domcontentloaded")
                    await _settle(page)
                else:
                    await page.keyboard.press("Escape")
            except Exception as exc:
                logger.warning("Wencor: stock link '%s' not read: %r", link["text"], exc)
                continue
            logger.info("Wencor: stock link '%s' showed %d new line(s)", link["text"], len(shown))
            if shown:
                out += [STOCK_HEADER.format(text=link["text"], qty=qty),
                        "\n".join(shown)[:4000]]
        return out

    async def _wait_stock_shown(self, page, url: str, before: set, popups_before: set
                                ) -> list[str]:
        """What the stock link showed, waited for up to STOCK_WAIT_SECONDS: a pop-up
        that wasn't open before (read whole), else the page's new lines, else the
        page it opened. Read once more a second later: it may still be filling."""
        deadline = asyncio.get_running_loop().time() + STOCK_WAIT_SECONDS

        async def look() -> list[str]:
            if page.url.split("#")[0] != url:
                await _settle(page)
                return await page.evaluate(_LINES_JS)
            popups = [p for p in await page.evaluate(_POPUPS_JS) if p not in popups_before]
            if popups:
                return [line for p in popups for line in p.split("\n") if line.strip()]
            return [x for x in await page.evaluate(_LINES_JS) if x not in before]

        shown: list[str] = []
        while not shown and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            shown = await look()
        if shown:
            await asyncio.sleep(1)
            shown = await look() or shown
        return shown

    async def _results_text(self, page, label: str) -> str:
        if self._product_text is not None:
            text, self._product_text = self._product_text, None
            return text
        return await super()._results_text(page, label)


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _qty(value) -> int:
    """Quantity needed as a whole number, 1 when empty or unreadable."""
    try:
        return max(1, int(float(str(value or 1).replace(",", ""))))
    except ValueError:
        return 1
