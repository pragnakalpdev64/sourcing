"""AarAdapter: AAR's parts shop (shop.aarcorp.com, a Magento store).

From the team's description (2026-11-03; the program hasn't seen the pages
yet, so everything below is checked on the first run):

  1 sign in   the landing page (vendor config login_url, else
              shop.aarcorp.com), the avatar at the top right, its "Sign In or
              Register" link, then the generic login on that form (team,
              2026-11-03: the way a person signs in; the login page opened
              directly was refused twice with "reCAPTCHA verification failed.").
              TOTP when the secret has one; else a person through Live View
  2 search    no search form is used: the results address
                one part   catalogsearch/result/?q=<part>
                several    catalogsearch/result/?q=<A>%2C+<B>%2C+<C>
              (spaces in a part number as +, parts joined by ", "; the team's
              examples add the description, "44066+O-RING": the program
              searches the part number only, so a description worded
              differently on AAR doesn't hide the part, and a word like O-RING
              doesn't list every O-ring in a multi-part search)
  3 read      each product card on the results page, every page of results
              (Magento shows 12 per page by default), cards for a requested
              part number first; read by AAR's own prompt (aar_prompt.py),
              every value checked. A search the site sends straight to a
              product page: that page's text
  4 finish    the generic logout

The team calls bulk search "not available": AAR has no bulk form, but the
multi-part address above lists several parts on one page, so search_many
uses it (vendor config `bulk_batch_size`, 0/1 = one part per search).
Some parts show a unit price, others only an RFQ button (team, 2026-11-03).

Never opened: cart, checkout, quote, wishlist, compare, account links. A run
with --dump-pages saves each results page and the text read
(debug_pages/*-aar-*).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote_plus, urljoin, urlsplit

from ..models import RequestRow, SearchOutcome
from . import web
from .aar_prompt import AAR_EXTRACT_SYSTEM
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, LoginFailed, SessionExpired,
    check_login_url, check_rate_limited, has_real_offer, host_allowed,
)
from .generic import GenericAdapter
from .web import _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.aar")

SEARCH_URL = "https://shop.aarcorp.com/catalogsearch/result/?q={part}"
RESULTS_PATH = "/catalogsearch/result/"
# Sign in from the landing page, as a person does (team, 2026-11-03): the
# avatar (span.miniuser-link in div#myLogin) opens a menu whose only entry is
# "Sign In or Register" (-> /customer/account/login). Seen 2026-11-03.
LANDING_URL = "https://shop.aarcorp.com/"
# The icon first: a selector list matches in page order, and the wrapper div's
# centre is not on the icon.
AVATARS = ("#myLogin .miniuser-link", "#myLogin")
SIGN_IN_LINK = "a:text-is('Sign In or Register'), a[href$='/customer/account/login']"
# Signed in or not, without touching the open page (Magento): the account page
# fetched with the browser's cookies answers 200 when signed in and redirects to
# the login page when not. The logout link is only inside the avatar menu, so
# the generic check never saw it: 2026-11-04 a login that went through (no
# CAPTCHA, home page after submit) couldn't be confirmed nor logged out.
ACCOUNT_URL = "https://shop.aarcorp.com/customer/account/"
# Magento's own logout address (a GET signs the session out).
LOGOUT_URL = "https://shop.aarcorp.com/customer/account/logout/"
MENU_WAIT_SECONDS = 10
FORM_WAIT_SECONDS = 30
# The sign-in form's ids (seen 2026-11-03). Saved like selectors from an
# earlier run: one that doesn't fit is skipped.
KNOWN_SELECTORS = {"username_input": "form#login-form input#email",
                   "password_input": "form#login-form input#pass",
                   "submit_button": "form#login-form button#send2"}
# Most parts in one multi-part search: not AAR's limit (none given), kept
# small so one page of cards per part fits the LLM's page limit.
MAX_PARTS = 10
# Result pages read per search (12 cards each by default on Magento).
MAX_PAGES = 5
# Magento renders the cards on the server, but a search add-on (Algolia,
# Klevu, ...) draws them by script: wait for cards or a "no results" line.
RESULTS_WAIT_SECONDS = 15
CARDS_NOTE = ("--- AAR search results for {query}: one block per product card (read from "
              "the page by the program; {pages}) ---")
CARD_HEADER = "--- Card {n}: {link} ---"
NO_CARDS_NOTE = "--- AAR page at {url} (no product cards; the page text) ---"
# Card selectors, most specific first: Magento's own, then common add-ons'.
CARD_SELECTORS = ("li.product-item", ".product-item-info", ".ais-Hits-item",
                  ".klevuProduct", "[class*='product-card']")
_NO_RESULTS = r"returned no results|no results|no products|0 results|nothing found"
# Card text, its product link, and what innerText drops: hover text (title,
# aria-label, data-tooltip) and image alt text (stock / condition icons).
CARDS_JS = """([selectors, max]) => {
  let cards = [];
  for (const s of selectors) {
    cards = [...document.querySelectorAll(s)].filter(el => {
      const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    if (cards.length) break;
  }
  // An outer card (li) holding an inner one (.product-item-info): the outer one.
  cards = cards.filter(c => !cards.some(o => o !== c && o.contains(c))).slice(0, max);
  return cards.map(card => {
    const link = card.querySelector("a.product-item-link, a[href*='.html'], a[href]");
    const extra = [];
    card.querySelectorAll('[title], [aria-label], [data-tooltip], img[alt]').forEach(el => {
      for (const v of [el.getAttribute('title'), el.getAttribute('aria-label'),
                       el.getAttribute('data-tooltip'), el.tagName === 'IMG' ? el.alt : null]) {
        const t = (v || '').replace(/\\s+/g, ' ').trim();
        if (t && !(card.innerText || '').includes(t) && !extra.includes(t)) extra.push(t);
      }
    });
    return {href: link ? link.href : '',
            text: (card.innerText || '').replace(/\\n{2,}/g, '\\n').trim().slice(0, 2500),
            extra: extra.slice(0, 15)};
  });
}"""
NEXT_PAGE = ".pages-item-next a[href], a.action.next[href], a[rel='next'][href]"
# Links the program never follows (a "next page" link can't be one, but checked).
_NEVER_OPEN = re.compile(r"cart|checkout|quote|order|wishlist|compare|account|logout", re.I)


class SearchAddressFailed(RuntimeError):
    """The search address didn't open a page on AAR's site."""


def search_query(part_numbers: list[str]) -> str:
    """The q= value: parts joined by ', ', spaces as + and the comma as %2C
    (team's format, 2026-11-03: "44066+O-RING%2C+1381-1176+RETAINER")."""
    return quote_plus(", ".join(" ".join(p.split()) for p in part_numbers), safe="")


def _key(value: str) -> str:
    """A part number without spaces, dashes, slashes or dots, upper case."""
    return re.sub(r"[\s\-/.]", "", (value or "").upper())


class AarAdapter(GenericAdapter):
    vendor_id = "aar"
    display_name = "AAR"
    allowed_hosts = ("aarcorp.com",)
    default_login_url = LANDING_URL
    default_part_url_template = SEARCH_URL
    extract_system = AAR_EXTRACT_SYSTEM
    max_parts_per_search = MAX_PARTS
    default_bulk_batch_size = MAX_PARTS
    default_bulk_min_parts = 2
    # Off (2026-11-04): proxies aren't in our Browserbase plan (402) and advanced
    # stealth is Enterprise-only (403). The reCAPTCHA failures of 2026-11-03 went
    # away with human typing in a shown local browser; Browserbase without a
    # proxy is tried next. `browser_stealth: true` in the vendor config turns it on.
    default_browser_stealth = False
    # TESTING (2026-11-04): reCAPTCHA failed from local Chromium too (not the IP),
    # so the email and password are typed like a person would.
    default_human_typing = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        for key, selector in KNOWN_SELECTORS.items():
            self.selectors.setdefault(key, selector)
        self.bulk_unavailable: Optional[str] = None
        self._query = ""
        self._requested: list[str] = []

    async def _open_login_page(self, page, cred) -> bool:
        """Landing page -> avatar -> "Sign In or Register" -> the form. Never the
        login page found on an earlier run or the secret's url: they open the
        form directly (vendor config login_url still picks the start page)."""
        start = self.config.get("login_url") or LANDING_URL
        if check_login_url(start, self.hosts):
            raise LoginFailed(f"AAR login URL {start} is not usable: "
                              f"{check_login_url(start, self.hosts)}",
                              retryable=False, credentials_sent=False)
        logger.info("Login: opening %s (AAR landing page)", start)
        await page.goto(start, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if not host_allowed(page.url, self.hosts):
            raise LoginFailed(f"AAR landing page {start} went to {page.url}",
                              credentials_sent=False)
        if not await self._has_form(page):
            if await self._account_state(page) is True:
                logger.info("Login: account page opens; already signed in")
                self._account_proof()
                return False
            await self._avatar_sign_in(page)
        try:
            await page.locator(KNOWN_SELECTORS["password_input"]).first.wait_for(
                state="visible", timeout=FORM_WAIT_SECONDS * 1000)
        except Exception:
            raise LoginFailed(f"AAR sign-in form didn't show at {page.url}",
                              credentials_sent=False)
        logger.info("AAR step 1 (sign in): the sign-in form at %s", page.url)
        return True

    async def _avatar_sign_in(self, page) -> None:
        """Click the avatar, then its "Sign In or Register" link."""
        try:
            avatar = AVATARS[0]
            for candidate in AVATARS:
                if await page.locator(candidate).count():
                    avatar = candidate
                    break
            await page.click(avatar, timeout=self.action_timeout_ms)
            link = page.locator(f"{SIGN_IN_LINK} >> visible=true").first
            await link.wait_for(state="visible", timeout=MENU_WAIT_SECONDS * 1000)
        except Exception as exc:
            raise LoginFailed(f"AAR avatar menu or its Sign In link not found at {page.url}: "
                              f"{exc!r}", credentials_sent=False)
        logger.info("Login: avatar clicked; following 'Sign In or Register'")
        await self._submit_and_wait(page, lambda: link.click(no_wait_after=True),
                                    "AAR Sign In link")
        await _settle(page)
        await accept_cookies(page)
        if not host_allowed(page.url, self.hosts):
            raise LoginFailed(f"AAR Sign In link went to {page.url}", credentials_sent=False)

    async def _account_state(self, page) -> Optional[bool]:
        """True signed in, False signed out, None can't tell. A request with the
        browser's cookies, so the page (or a person's Live View) is left alone."""
        try:
            resp = await page.request.get(ACCOUNT_URL, max_redirects=0,
                                          timeout=self.action_timeout_ms)
        except Exception as exc:
            logger.info("AAR account check failed: %r", exc)
            return None
        location = resp.headers.get("location", "")
        if resp.status in (301, 302, 303, 307) and "/customer/account/login" in location:
            state = False
        elif resp.status == 200:
            state = True
        else:
            state = None
        logger.info("AAR account check: %s%s -> %s", resp.status,
                    f" to {location}" if location else "",
                    {True: "signed in", False: "signed out", None: "can't tell"}[state])
        return state

    def _account_proof(self) -> None:
        self.signed_in_proof = f"account page {ACCOUNT_URL}"
        self._login_ok = True

    async def _after_submit(self, page, cred, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        """The generic checks (CAPTCHA, lockout, wrong password, 2FA) first; AAR's
        home page after a good sign-in shows nothing they know, so the account
        page decides instead of the LLM's "unknown"."""
        text = await self._stop_if_blocked(page)
        if (not await self._password_field(page) and not await self._mfa_input(page)
                and not any(h in text for h in web.MFA_TEXT_HINTS)
                and await self._account_state(page) is True):
            logger.info("Login: signed in (AAR account page opens; now at %s)", page.url)
            self._account_proof()
            return
        await super()._after_submit(page, cred, totp_tried=totp_tried, code_given=code_given)

    async def is_authenticated(self, page) -> bool:
        state = await self._account_state(page)
        if state is not None:
            return state
        return await super().is_authenticated(page)

    async def logout(self, page) -> bool:
        """Magento's logout address, then the account check confirms it."""
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

    @property
    def part_url(self) -> str:
        """Vendor record `part_url_template`, else AAR's search address (never one
        learned from a search box)."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return SEARCH_URL

    @property
    def max_pages(self) -> int:
        return int(self.config.get("results_pages_max") or MAX_PAGES)

    def _search_url(self, part_numbers: list[str]) -> str:
        template = self.part_url or SEARCH_URL
        return template.replace("{part}", search_query(part_numbers))

    # -- one part -----------------------------------------------------------
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        if self.extractor is None:
            raise RuntimeError("AAR search needs an LLM to read the results page")
        self.apply_timeouts(page)
        url = await self._open_results(page, [row.part_number])
        return await self._read_results(page, row, url)

    async def _open_results(self, page, part_numbers: list[str]) -> str:
        """Opens the search address. Raises SearchAddressFailed when it doesn't open
        a page on AAR's site."""
        url = self._search_url(part_numbers)
        if not host_allowed(url, self.hosts):
            raise SearchAddressFailed(f"AAR search address {url} is not on "
                                        f"{', '.join(self.hosts)}")
        logger.info("AAR step 2 (search): %d part(s): %s", len(part_numbers), url)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out when opening the AAR search address")
        status = response.status if response is not None else 0
        if status >= 400 or not host_allowed(page.url, self.hosts):
            raise SearchAddressFailed(f"AAR search gave HTTP {status} at {page.url}")
        self._query = ", ".join(part_numbers)
        self._requested = part_numbers
        return page.url.split("#")[0]

    # -- reading ------------------------------------------------------------
    async def _results_text(self, page, label: str) -> str:
        """The cards of every results page (up to max_pages), cards for a
        requested part number first; the page text when there are no cards (no
        results, or the search went straight to a product page)."""
        label = label.replace("generic-", "aar-")
        await self._wait_for_results(page)
        first_url = page.url.split("#")[0]
        cards: list[dict] = []
        pages = 0
        while True:
            pages += 1
            cards += await self._cards(page)
            next_url = await self._next_page(page)
            if not next_url or pages >= self.max_pages:
                break
            await page.goto(next_url, wait_until="domcontentloaded")
            await _settle(page)
            if await self._signed_out_here(page):
                raise SessionExpired("Signed out when opening the next results page")
            await self._wait_for_results(page)
        if not cards:
            text = await web.page_text(page, label)
            logger.info("AAR: no product cards at %s; reading the page text", page.url)
            return NO_CARDS_NOTE.format(url=page.url.split("#")[0]) + "\n" + text
        more = " - more results pages not read" if next_url else ""
        cards = self._requested_first(cards)
        lines = [CARDS_NOTE.format(query=self._query or "the part",
                                   pages=f"{pages} results page(s) read{more}")]
        for n, card in enumerate(cards, 1):
            lines.append(CARD_HEADER.format(n=n, link=card["href"] or "no product link"))
            lines.append(card["text"])
            if card["extra"]:
                lines.append("(shown on hover or as icon labels: " + "; ".join(card["extra"]) + ")")
        text = "\n".join(lines)
        logger.info("AAR step 3 (read): %d card(s) from %d page(s) starting %s%s", len(cards),
                    pages, first_url, more)
        if more:
            logger.warning("AAR: more than %d results pages for %s", self.max_pages, self._query)
        if _dumping():
            await web.page_text(page, label)       # the last page, saved for redesign
            _dump(label, text)
        return text

    def _requested_first(self, cards: list[dict]) -> list[dict]:
        wanted = [_key(p) for p in self._requested if _key(p)]
        if not wanted:
            return cards

        def rank(card: dict) -> int:
            """0: a line of the card is a requested part number (its name line);
            1: the card mentions one (inside a longer number too: 3880938-1 in
            3880938-10, so only after the exact ones); 2: neither."""
            lines = card["text"].split("\n")
            if any(_key(line) in wanted or _key(line.split(" ")[0]) in wanted
                   for line in lines if line.strip()):
                return 0
            body = _key(card["text"] + " " + card["href"])
            return 1 if any(w in body for w in wanted) else 2
        return sorted(cards, key=rank)      # stable: the site's order within a rank

    async def _wait_for_results(self, page) -> None:
        try:
            await page.wait_for_function(
                """([sels, none]) => sels.some(s => document.querySelector(s))
                     || new RegExp(none, 'i').test(document.body.innerText)""",
                arg=[list(CARD_SELECTORS), _NO_RESULTS],
                timeout=RESULTS_WAIT_SECONDS * 1000)
        except Exception:
            logger.info("AAR: no cards and no 'no results' line at %s after %ss", page.url,
                        RESULTS_WAIT_SECONDS)

    async def _cards(self, page) -> list[dict]:
        try:
            return await asyncio.wait_for(
                page.evaluate(CARDS_JS, [list(CARD_SELECTORS), 60]), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("AAR: product cards not read: %r", exc)
            return []

    async def _next_page(self, page) -> Optional[str]:
        try:
            link = page.locator(NEXT_PAGE).first
            if not await link.count():
                return None
            href = urljoin(page.url, await link.get_attribute("href") or "")
        except Exception:
            return None
        if (not href or not host_allowed(href, self.hosts) or _NEVER_OPEN.search(
                urlsplit(href).path) or href.split("#")[0] == page.url.split("#")[0]):
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
            raise RuntimeError("AAR search needs an LLM to read the results page")
        self.apply_timeouts(page)
        try:
            url = await self._open_results(page, [r.part_number for r in rows])
        except SearchAddressFailed as exc:
            raise BulkSearchUnavailable(str(exc)) from exc
        if urlsplit(page.url).path.lower().rstrip("/") != RESULTS_PATH.rstrip("/"):
            # A product page for one of several parts: the others would look not found.
            raise BulkSearchUnavailable(f"AAR multi-part search opened {page.url}, not a "
                                        "results page")
        text = await self._results_text(page, "aar-bulk")
        if any(h in text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(text)
        if len(text) > self.max_page_chars:
            # The pipeline tries half the batch next.
            raise BulkResultsIncomplete(f"AAR multi-part results are {len(text)} chars "
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
            raise BulkResultsIncomplete("Could not read the AAR multi-part results (LLM "
                                        "answer missing or cut off)")
        outcomes = {}
        for r in rows:
            offers = found.get(r.part_number) or []
            if offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=url)
                self._search_ok = self._search_ok or has_real_offer(offers)
        logger.info("AAR multi-part search: offers for %d of %d parts", len(outcomes), len(rows))
        return outcomes, usage


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _dump(label: str, text: str) -> None:
    """With DEBUG_DUMP_PAGES=1, the whole text the LLM reads."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-aar-read.txt"
        path.write_text(text, encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
