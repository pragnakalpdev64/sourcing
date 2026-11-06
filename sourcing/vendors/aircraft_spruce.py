"""AircraftSpruceAdapter: Aircraft Spruce (www.aircraftspruce.com).

From the team's notes after signing in and the public pages seen 2026-11-05
(product and search pages open without a login; what a signed-in page shows
differently is checked on the first run):

  1 sign in   the login page is the secret's `url` (none in the code, as Monroe);
              signed in = the navbar's logout link (team, 2026-11-05), signed out
              = its "Sign In" link (/account.html). The generic login and logout
              do the rest (logout = that navbar link, confirmed by "Sign In")
  2 open      the product page by its address, catalog/pnpages/<part>.php (team's
              examples 03-28010, 03-28130; the address ignores case). A part that
              isn't Aircraft Spruce's own number gives "Page Not Found / Error 404"
              with HTTP 200
  3 search    no product page: the site's search box with the part number. The
              search is a POST (search.php?s=... by GET lists nothing), so the box is
              used. It either opens a product page itself (MS20470AD4-4 opened
              AN470AD-4-4, 2026-11-05: another part number, so `other`) or lists
              cards (.item.filteredItem: title, price, "Part# | MFR Model#", stock);
              the cards whose Part# or MFR Model# is the requested part are opened
  4 describe  still nothing and the row has a Part Description (else Keyword): the
              search box with it (team's example "Solid Rivet Assortment"); the
              cards whose titles share most of its words are opened, at most
              `product_pages_max` (3), and their lines flagged FOUND_BY_DESCRIPTION
  5 quantity  on each product page the requested quantity is typed into the
              Quantity box: the page has no stock number, and its stock line is
              worked out for the quantity in the box (team, 2026-11-05; seen on
              01-08800: qty 1 and 5 "In Stock", qty 50 "Partial Stock" with "6
              Expected to ship ... 44 Backordered"). The price is read before and
              after (the price at the requested quantity, when the account has one)
  6 read      the product's own section per page by Aircraft Spruce's prompt
              (aircraft_spruce_prompt.py); then the values the program read itself
              are set on the line (_finish): price, unit, stock, currency, listing

No multi-part search: the site's "Order by Part Number" adds to the cart, never
used. Never opened: cart, checkout, wishlist, quick shop, reviews, Q&A. A run with
--dump-pages saves the text read (debug_pages/*-aircraft_spruce-*).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote, urlsplit

from ..conditions import CONDITION_CODES
from ..config import captcha_solving_allowed
from ..models import RequestRow, SearchOutcome
from . import web
from .aircraft_spruce_prompt import AIRCRAFT_SPRUCE_EXTRACT_SYSTEM
from .base import MfaRequired, SessionExpired, host_allowed
from .generic import GenericAdapter
from .web import _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.aircraft_spruce")

HOME_URL = "https://www.aircraftspruce.com/"
PRODUCT_URL = "https://www.aircraftspruce.com/catalog/pnpages/{part}.php"
SEARCH_BOX = "#search_text"
CARD_SELECTOR = ".item.filteredItem"
# The sign-in form on /account.html (seen 2026-11-05): "User ID / Dealer #" is
# input#user_email (type text, no label or placeholder). The generic list's
# input[type='email'] matched the footer's newsletter box (#EmailAddress) instead,
# so the first onboarding run typed the email there and left User ID empty. Pinned
# here, over any selector saved from an earlier run.
LOGIN_FORM = "form[name='user_login']"
KNOWN_SELECTORS = {"username_input": f"{LOGIN_FORM} input#user_email",
                   "password_input": f"{LOGIN_FORM} input#user_psw",
                   "submit_button": f"{LOGIN_FORM} button#login_button"}
PRODUCT_PAGES_MAX = 3
RESULTS_WAIT_SECONDS = 15
# The stock line is drawn by the page's script after load (~1s locally, 2026-11-05).
STOCK_WAIT_SECONDS = 10
PRODUCT_TEXT_MAX = 6000
# Where the product's own section ends (then other products, reviews, Q&A).
PRODUCT_END = ("Frequently Purchased With", "Customers Also Viewed")
# Aircraft Spruce shows no condition; the client's RFQ rule quotes no in-stock line
# without one (Wencor, Monroe), so the same default. Vendor config
# `condition_not_shown` changes it ("" leaves it empty).
CONDITION_NOT_SHOWN = "NE"
_NO_RESULTS = r"\b0 search results for"
# Google's image challenge ("select all images with ..."): the invisible reCAPTCHA
# on Sign In shows it when it scores the browser as risky (onboarding, 2026-11-05).
# It is in Google's own frame (recaptcha/api2/bframe), so the page's text never
# says "captcha"; the frame is in the page all along, hidden until it is shown.
CHALLENGE_JS = """() => [...document.querySelectorAll('iframe')].some(f =>
  /recaptcha\\/(api2|enterprise)\\/bframe/.test(f.src || '')
  && getComputedStyle(f).visibility === 'visible' && f.getBoundingClientRect().width > 50)"""
CHALLENGE_WAIT_SECONDS = 6
# DEV_ALLOW_CAPTCHA_SOLVING only: Browserbase's docs say solving takes 5-30s.
SOLVER_WAIT_SECONDS = 45
_NEVER_OPEN = re.compile(r"cart|checkout|wishlist|account|logout|login", re.I)

HEADER = "--- Product {n}: {link} ({how}) ---"
FACTS_HEADER = "--- What the program read on product {n} ---"
TEXT_HEADER = "--- Page text of product {n} ---"
NOTHING = ("--- Aircraft Spruce: no product page for {part} (its address: page not found; "
           "the site's search: {search}) ---")

# The product's own values, read by the program after the page's script ran. The
# stock line and delivery line are the ones beside Part# (the quick-shop pop-up
# has its own, empty ones).
PRODUCT_JS = """() => {
  const clean = t => (t || '').replace(/[ \\t\\u00a0]+/g, ' ').replace(/ *\\n */g, '\\n')
                               .replace(/\\n{2,}/g, '\\n').trim();
  const q = s => document.querySelector(s);
  const own = sel => q('.modelstatus ' + sel)
    || [...document.querySelectorAll(sel)].find(e => !e.closest('#quickshop, .modal, [id^=qs]'));
  const body = document.body.innerText || '';
  const title = clean((q('h1') || {}).innerText);
  return {pn: ((q('#dd-pn') || {}).value || '').trim(),
          model: clean((own('.prModel') || {}).innerText),
          title, price: clean((q('#np') || {}).innerText),
          other_price: clean((q('#gp') || {}).innerText),
          stock: clean((own('.prStockStatus') || {}).innerText),
          delivery: clean((own('.prDelivery') || {}).innerText),
          qty: ((q('#dd-qty') || {}).value || '').trim(),
          currency: (body.match(/Invoice currency ([A-Z]{3})/) || [])[1] || '',
          not_found: !q('#dd-pn') && /Page Not Found|Error 404/.test(body),
          text: clean(body), title_at: title ? clean(body).indexOf(title) : -1};
}"""
STOCK_READY_JS = """() => { const e = document.querySelector('.modelstatus .prStockStatus')
  || document.querySelector('.prShortDesc .prStockStatus');
  return !e || /\\S/.test(e.innerText); }"""
CARDS_JS = """() => [...document.querySelectorAll('.item.filteredItem')].map(card => {
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const a = card.querySelector('.prTitle a[href]');
  const pn = card.querySelector('.prModel [tpl-html="pn"]');
  const mpn = card.querySelector('.prModel [tpl-html="mpn"]');
  const model = clean((card.querySelector('.prModel') || {}).textContent).split('|');
  const shown = clean(card.innerText);
  return {title: clean(a && a.textContent), href: a ? a.href : '',
          pn: clean(pn ? pn.textContent : model[0]), mpn: clean(mpn ? mpn.textContent : (model[1] || '')),
          price: clean((card.querySelector('.prPrice') || {}).textContent),
          series: /Product Series Page/.test(shown) || /^From \\$/.test(
            clean((card.querySelector('.prPrice') || {}).textContent))};
})"""

_PRICE = re.compile(r"\$\s*([\d,]+\.\d{2})\s*(?:/\s*([A-Za-z][A-Za-z .]*))?")
_PART = re.compile(r"Part#\s*(\S+)", re.I)
_MODEL = re.compile(r"MFR Model#\s*(.+)", re.I)
# Rivets are sold by the pound: "86.6 Expected to ship" (AN470AD-4-4, 2026-11-05).
_PARTIAL = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s+Expected to", re.I | re.M)
# Stock words the page's script writes (custom/plugins js, 2026-11-05).
_IN_STOCK = re.compile(r"^In Stock\b", re.I)
_NO_STOCK = re.compile(r"^(No Stock|Special Order|Non-Stocking Product|Made to Order|"
                       r"Pre-Order|Dropship from Supplier|Sent by Email)\b", re.I)


def product_url(part_number: str) -> str:
    return PRODUCT_URL.format(part=quote(" ".join(part_number.split()), safe="-._"))


def _key(value: str) -> str:
    """A part number without spaces, dashes, slashes or dots, upper case."""
    return re.sub(r"[\s\-/.]", "", (value or "").upper())


def _clean_url(url: str) -> str:
    return (url or "").split("#")[0].split("?")[0]


def _plain(number: str) -> str:
    return number.replace(",", "")


def requested_qty(qty_needed: str) -> Optional[float]:
    """The row's quantity ("5", "5.0", "10 EA", "0.25" pounds of rivets); None if none."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?|\.\d+", qty_needed or "")
    if not match:
        return None
    value = float(_plain(match.group(0)))
    return value if value > 0 else None


def qty_text(qty: float) -> str:
    """5.0 -> "5", 0.25 -> "0.25" (as typed into the Quantity box)."""
    return f"{qty:g}"


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1]


def rank_by_description(cards: list[dict], description: str, limit: int) -> list[dict]:
    """The cards whose titles share most of the description's words (at least half),
    best first; series cards (no single price) left out."""
    wanted = set(_words(description))
    if not wanted:
        return []
    scored = []
    for i, card in enumerate(cards):
        if card.get("series") or not card.get("href"):
            continue
        score = len(wanted & set(_words(card.get("title", ""))))
        if score * 2 >= len(wanted):
            scored.append((-score, i, card))
    return [c for _, _, c in sorted(scored, key=lambda s: s[:2])][:limit]


def product_facts(product: dict, qty_typed: Optional[float]) -> dict:
    """The values one product page states, read by the program: part#, MFR model,
    price / unit (at 1 and at the requested quantity), stock, currency."""
    facts: dict = {"pn": product.get("pn") or "", "model": "", "how": product.get("how", "")}
    model_text = product.get("model") or ""
    part = _PART.search(model_text)
    facts["pn"] = facts["pn"] or (part.group(1) if part else "")
    model = _MODEL.search(model_text)
    if model:
        facts["model"] = model.group(1).strip()
    price = _PRICE.search(product.get("price_at_1") or product.get("price") or "")
    if price:
        facts["price"] = _plain(price.group(1))
        if price.group(2):
            facts["uom"] = price.group(2).strip()
    at_qty = _PRICE.search(product.get("price") or "")
    if price and at_qty and qty_typed and qty_typed != 1:
        unit, shown = float(facts["price"]), float(_plain(at_qty.group(1)))
        # A figure that is the unit price times the quantity is a line total, not a price.
        if abs(shown - unit * qty_typed) > 0.005 or shown == unit:
            facts["qty_price"] = _plain(at_qty.group(1))
    if product.get("currency"):
        facts["currency"] = product["currency"]
    stock = (product.get("stock") or "").strip()
    facts["stock"] = stock
    if _IN_STOCK.search(stock):
        facts["qty"] = qty_text(qty_typed) if qty_typed else "In stock"
        if qty_typed:
            facts["note"] = (f"IN STOCK FOR THE REQUESTED QTY {qty_text(qty_typed)} (SITE SHOWS NO "
                             f"STOCK COUNT)")
    elif stock.lower().startswith("partial stock"):
        partial = _PARTIAL.search(product.get("delivery") or "")
        facts["qty"] = _plain(partial.group(1)) if partial else "In stock"
        facts["note"] = (f"PARTIAL STOCK FOR QTY {qty_text(qty_typed or 1)}: "
                         + " / ".join((product.get("delivery") or "").upper().splitlines()))
    elif _NO_STOCK.search(stock):
        facts["qty"] = "Out of stock"
        if not stock.lower().startswith("no stock"):
            facts["note"] = stock.upper()
    return facts


class AircraftSpruceAdapter(GenericAdapter):
    vendor_id = "aircraft_spruce"
    display_name = "Aircraft Spruce"
    allowed_hosts = ("aircraftspruce.com",)
    # No built-in login address: the login page is the `url` in the vendor's secret
    # (sourcing-vendor-aircraft-spruce), as for Monroe.
    default_login_url = ""
    extract_system = AIRCRAFT_SPRUCE_EXTRACT_SYSTEM
    # One part per search (base default): no multi-part search on the site.
    default_show_substitutes = False
    # Its Sign In button runs an invisible reCAPTCHA (data-size="invisible",
    # 2026-11-05): fields typed key by key, as for AAR. Vendor config wins.
    default_human_typing = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.selectors.update(KNOWN_SELECTORS)
        self._product_text: Optional[str] = None
        # Per product address: what its page states (product_facts), set on every
        # offer the LLM returns for it (_finish).
        self._facts: dict[str, dict] = {}

    # -- sign in: the image challenge goes to a person --------------------------------
    async def _after_submit(self, page, cred, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        """After Sign In: Google's image challenge is a person's job, never the
        program's (client rule) and never a retry: each new try makes Google's score
        worse. MfaRequired("captcha") sends it through the Live View handoff
        (a person picks the images; the run carries on once signed in), or stops
        the login with no retry when nobody can be asked."""
        if await self._image_challenge(page) and not await self._solver_cleared(page):
            logger.warning("Login: Google's reCAPTCHA image challenge is shown; asking a person")
            raise MfaRequired("captcha", "Aircraft Spruce shows Google's reCAPTCHA image "
                                         "challenge: a person must pick the images")
        await super()._after_submit(page, cred, totp_tried=totp_tried, code_given=code_given)

    async def _solver_cleared(self, page) -> bool:
        """REMOVE-AFTER-CAPTCHA-TRIAL (2026-11-05): with DEV_ALLOW_CAPTCHA_SOLVING=1 the
        browser service may solve the challenge (Browserbase: 5-30s by its docs).
        True once the challenge is gone; still shown at the end -> a person, as before."""
        if not captcha_solving_allowed():
            return False
        logger.info("Login: image challenge shown; waiting up to %ss for the CAPTCHA solver",
                    SOLVER_WAIT_SECONDS)
        deadline = time.time() + SOLVER_WAIT_SECONDS
        while time.time() < deadline:
            await page.wait_for_timeout(1000)
            try:
                if not await page.evaluate(CHALLENGE_JS):
                    logger.info("Login: the image challenge is gone (solver)")
                    return True
            except Exception:
                return True                   # navigating: the form was sent
        logger.warning("Login: the solver didn't clear the image challenge in %ss",
                       SOLVER_WAIT_SECONDS)
        return False

    async def _image_challenge(self, page) -> bool:
        """The challenge shown, waited for a few seconds while the page stays on the
        sign-in form (it opens ~1-2s after the click)."""
        deadline = time.time() + CHALLENGE_WAIT_SECONDS
        while True:
            try:
                if await page.evaluate(CHALLENGE_JS):
                    return True
                if not await page.locator(KNOWN_SELECTORS["password_input"]).count():
                    return False              # left the form: signed in or an error page
            except Exception:
                return False                  # mid-navigation: not a challenge on this page
            if time.time() >= deadline:
                return False
            await page.wait_for_timeout(500)

    # -- settings -----------------------------------------------------------------
    @property
    def probe_url(self) -> str:
        """The home page: its navbar shows the logout link when signed in."""
        return self.config.get("probe_url") or HOME_URL

    @property
    def product_pages_max(self) -> int:
        return int(self.config.get("product_pages_max") or PRODUCT_PAGES_MAX)

    def _fill_condition(self, offers: list) -> list:
        code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN)
        for offer in offers:
            if code and (offer.condition or "").strip().upper() not in CONDITION_CODES:
                offer.condition = code
        return offers

    # -- one part -------------------------------------------------------------------
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        if self.extractor is None:
            raise RuntimeError("Aircraft Spruce search needs an LLM to read the pages")
        self.apply_timeouts(page)
        self._facts = {}
        text, url, any_product = await self._gather(page, row)
        if not any_product:
            # Nothing to read: no LLM call. Searched signed in (checked on every page).
            logger.info("Aircraft Spruce: %s not found (%s)", row.part_number, text)
            usage, self.usage = self.usage, type(self.usage)()
            self._search_ok = True
            return SearchOutcome(found=False, usage=usage, page_url=url, page_kind="no_results")
        self._product_text = text
        outcome = await self._read_results(page, row, url)
        outcome.offers = self._finish(outcome.offers or [], row)
        outcome.found = bool(outcome.offers)
        return outcome

    async def _results_text(self, page, label: str) -> str:
        if self._product_text is not None:
            text, self._product_text = self._product_text, None
            return text
        return await super()._results_text(page, label)

    async def _gather(self, page, row: RequestRow) -> tuple[str, str, bool]:
        """Steps 2-5: (the text the LLM reads, the address it came from, whether any
        product page was read)."""
        part = row.part_number.strip()
        qty = requested_qty(row.qty_needed)
        products: list[dict] = []
        url = product_url(part)
        found = await self._read_product(page, url, qty, f"opened by its address for {part}")
        searched = ""
        if found:
            products.append(found)
        else:
            products, searched = await self._search_box(page, part, qty, by_part=part)
            url = page.url
            description = (row.part_description or row.keyword or "").strip()
            if not products and description:
                logger.info("Aircraft Spruce: nothing for %s by part number; searching the "
                            "description %r", part, description)
                products, more = await self._search_box(page, description, qty, by_part=None)
                searched += f"; the description {description!r}: {more}"
                url = page.url
        if not products:
            return NOTHING.format(part=part, search=searched or "not run"), url, False
        lines: list[str] = []
        for n, product in enumerate(products, start=1):
            link = _clean_url(product["url"])
            facts = product_facts(product, product.get("qty_typed"))
            self._facts[link] = facts
            lines += [HEADER.format(n=n, link=link, how=product["how"]), FACTS_HEADER.format(n=n),
                      *_fact_lines(product, part), TEXT_HEADER.format(n=n), product["section"]]
        text = "\n".join(lines)
        logger.info("Aircraft Spruce step 6 (read): %d product page(s) for %s, %d chars",
                    len(products), part, len(text))
        if _dumping():
            _dump(_key(part)[:60] or "search", text)
        return text, url, True

    async def _search_box(self, page, term: str, qty: Optional[float], *,
                          by_part: Optional[str]) -> tuple[list[dict], str]:
        """Steps 3 / 4: the site's search box with a part number (by_part) or a
        description: (product pages read, what the search showed)."""
        await self._submit_search(page, term)
        await self._wait_for_results(page)
        if "/catalog/pnpages/" in page.url.lower():
            # The search opened a product page itself.
            how = (f"the site's search for {term} opened this product page" if by_part else
                   f"the site's search for the description {term!r} opened this product page")
            product = await self._read_product(page, None, qty, how)
            return ([product] if product else []), "opened a product page"
        count = await self._result_count(page)
        cards = await self._cards(page)
        if by_part:
            wanted = _key(by_part)
            picked = [c for c in cards if c["href"] and wanted in (_key(c["pn"]), _key(c["mpn"]))]
            picked = picked[: self.product_pages_max]
            how = f"from the site's search for {by_part}: card {{card}}"
        else:
            picked = rank_by_description(cards, term, self.product_pages_max)
            how = (f"from the site's search for the description {term!r} (the part number "
                   f"found nothing): card {{card}}")
        logger.info("Aircraft Spruce step %s (search %r): %s, %d card(s), %d opened",
                    3 if by_part else 4, term, count or "count not shown", len(cards), len(picked))
        products = []
        for card in picked:
            label = " | ".join(v for v in (card["pn"], card["mpn"]) if v)
            product = await self._read_product(page, card["href"], qty,
                                               how.format(card=f"{label} {card['title']!r}"))
            if product:
                products.append(product)
        return products, count or f"{len(cards)} card(s)"

    async def _submit_search(self, page, term: str) -> None:
        """The search box, as a person would: the search is a POST, so no address
        opens it (2026-11-05)."""
        if not await page.locator(f"{SEARCH_BOX}:visible").count():
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)
        box = page.locator(f"{SEARCH_BOX}:visible").first
        await box.fill(term)
        await self._submit_and_wait(page, lambda: box.press("Enter"), "Search")
        await _settle(page)
        if not host_allowed(page.url, self.hosts):
            raise SessionExpired(f"Aircraft Spruce search left the site ({page.url})")
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out on the Aircraft Spruce search page")

    async def _read_product(self, page, href: Optional[str], qty: Optional[float],
                            how: str) -> Optional[dict]:
        """One product page (opened by its address, or the one open when href is
        None): signed in checked, the requested quantity typed, its values read.
        None when it is no product page ("Page Not Found")."""
        if href is not None:
            if not host_allowed(href, self.hosts) or _NEVER_OPEN.search(urlsplit(href).path):
                logger.warning("Aircraft Spruce: %s not opened (not a product address)", href)
                return None
            response = await page.goto(href, wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)
            status = response.status if response is not None else 0
            if status >= 400 or not host_allowed(page.url, self.hosts):
                logger.info("Aircraft Spruce: %s gave HTTP %s at %s", href, status, page.url)
                return None
        # Product pages open without a login too, with public prices.
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out on an Aircraft Spruce product page")
        await self._wait_for_stock(page)
        first = await self._product_values(page)
        if first is None or first["not_found"] or not first["pn"] and not first["price"]:
            logger.info("Aircraft Spruce: no product at %s", page.url)
            return None
        values = first
        if qty and qty != 1 and await page.locator("#dd-qty").count():
            values = await self._type_qty(page, qty) or first
        values["price_at_1"] = first["price"]
        values["qty_typed"] = qty if values is not first or qty == 1 else None
        values["url"], values["how"] = page.url, how
        values["section"] = _product_section(values["text"], values.get("title_at", -1))
        logger.info("Aircraft Spruce: %s at %s: %s, stock for qty %s: %r", values["pn"] or "?",
                    page.url, values["price"] or "no price", qty_text(values["qty_typed"] or 1),
                    values["stock"])
        return values

    async def _type_qty(self, page, qty: float) -> Optional[dict]:
        """The requested quantity in the Quantity box; the page's script redraws the
        stock line (and the price, when the account has one per quantity)."""
        try:
            box = page.locator("#dd-qty").first
            await box.fill(qty_text(qty))
            await box.dispatch_event("input")
            await box.dispatch_event("change")
            await box.press("Tab")
            await page.wait_for_timeout(1200)
            await self._wait_for_stock(page)
            values = await self._product_values(page)
            if values and values["qty"] == qty_text(qty):
                return values
            logger.warning("Aircraft Spruce: quantity %s not kept in the box at %s", qty, page.url)
        except Exception as exc:
            logger.warning("Aircraft Spruce: quantity %s not typed at %s: %r", qty, page.url, exc)
        return None

    async def _product_values(self, page) -> Optional[dict]:
        try:
            return await asyncio.wait_for(page.evaluate(PRODUCT_JS), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Aircraft Spruce: product page %s not read: %r", page.url, exc)
            return None

    async def _wait_for_stock(self, page) -> None:
        try:
            await page.wait_for_function(STOCK_READY_JS, timeout=STOCK_WAIT_SECONDS * 1000)
        except Exception:
            logger.info("Aircraft Spruce: no stock line at %s after %ss", page.url,
                        STOCK_WAIT_SECONDS)

    async def _wait_for_results(self, page) -> None:
        """Cards, "0 search results", or a product page the search opened itself
        (MS20470AD4-4 -> AN470AD-4-4, 2026-11-05). A second navigation (a redirect
        drawn by script) ends the first wait, so it is tried once more."""
        for attempt in range(2):
            try:
                await page.wait_for_function(
                    """([card, none]) => document.querySelector(card)
                         || document.querySelector('#dd-pn')
                         || new RegExp(none, 'i').test(document.body.innerText)""",
                    arg=[CARD_SELECTOR, _NO_RESULTS], timeout=RESULTS_WAIT_SECONDS * 1000)
                return
            except Exception as exc:
                if attempt == 0 and "context was destroyed" in str(exc):
                    await _settle(page)
                    continue
                logger.info("Aircraft Spruce: no cards, product or '0 search results' at %s "
                            "after %ss", page.url, RESULTS_WAIT_SECONDS)
                return

    async def _result_count(self, page) -> str:
        try:
            text = await page.evaluate("() => document.body.innerText")
        except Exception:
            return ""
        match = re.search(r"[\d,]+ search results? for \"[^\"\n]*\"", text or "")
        return match.group(0) if match else ""

    async def _cards(self, page) -> list[dict]:
        try:
            return await asyncio.wait_for(page.evaluate(CARDS_JS), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Aircraft Spruce: result cards not read: %r", exc)
            return []

    # -- the line: what the page states ----------------------------------------------
    def _finish(self, offers: list, row: RequestRow) -> list:
        """The values the program read itself are set on each line (team, 2026-11-04:
        no value the page doesn't state): price at 1 and at the requested quantity,
        unit, stock for the requested quantity, currency; listing and part number
        from the page's Part# / MFR Model#. Trace and warehouses are cleared (the
        site shows none). An offer for no product read is dropped."""
        kept = []
        for offer in offers:
            facts = self._facts_for(offer)
            if facts is None:
                logger.warning("Aircraft Spruce: offer for %s matches no product read (%s); "
                               "dropped", offer.part_number, offer.source_url)
                continue
            self._match_page(offer, facts, row)
            kept.append(offer)
        return self._fill_condition(kept)

    def _facts_for(self, offer) -> Optional[dict]:
        facts = self._facts.get(_clean_url(offer.source_url or ""))
        if facts is not None:
            return facts
        key = _key(offer.part_number)
        same = [f for f in self._facts.values() if key in (_key(f["pn"]), _key(f["model"]))]
        if len(same) == 1:
            return same[0]
        return next(iter(self._facts.values())) if len(self._facts) == 1 else None

    @staticmethod
    def _match_page(offer, facts: dict, row: RequestRow) -> None:
        changed = []

        def put(field: str, value) -> None:
            if (getattr(offer, field) or None) != (value or None):
                changed.append(f"{field} {getattr(offer, field)!r} -> {value!r}")
                setattr(offer, field, value)

        wanted = _key(row.part_number)
        by_part = bool(facts["pn"]) and _key(facts["pn"]) == wanted
        by_model = bool(facts["model"]) and _key(facts["model"]) == wanted
        if by_part or by_model:
            put("listing", "requested")
            # Matched by the manufacturer's number: that is the part asked for; the
            # site's own catalog number goes to Internal Comments.
            put("part_number", facts["pn"] if by_part else facts["model"])
        else:
            # The site's search or the description led here: another part number.
            put("listing", "other")
            if facts["pn"]:
                put("part_number", facts["pn"])
        if facts.get("qty"):
            put("qty_available", facts["qty"])
        if facts.get("price"):
            put("price", facts["price"])
            put("qty_price", facts.get("qty_price"))
        if facts.get("uom"):
            put("uom", facts["uom"])
        if offer.price and facts.get("currency"):
            put("currency", facts["currency"])
        put("trace", None)
        put("warehouses", None)
        notes = [facts["note"]] if facts.get("note") else []
        if by_model and not by_part and facts["pn"]:
            notes.append(f"AIRCRAFT SPRUCE PART# {facts['pn']}")
        offer.vendor_note = "; ".join(notes) or None
        flags = [f for f in (offer.review or "").split(",") if f]
        if "description" in facts.get("how", ""):
            flags.append("FOUND_BY_DESCRIPTION")
        if changed:
            logger.warning("Aircraft Spruce %s: set to what the page states: %s",
                           offer.part_number, "; ".join(changed))
            fixed = {"QTY", "PRICE", "CURRENCY", "UOM", "TRACE", "WAREHOUSES", "PART_NUMBER"}
            flags = [f for f in flags if f.removesuffix("_NOT_ON_PAGE") not in fixed]
        offer.review = ",".join(dict.fromkeys(flags)) or None


def _fact_lines(product: dict, part: str) -> list[str]:
    """The program's own reading of one product page, one value per line."""
    lines = [f"Title: {product['title']}"]
    if product["pn"]:
        lines.append(f"Part#: {product['pn']}")
    model = _MODEL.search(product.get("model") or "")
    if model:
        lines.append(f"MFR Model#: {model.group(1).strip()}")
    keys = {_key(product["pn"]), _key(model.group(1)) if model else ""}
    lines.append(f"Is the requested part {part}: "
                 + ("yes" if _key(part) in keys else "no (another part number)"))
    lines.append(f"Price shown: {product.get('price_at_1') or 'none shown'}")
    typed = product.get("qty_typed")
    if typed and typed != 1:
        lines.append(f"Quantity typed into the Quantity box: {qty_text(typed)}")
        if product.get("price") != product.get("price_at_1"):
            lines.append(f"Price shown for that quantity: {product.get('price') or 'none shown'}")
    if product.get("other_price"):
        lines.append(f"Other price shown beside it: {product['other_price']}")
    qty_label = f"quantity {qty_text(typed or 1)}"
    lines.append(f"Stock line for {qty_label}: {product.get('stock') or 'none shown'}")
    if product.get("delivery"):
        lines.append("Delivery line: " + " / ".join(product["delivery"].splitlines()))
    if product.get("currency"):
        lines.append(f"Page footer: Invoice currency {product['currency']}")
    return lines


def _product_section(text: str, start: int) -> str:
    """The product's own part of the page: from its title to "Frequently Purchased
    With" (the header, menus, other products, reviews and Q&A stay out)."""
    text = text[start:] if start > 0 else text
    cuts = [i for i in (text.find(end) for end in PRODUCT_END) if i > 0]
    return (text[: min(cuts)] if cuts else text)[:PRODUCT_TEXT_MAX].strip()


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _dump(label: str, text: str) -> None:
    """With DEBUG_DUMP_PAGES=1, the whole text the LLM reads."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-aircraft_spruce-{label}-read.txt"
        path.write_text(text, encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
