"""GenericAdapter: log in to a vendor portal that has no adapter code, from its
vendor record alone (login_url / website + allowed_hosts). Plan:
docs/GENERIC_VENDOR_PLAN.md, phases 3 (login) and 4 (search).

Every element is found in this order, and the first that works is saved to
`selectors/<vendor>` so later runs skip the rest:
  1. selectors saved by an earlier run
  2. the generic selector lists (web.py)
  3. the LLM, picking from a list of the page's visible inputs/buttons/links
     (metadata only; the model never sees the password and never acts: the
     code checks its answer and does every click and keystroke)

The outcome of a sign-in is decided by rules first. CAPTCHA / lockout wording
always stops the run (client rule: never work around a bot check); the LLM is
asked only when the rules can't tell, and its answer can't override them.
2FA: a TOTP code is typed when the secret has a seed. Any other code (email,
SMS) is asked from a person over Google Chat when the run has a code handoff
(sourcing/auth/hitl.py) and typed in by submit_mfa_code; without one the row
goes NEEDS_ATTENTION. A login with "passwordless" in its secret types only the
username and expects a code page next (Notion, tested 2026-10-22).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..credentials.base import VendorCredential
from ..models import RequestRow, SearchOutcome
from . import web
from .base import (
    CaptchaFailed, InvalidCredentials, LoginBlocked, LoginFailed, MfaRequired, SessionExpired,
    VendorAdapter, check_rate_limited, fill_part_url, has_real_offer, host_allowed, part_context, part_url_template,
)
from .grid import GRID_RECORDS_JS, parse_grid_result
from .web import _css_attr, _first_visible, _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.generic")

# Username page → password page → code page is the longest flow seen on portals.
MAX_LOGIN_STEPS = 3
# LLM calls allowed per adapter (one account, one run) to find elements or read
# an unclear page. Answers are saved, so a normal run makes none.
MAX_DISCOVERY_CALLS = 5
# LLM checks per run of "does this page say the part isn't sold?" for searches
# with no real offer. Each confirmed wording is saved, so later parts match it
# by rule (run 11: Procart's wording had to be added by hand before this).
MAX_NOT_SOLD_CHECKS = 5
# A learned not-sold wording must be at least this long without the part number,
# so it can't match unrelated text.
MIN_NOT_SOLD_PHRASE = 12

_TEXT_INPUT_TYPES = (None, "", "text", "email", "tel")
# A page with only a username box counts as a sign-in page when its address says
# so, or it mentions a password ("Forgot password?"); 2-step logins show the
# password field on the next page. Not "sign in": a home page's header link
# says that too.
LOGIN_PATH = re.compile(r"(log|sign)[-_]?(in|on)|auth|sso|account|/identity")
LOGIN_PAGE_WORDS = ("password",)
# Seconds to look for a sign-in form after following a login link (pop-ups).
FORM_WAIT_SECONDS = 8
_BUTTON_TAGS = ("button", "a")
# New steps the LLM may suggest per part when Enter and the search button didn't
# run the search (each waits up to the action timeout for a page): 4 steps plus
# one last look at the page.
MAX_SEARCH_STEPS = 4
# Time for all the ways to run one part's search; kept under the pipeline's
# 300s per part (SEARCH_TIMEOUT_SECONDS) so a failure is reported, and stops the
# run's other parts, before the pipeline cuts it off. On DASI each Enter that
# did nothing took ~37s (20s for a page + up to 15s for the network to settle).
SEARCH_FORM_BUDGET_SECONDS = 200
# Never clicked on the way to a search, whatever the LLM suggests.
_NOT_A_SEARCH = re.compile(r"delete|remove|cart|checkout|buy|purchase|order|pay|cancel"
                           r"|log ?out|sign ?out|account|profile|password", re.I)
# What the LLM is asked for when no rule finds the search button.
SEARCH_BUTTON_QUESTION = (
    "the button that runs the part number search for the text in the search box: usually "
    "right next to the box, a magnifying-glass icon or a 'Search', 'Go' or 'Find' button. "
    "Not a bulk, multiple-part or advanced search button, and not a filter")
# Never clicked as the account menu, whatever the LLM picks: buttons that change
# something on a signed-in portal, or lead back to a sign-in.
_NOT_A_MENU = re.compile(r"delete|remove|cart|checkout|buy|purchase|order|pay|cancel|submit"
                         r"|log ?in|sign ?in|search", re.I)

# What a confirmed sign-in confirms, and what a search with offers (or a "not
# sold" message) confirms. Only confirmed values are saved for later runs: a
# wrong pick from a failed run (Notion's decoy password box, picked by the LLM
# 2026-10-22) must not be the first thing tried next time.
LOGIN_KEYS = ("username_input", "password_input", "submit_button", "next_button", "login_link",
              "login_page_url", "mfa_code_input", "mfa_submit_button", "logout_link", "home_url",
              "account_menu")
SEARCH_KEYS = ("search_input", "search_button", "part_url_template", "not_sold_phrases")

# Saved selectors are checked against the kind of element they must find.
_TEXT_KEYS = ("username_input", "search_input", "mfa_code_input")
_CLICK_KEYS = ("submit_button", "next_button", "mfa_submit_button", "search_button",
               "login_link", "logout_link", "account_menu")
_KIND_JS = """el => ({tag: el.tagName.toLowerCase(),
  type: (el.getAttribute('type') || '').toLowerCase(), role: el.getAttribute('role'),
  hidden: el.getAttribute('aria-hidden') === 'true', tab: el.tabIndex})"""

# Ids and names a framework generates (React's ":r4:" on Notion's email box,
# random hashes, long numbers): they change with the site's next build.
_GENERATED = re.compile(
    r"[:«»]|^(radix|headlessui|react-aria|mui|downshift|ember|ext-gen|yui_|rc[-_])|\d{5,}"
    r"|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}", re.I)


def looks_generated(value: Optional[str]) -> bool:
    return bool(_GENERATED.search(value or ""))


def _stable(selector: Optional[str]) -> bool:
    """A selector that still works after a reload (not an index mark or nth match)."""
    return bool(selector) and "data-sourcing-el" not in selector and " >> nth=" not in selector


def _fits(key: str, info: dict) -> bool:
    """Whether an element is the kind `key` must find."""
    if info.get("hidden"):
        return False
    tag, kind = info.get("tag"), info.get("type") or ""
    if key == "password_input":
        return tag == "input" and kind == "password" and (info.get("tab") or 0) >= 0
    if key in _TEXT_KEYS:
        return tag == "input" and kind in ("", "text", "email", "tel", "number", "search")
    return (tag in _BUTTON_TAGS or info.get("role") == "button"
            or (tag == "input" and kind in ("submit", "button", "image")))


class GenericAdapter(VendorAdapter):
    vendor_id = "generic"
    display_name = "Vendor"           # the vendor record's display_name replaces it
    default_bulk_min_parts = None     # one part per search; bulk is vendor code
    accepts_mfa_codes = True          # a person's 2FA code can be typed in
    accepts_person_login = True       # a person can finish the login in Live View
    # The extraction prompt: "" = the shared one (extract.EXTRACT_SYSTEM); a vendor
    # adapter built on this one can bring its own (vendors/dasi_prompt.py).
    extract_system = ""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.discovery_calls = 0
        self.not_sold_checks = 0
        # How each element was found this run ("saved" / "generic list" / "LLM" /
        # "vendor record"), for the onboarding report (scripts/onboard_vendor.py).
        self.found_by: dict[str, str] = {}
        # Set by a confirmed sign-in / a search with offers: see confirmed_learned.
        self._login_ok = False
        self._search_ok = False
        self._menu_asked = False          # the LLM is asked for the account menu once per run
        # The frame the sign-in form was found in (a pop-up iframe), None = the page.
        self._login_frame = None
        # Set when no way to run a search was found: the run's other parts fail at
        # once instead of trying the same ways again.
        self.search_failed: Optional[str] = None

    def confirmed_learned(self) -> dict:
        """What this run learned and proved: login values after a confirmed
        sign-in, search values after a search with offers or a "not sold" message."""
        keys = (LOGIN_KEYS if self._login_ok else ()) + (SEARCH_KEYS if self._search_ok else ())
        # A page-action recipe is learned only after its check passed (assist.py).
        return {k: v for k, v in self.learned.items() if k in keys or k.startswith("assist_")}

    def _learn(self, key: str, selector: Optional[str], *, keep_first: bool = False) -> None:
        """Remember a selector for later runs, only if it survives a reload."""
        if not _stable(selector):
            return
        if keep_first:
            self.learned.setdefault(key, selector)
        else:
            self.learned[key] = selector

    def _note(self, key: str, selector: Optional[str], *, saved: Optional[str] = None,
              record: Optional[str] = None) -> Optional[str]:
        if selector and key not in self.found_by:
            self.found_by[key] = ("vendor record" if record and selector == record else
                                  "saved" if saved and selector == saved else "generic list")
        return selector

    # -- finding elements ---------------------------------------------------
    async def _ask(self, call):
        """Run one LLM discovery call within the budget; None when over it or no LLM."""
        if self.extractor is None:
            logger.warning("%s: no LLM configured to find elements on an unknown page",
                           self.display_name)
            return None
        if self.discovery_calls >= MAX_DISCOVERY_CALLS:
            logger.warning("%s: LLM discovery limit (%d calls) reached for this run",
                           self.display_name, MAX_DISCOVERY_CALLS)
            return None
        self.discovery_calls += 1
        result, usage = await call()
        self.usage.add(usage)
        return result

    async def _elements(self, page) -> list[dict]:
        try:
            return await page.evaluate(web._LIST_ELEMENTS_JS)
        except Exception as exc:
            logger.warning("Could not list the page's elements: %r", exc)
            return []

    @staticmethod
    def _selector_for(element: dict) -> tuple[str, Optional[str]]:
        """(selector for this page, selector to save or None). An id or input name
        survives a reload unless a framework generated it; otherwise a button is
        saved by its text and an input by a describing attribute. The index mark
        used on this page only works here."""
        tag, el_id, name = element.get("tag"), element.get("id"), element.get("name")
        if el_id and not looks_generated(el_id):
            selector = _css_attr("id", el_id)
            return selector, selector
        if name and tag == "input" and not looks_generated(name):
            selector = f"input{_css_attr('name', name)}"
            return selector, selector
        mark = _css_attr("data-sourcing-el", str(element["index"]))
        text = (element.get("text") or "").strip()
        kind = (element.get("type") or "").lower()
        if tag == "input" and kind in ("submit", "button") and text:
            return mark, f"input{_css_attr('value', text)}"
        if tag in _BUTTON_TAGS or element.get("role") == "button":
            if text and len(text) <= 40 and "'" not in text:
                base = tag if tag in _BUTTON_TAGS else "[role='button']"
                return mark, f"{base}:has-text('{text}')"
            return mark, None
        if tag == "input":
            for key, attr in (("autocomplete", "autocomplete"), ("aria_label", "aria-label"),
                              ("placeholder", "placeholder")):
                value = (element.get(key) or "").strip()
                if value and value.lower() not in ("on", "off") and len(value) <= 60:
                    return mark, f"input{_css_attr(attr, value)}"
        return mark, None

    def _saved(self, key: str) -> Optional[str]:
        """Found earlier in this run, else saved by an earlier run."""
        return self.learned.get(key) or self.selectors.get(key)

    async def _saved_fit(self, page, key: str) -> Optional[str]:
        """_saved, but only while it still finds the right kind of element (a site
        change can leave an old selector pointing at something else)."""
        saved = self._saved(key)
        if not saved or (key not in _TEXT_KEYS and key not in _CLICK_KEYS
                         and key != "password_input"):
            return saved
        try:
            match = page.locator(saved).first
            if not await match.count():
                return saved                  # not on this page: nothing to check
            info = await match.evaluate(_KIND_JS)
        except Exception:
            return saved
        if _fits(key, info):
            return saved
        logger.warning("Saved %s %s now finds a %s type=%s; not used", key, saved,
                       info.get("tag"), info.get("type") or "-")
        if self.learned.get(key) == saved:
            del self.learned[key]
        return None

    async def _pick(self, page, saved_key: str, generic: list[str]) -> Optional[str]:
        """Steps 1 and 2: a saved selector (if it still fits), then the generic list."""
        saved = await self._saved_fit(page, saved_key)
        return self._note(saved_key, await _first_visible(page, [saved, *generic]), saved=saved)

    async def _pick_button(self, page, saved_key: str, generic: list[str]) -> Optional[str]:
        """_pick for a button to click: never one that signs in with another
        account ("Continue with Google" next to the form's own "Continue")."""
        saved = await self._saved_fit(page, saved_key)
        return self._note(saved_key, await web.first_visible_button(page, [saved, *generic]),
                          saved=saved)

    async def _llm_login_fields(self, page) -> dict[str, Optional[str]]:
        """Step 3 for the sign-in form: ask the LLM, then check every answer."""
        elements = await self._elements(page)
        if not elements:
            return {}
        picked = await self._ask(lambda: self.extractor.pick_login_fields(elements))
        if not picked:
            return {}
        out: dict[str, Optional[str]] = {}
        for key, index in picked.items():
            if index is None:
                continue
            el = elements[index]
            tag, kind = el.get("tag"), (el.get("type") or "").lower() or None
            if el.get("role") == "button" and tag != "input":
                tag = "button"            # <div role="button"> (Notion's Continue)
            ok = {
                "password": tag == "input" and kind == "password",
                "username": tag == "input" and kind in _TEXT_INPUT_TYPES,
                "submit": tag in _BUTTON_TAGS or (tag == "input" and kind in ("submit", "button", "image"))
                          or el.get("type") == "submit",
                "next": tag in _BUTTON_TAGS or (tag == "input" and kind in ("submit", "button")),
            }.get(key, False)
            if not ok:
                logger.warning("LLM picked element %d as %s, but it is a %s type=%s; not used",
                               index, key, tag, kind)
                continue
            selector, save = self._selector_for(el)
            learned_key = {"username": "username_input", "password": "password_input",
                           "submit": "submit_button", "next": "next_button"}[key]
            logger.info("LLM picked %s: %s, saved as %s (%s)", key, selector, save,
                        json.dumps(el)[:200])
            self.found_by.setdefault(learned_key, "LLM")
            out[key] = selector
            self._learn(learned_key, save)
        return out

    @staticmethod
    def _clickable(el: dict) -> bool:
        """A link, a button, or a button-like input (never a text box)."""
        if el.get("tag") == "input":
            return (el.get("type") or "").lower() in ("submit", "button", "image")
        return True              # a, button, [role=button]

    async def _llm_pick_one(self, page, what: str, learned_key: str) -> Optional[str]:
        """Ask the LLM for one link or button to click; anything else is refused."""
        elements = await self._elements(page)
        if not elements:
            return None
        index = await self._ask(lambda: self.extractor.pick_element(elements, what))
        if index is None:
            return None
        el = elements[index]
        if not self._clickable(el):
            logger.warning("LLM picked element %d as %s, but it is a %s type=%s; not used",
                           index, learned_key, el.get("tag"), el.get("type"))
            return None
        selector, save = self._selector_for(el)
        logger.info("LLM picked %s: %s, saved as %s (%s)", learned_key, selector, save,
                    json.dumps(el)[:200])
        self.found_by.setdefault(learned_key, "LLM")
        self._learn(learned_key, save)
        return selector

    # -- page state ---------------------------------------------------------
    async def _password_field(self, page) -> Optional[str]:
        return await self._pick(page, "password_input", web.PASSWORD_SELECTORS)

    async def _logout_link(self, page) -> Optional[str]:
        marker, saved = (self.config.get("logged_in_selector"),
                         await self._saved_fit(page, "logout_link"))
        return self._note("logout_link", await _first_visible(
            page, [marker, saved, *web.LOGOUT_SELECTORS]), saved=saved, record=marker)

    async def _logout_in_menu(self, page) -> Optional[str]:
        """The logout link when it is inside a menu (profile avatar, account or
        user name; DASI 2026-10-29): open the menu and look again. The menu is the
        saved one, then the generic list, then the LLM's pick (once per run). The
        menu is left open for the caller to click logout or close it."""
        saved = await self._saved_fit(page, "account_menu")
        menu = self._note("account_menu", await web.first_visible_button(
            page, [saved, *web.ACCOUNT_MENU_SELECTORS]), saved=saved)
        if menu and await self._menu_words_ok(page, menu):
            link = await self._open_menu(page, menu, menu)
            if link:
                return link
        if self._menu_asked:
            return None
        self._menu_asked = True
        elements = await self._elements(page)
        if not elements:
            return None
        index = await self._ask(lambda: self.extractor.pick_element(
            elements, "the profile, avatar, account or user-name button that opens the menu "
                      "holding the log out / sign out link"))
        if index is None:
            return None
        el = elements[index]
        words = " ".join(str(el.get(k) or "") for k in ("text", "aria_label", "title", "id"))
        href = el.get("href") or ""
        if (not self._clickable(el) or el.get("tag") == "input" or _NOT_A_MENU.search(words)
                or (href.startswith("http") and not host_allowed(href, self.hosts))):
            logger.warning("LLM picked element %d as the account menu, but it is a %s %r; "
                           "not clicked", index, el.get("tag"), words.strip()[:80])
            return None
        selector, save = self._selector_for(el)
        logger.info("LLM picked account_menu: %s, saved as %s (%s)", selector, save,
                    json.dumps(el)[:200])
        link = await self._open_menu(page, selector, save)
        if link:
            self.found_by.setdefault("account_menu", "LLM")
        return link

    async def _menu_words_ok(self, page, menu: str) -> bool:
        """A list or saved match whose words say it changes something ("Delete
        account" matches aria-label*='account') is never clicked."""
        try:
            words = await page.locator(menu).first.evaluate(
                "el => [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'),"
                " el.id].filter(Boolean).join(' ')")
        except Exception:
            return False
        if _NOT_A_MENU.search(words or ""):
            logger.info("Account menu %s: %r is not a menu; not clicked", menu, words[:80])
            return False
        return True

    async def _open_menu(self, page, menu: str, save: Optional[str]) -> Optional[str]:
        """Click a menu button and return the logout link it shows (else None,
        with the menu closed again). The menu is saved only when it held one."""
        try:
            await page.click(menu, timeout=5000)
        except Exception as exc:
            logger.info("Account menu %s: could not click it (%s)", menu, str(exc).splitlines()[0])
            return None
        await _settle(page, 3000)
        if not host_allowed(page.url, self.hosts):
            raise LoginFailed(f"The account menu {menu} led off the vendor's site ({page.url})",
                              retryable=False)
        link = await _first_visible(page, [await self._saved_fit(page, "logout_link"),
                                           *web.LOGOUT_SELECTORS])
        if not link:
            logger.info("Account menu %s: no logout link in it", menu)
            await page.keyboard.press("Escape")
            return None
        logger.info("Account menu %s: holds %s", menu, link)
        self._learn("account_menu", save)
        self.found_by.setdefault("logout_link", "account menu")
        return link

    async def _close_menu(self, page) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    async def _login_link(self, page) -> Optional[str]:
        marker, saved = (self.config.get("logged_out_selector"),
                         await self._saved_fit(page, "login_link"))
        return self._note("login_link", await _first_visible(
            page, [marker, saved, *web.LOGIN_LINK_SELECTORS]), saved=saved, record=marker)

    @property
    def probe_url(self) -> str:
        return (self.config.get("probe_url") or self._saved("home_url")
                or self.config.get("website") or "")

    async def is_authenticated(self, page) -> bool:
        """Signed in only on positive proof (a logout link, or the LLM reading a
        page with neither a logout nor a login link): an unsure answer makes the
        pipeline log in again, never search signed out (no false NOT_FOUND)."""
        self.apply_timeouts(page)
        url = self.probe_url
        if not url or not host_allowed(url, self.hosts):
            logger.info("Auth probe: no page to check on %s yet -> not signed in",
                        ", ".join(self.hosts) or "(no hosts)")
            return False
        await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._password_field(page):
            logger.info("Auth probe %s: sign-in form shown -> not signed in", page.url)
            return False
        logout = await self._logout_link(page)
        if logout:
            logger.info("Auth probe %s: %s visible -> signed in", page.url, logout)
            self._learn("logout_link", logout, keep_first=True)
            self._login_ok = True
            return True
        if await self._login_link(page):
            logger.info("Auth probe %s: Login link shown -> not signed in", page.url)
            return False
        logout = await self._logout_in_menu(page)
        if logout:
            await self._close_menu(page)
            logger.info("Auth probe %s: %s in the account menu -> signed in", page.url, logout)
            self._login_ok = True
            return True
        # Neither link: some portals have no logout link on every page. The LLM
        # reads the page; anything but a clear "signed_in" counts as signed out.
        text = await web._body_text(page)
        state = await self._ask(lambda: self.extractor.classify_login_page(text, page.url))
        logger.info("Auth probe %s: no logout or login link; LLM says %s", page.url, state)
        self._login_ok = self._login_ok or state == "signed_in"
        return state == "signed_in"

    # -- login --------------------------------------------------------------
    async def _type_login_field(self, page, selector: str, value: str, target=None) -> None:
        """Fill a login field. With the vendor setting human_typing (AAR, testing
        2026-11-04: its invisible reCAPTCHA failed after an instant fill), move the
        mouse to the field, click it and type one character at a time with uneven
        gaps, the way a person would. The value is never logged."""
        target = target or page            # the frame holding the form (_form_frame)
        if not self.human_typing:
            await target.fill(selector, value)
            return
        field = target.locator(selector).first
        box = await field.bounding_box()
        if box:
            await page.mouse.move(box["x"] + box["width"] * random.uniform(0.2, 0.7),
                                  box["y"] + box["height"] * random.uniform(0.3, 0.7),
                                  steps=random.randint(8, 20))
        await asyncio.sleep(random.uniform(0.2, 0.6))
        await field.click()
        await field.fill("")
        for ch in value:
            await page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(0.06, 0.22))
        await asyncio.sleep(random.uniform(0.3, 0.9))

    async def login(self, page, cred: VendorCredential) -> None:
        self.apply_timeouts(page)
        self.signed_in_proof = None       # from an earlier attempt: proves nothing now
        self.login_claimed = False
        self._login_frame = None
        logger.info("Login: %s as %s", self.display_name, cred.username)
        try:
            await self._login(page, cred)
        except PlaywrightTimeoutError as exc:
            first_line = str(exc).splitlines()[0]
            logger.warning("Login: timed out at %s: %s", page.url, first_line)
            raise LoginFailed(f"Timed out at {page.url}: {first_line}") from exc

    async def _open_login_page(self, page, cred: VendorCredential) -> bool:
        """Get to a page with a sign-in form (a username or password field). False
        when the start page turns out to be signed in already."""
        candidates = self.login_url_candidates(cred)
        if not candidates:
            # A setup problem: trying again changes nothing.
            raise LoginFailed(f"No usable login URL for {self.display_name}: set login_url or "
                              "website (on one of allowed_hosts) in its vendor record",
                              retryable=False, credentials_sent=False)
        tried = []
        for source, url in candidates:
            logger.info("Login: opening %s (from %s)", url, source)
            await page.goto(url, wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)   # before the form: some consent buttons reload the page
            if not host_allowed(page.url, self.hosts):
                logger.warning("Login: %s went to %s, outside %s; not used", url, page.url,
                               ", ".join(self.hosts))
                tried.append(f"{url} (off-site)")
                continue
            if await self._has_form(page):
                self._remember_login_page(page, url)
                return True
            logout = await self._logout_link(page)
            if logout:
                logger.info("Login: %s shows a logout link; already signed in", page.url)
                self._proven(page, logout)
                return False
            link = (await self._login_link(page)
                    or await self._llm_pick_one(page, "the link or button that opens the "
                                                      "sign-in / log-in page", "login_link"))
            if link:
                logger.info("Login: no form at %s; following %s", page.url, link)
                await self._submit_and_wait(page, lambda: page.click(link, no_wait_after=True),
                                            "Login link")
                await accept_cookies(page)
                if not host_allowed(page.url, self.hosts):
                    logger.warning("Login: login link led to %s, outside %s; not used",
                                   page.url, ", ".join(self.hosts))
                    tried.append(f"{url} (login link went off-site)")
                    continue
                if await self._wait_for_form(page):
                    self._remember_login_page(page, url)
                    return True
                if await self._llm_login_link(page, url, link):
                    self._remember_login_page(page, url)
                    return True
            tried.append(url)
        raise LoginFailed("No sign-in form found (tried: " + "; ".join(tried) + ")",
                          credentials_sent=False)

    async def _has_form(self, page) -> bool:
        """The page, or a frame in it, shows a sign-in form (_form_frame)."""
        return await self._form_frame(page) is not None

    async def _form_in(self, target, page) -> bool:
        """A password field, or a username field on a page that is about signing in.
        A lone email box elsewhere is usually a newsletter sign-up: typing the
        username there and pressing its button would be wrong."""
        if await self._password_field(target):
            return True
        if not await self._pick(target, "username_input", web.USERNAME_SELECTORS):
            return False
        path = page.url.split("?")[0].lower()
        if LOGIN_PATH.search(path):
            return True
        text = await web._body_text(target)
        return any(word in text for word in LOGIN_PAGE_WORDS)

    async def _form_frame(self, page):
        """Where the sign-in form is: the page itself, else a shown frame on the
        vendor's site. Monroe (2026-11-04): its Sign In opens a pop-up drawn in an
        about:blank iframe; the page itself has no field, so the run said "No
        sign-in form found" with the form on screen. None = no form anywhere."""
        if await self._form_in(page, page):
            self._login_frame = None
            return page
        for frame in getattr(page, "frames", [])[1:]:
            if not await self._frame_usable(page, frame):
                continue
            if await self._form_in(frame, page):
                logger.info("Login: the sign-in form is inside a frame (%s) on %s",
                            frame.url or "about:blank", page.url)
                self._login_frame = frame
                return frame
        return None

    async def _frame_usable(self, page, frame) -> bool:
        """A frame credentials may be typed into: shown, and on the vendor's site. A
        frame without its own address (about:blank / srcdoc, drawn by the page's
        script) belongs to the page, so the page's address decides."""
        try:
            if frame.is_detached():
                return False
            owner = frame
            while owner is not None and (owner.url or "").split(":", 1)[0] not in ("http", "https"):
                owner = owner.parent_frame       # about:blank / srcdoc: whoever drew it
            if not host_allowed(owner.url if owner is not None else page.url, self.hosts):
                return False
            element = await frame.frame_element()
            return await element.is_visible()
        except Exception:
            return False

    async def _wait_for_form(self, page, seconds: float = FORM_WAIT_SECONDS) -> bool:
        """A sign-in pop-up is drawn after the click (Monroe's iframe ~1-2s, 2026-11-04):
        look again for a few seconds before saying there is none."""
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            if await self._has_form(page):
                return True
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(1)

    async def _login_text(self, page) -> str:
        """Body text of the page and of the frame the form was in (lower case): a
        wrong-password or CAPTCHA line in a pop-up frame is not in the page's text."""
        text = await web._body_text(page)
        frame = self._login_frame
        if frame is not None and await self._frame_usable(page, frame):
            text += "\n" + await web._body_text(frame)
        return text

    async def _llm_login_link(self, page, url: str, tried_link: str) -> bool:
        """The rules' login link led to no form (Monroe 2026-11-04: "a:has-text('Login')"
        was "Forgot Login"): back to the start page, the LLM picks the link once."""
        if self.extractor is None or self.found_by.get("login_link") == "LLM":
            return False
        await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        link = await self._llm_pick_one(page, "the link or button that opens the sign-in / "
                                              "log-in form (not 'forgot password' or "
                                              "'register')", "login_link")
        if not link or link == tried_link:
            return False
        logger.info("Login: %s showed no form; following the LLM's pick %s", tried_link, link)
        await self._submit_and_wait(page, lambda: page.click(link, no_wait_after=True),
                                    "Login link (LLM)")
        await accept_cookies(page)
        return host_allowed(page.url, self.hosts) and await self._wait_for_form(page)

    def _remember_login_page(self, page, start_url: str) -> None:
        if page.url.split("?")[0] != start_url.split("?")[0]:
            self.learned["login_page_url"] = page.url.split("#")[0]

    async def _login(self, page, cred: VendorCredential) -> None:
        if not await self._open_login_page(page, cred):
            return
        username_done = False
        for step in range(1, MAX_LOGIN_STEPS + 1):
            # Last check before typing anything: never send credentials off-site.
            if not host_allowed(page.url, self.hosts):
                raise LoginBlocked(f"Refusing to enter credentials on {page.url}")
            # The page, or the frame the form is drawn in (a sign-in pop-up).
            form = await self._form_frame(page) or page
            if form is not page and not await self._frame_usable(page, form):
                raise LoginBlocked(f"Refusing to enter credentials in frame {form.url}")
            fields = {
                "username": None if username_done else
                await self._pick(form, "username_input", web.USERNAME_SELECTORS),
                "password": await self._password_field(form),
            }
            if (not username_done and not fields["username"]) or not fields["password"]:
                if not fields["password"] and fields["username"] and await self._pick_button(
                        form, "next_button", web.NEXT_SELECTORS):
                    pass                      # two-step login: username + Next, no LLM needed
                else:
                    asked = await self._llm_login_fields(form)
                    fields = {k: fields.get(k) or asked.get(k)
                              for k in ("username", "password", "submit", "next")}
            logger.info("Login step %d at %s: username=%s password=%s", step, page.url,
                        fields.get("username") or ("(done)" if username_done else None),
                        fields.get("password"))

            if cred.passwordless and fields.get("password"):
                raise self._passwordless_error(page)
            if fields.get("password") and not username_done and not fields.get("username"):
                # Never send a password without the username it belongs to.
                raise LoginFailed(f"No username field could be identified at {page.url}",
                                  credentials_sent=False)
            if fields.get("username") and not username_done:
                await self._type_login_field(page, fields["username"], cred.username, form)
                username_done = True
                self._learn("username_input", fields["username"], keep_first=True)
            if fields.get("password"):
                await self._type_login_field(page, fields["password"], cred.password, form)
                self._learn("password_input", fields["password"], keep_first=True)
                submit = (fields.get("submit")
                          or await self._pick_button(form, "submit_button", web.SUBMIT_SELECTORS))
                if self.human_typing:
                    await asyncio.sleep(random.uniform(0.6, 1.5))
                if submit:
                    logger.info("Login: submitting with %s", submit)
                    await self._submit_and_wait(
                        page, lambda: form.click(submit, no_wait_after=True), "Login submit")
                else:
                    logger.info("Login: no submit button found; pressing Enter")
                    await self._submit_and_wait(
                        page, lambda: form.press(fields["password"], "Enter", no_wait_after=True),
                        "Login submit")
                await _settle(page)
                await self._after_submit(page, cred, totp_tried=False)
                return
            if username_done:
                # Username typed; a password or code page comes next.
                nxt = (fields.get("next") or (cred.passwordless and fields.get("submit"))
                       or await self._pick_button(form, "next_button", web.NEXT_SELECTORS))
                if not nxt:
                    raise LoginFailed(f"Username typed but no password field or Next button "
                                      f"at {page.url}", credentials_sent=False)
                logger.info("Login: two-step sign-in; continuing with %s", nxt)
                self._learn("next_button", nxt, keep_first=True)
                await self._submit_and_wait(page, lambda: form.click(nxt, no_wait_after=True),
                                            "Login next")
                await _settle(page)
                await self._stop_if_blocked(page)
                # Some portals send a code instead of asking for a password.
                if cred.passwordless or await self._mfa_input(page):
                    if cred.passwordless and await self._password_field(page):
                        raise self._passwordless_error(page)
                    await self._after_submit(page, cred, totp_tried=False)
                    return
                continue
            raise LoginFailed(f"No username or password field could be identified at {page.url}",
                              credentials_sent=False)
        raise LoginFailed(f"Still no password field after {MAX_LOGIN_STEPS} sign-in steps",
                          credentials_sent=False)

    def _passwordless_error(self, page) -> LoginFailed:
        # A setup problem (the secret says "passwordless"): trying again changes nothing.
        return LoginFailed(f"{page.url} asks for a password, but the login for "
                           f"{self.display_name} is passwordless (no password in its secret)",
                           retryable=False, credentials_sent=False)

    async def _mfa_input(self, page) -> Optional[str]:
        return await _first_visible(page, [await self._saved_fit(page, "mfa_code_input"),
                                           *web.MFA_INPUT_SELECTORS])

    async def _stop_if_blocked(self, page) -> str:
        """Body text, lowercased (with the sign-in frame's); raises LoginBlocked on
        lockout / CAPTCHA wording."""
        text = await self._login_text(page)
        blocked = next((h for h in web.BLOCKED_HINTS if h in text), None)
        if blocked:
            logger.warning("Login: page mentions %r -> stopping (lockout / CAPTCHA / block)",
                           blocked)
            # With --dump-pages, the page that said so: on AAR (2026-11-03) only the
            # session recording showed "reCAPTCHA verification failed".
            if os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes"):
                try:
                    await web.page_text(page, f"{self.vendor_id}-login-blocked")
                except Exception as exc:
                    logger.debug("Blocked login page not saved: %r", exc)
            raise LoginBlocked(f"Portal reports a lockout or block ({blocked!r})")
        captcha = next((h for h in web.CAPTCHA_HINTS if h in text), None)
        if captcha:
            # For now tried again, not a stop (web.CAPTCHA_HINTS, 2026-11-03).
            logger.warning("Login: page mentions %r -> login failed, may be tried again",
                           captcha)
            raise CaptchaFailed(f"Portal shows a CAPTCHA ({captcha!r})")
        return text

    async def _after_submit(self, page, cred: VendorCredential, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        text = await self._stop_if_blocked(page)          # 1. always first

        mfa_sel = await self._mfa_input(page)
        frame = self._login_frame
        on_login = bool(await self._password_field(page)) or bool(
            frame is not None and await self._frame_usable(page, frame)
            and await self._password_field(frame))
        if mfa_sel or (not on_login and any(h in text for h in web.MFA_TEXT_HINTS)):
            await self._mfa(page, cred, mfa_sel, text, totp_tried, code_given)    # 2.
            return

        if on_login:                                                   # 3.
            bad = next((h for h in web.BAD_PASSWORD_HINTS if h in text), None)
            if bad:
                logger.warning("Login: portal says %r -> password needs updating", bad)
                raise InvalidCredentials(f"Portal rejected the username or password ({bad!r})")

        logout = await self._logout_link(page)                         # 4.
        if not logout and not on_login:
            logout = await self._logout_in_menu(page)                  # 4b. in a menu
            if logout:
                await self._close_menu(page)
        if logout and not on_login:
            self._signed_in(page, logout)
            return

        server_error = next((h for h in web.SERVER_ERROR_HINTS if h in text), None)
        if server_error:
            logger.warning("Login: server error page after submit (%r)", server_error)
            raise LoginFailed(f"Server error after the login submit ({server_error!r})")

        # Read again: the text above can be from a redirect page the portal left
        # since (DASI's frontdoor.jsp -> /myprofile, 2026-10-29).
        text = await self._stop_if_blocked(page)
        state = await self._ask(lambda: self.extractor.classify_login_page(text, page.url))
        logger.info("Login: rules can't tell what %s shows; LLM says %s", page.url, state)
        if state == "signed_in" and not on_login:
            self._signed_in(page, None)
            return
        if state == "bad_credentials":
            raise InvalidCredentials("Portal rejected the username or password (read by the LLM)")
        if state == "blocked":
            raise LoginBlocked("Page looks like a block / CAPTCHA (read by the LLM)")
        if state == "mfa":
            await self._mfa(page, cred, None, text, totp_tried, code_given)
            return
        # With --dump-pages, the page the rules couldn't read: AAR (2026-11-04) went to
        # its home page after the first login without a CAPTCHA and nothing told
        # whether that was signed in.
        if os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes"):
            try:
                await web.page_text(page, f"{self.vendor_id}-login-unknown")
            except Exception as exc:
                logger.debug("Unknown login page not saved: %r", exc)
        # A rejection in words we don't know looks the same: a second submit could be
        # the failed attempt that locks the account, so neither is retried.
        raise LoginFailed("Still on the sign-in form after submit" if on_login
                          else f"Couldn't tell whether the sign-in worked at {page.url}",
                          retryable=False)

    def _signed_in(self, page, logout: Optional[str]) -> None:
        logger.info("Login: signed in (%s)", f"{logout} visible" if logout else page.url)
        self.login_claimed = True
        if logout:
            self.learned["logout_link"] = logout
            self._proven(page, logout)
        if host_allowed(page.url, self.hosts):
            # A form posted back to its own address (ASP.NET, Procart) leaves us on
            # the login URL, which shows the form again when opened: use the site's
            # root page as the place to check the sign-in instead.
            url = page.url.split("#")[0]
            if LOGIN_PATH.search(url.split("?")[0].lower()):
                parts = urlsplit(url)
                url = f"{parts.scheme}://{parts.netloc}/"
            self.learned["home_url"] = url

    def _proven(self, page, logout: str) -> None:
        """A logout link on one of the vendor's own pages: the proof is_authenticated
        looks for, so the probe right after login is skipped (signed_in_proof)."""
        if host_allowed(page.url, self.hosts):
            self.signed_in_proof = f"{logout} at {page.url.split('?')[0]}"
            self._login_ok = True

    async def _mfa(self, page, cred: VendorCredential, mfa_sel: Optional[str], text: str,
                   totp_tried: bool, code_given: bool = False) -> None:
        if cred.totp_secret and mfa_sel and not totp_tried:
            left = 30 - (time.time() % 30)
            if left < 3:  # don't submit a code that expires in flight
                await asyncio.sleep(left + 0.5)
            logger.info("Submitting TOTP for %s", cred.secret_key)
            await self._type_code(page, mfa_sel, cred.totp_now())
            await self._after_submit(page, cred, totp_tried=True)
            return
        method = ("code_rejected" if code_given else
                  "totp_rejected" if totp_tried else
                  "sms" if "text message" in text or "sms" in text else
                  "email" if "email" in text or "inbox" in text else
                  "totp" if "authenticator" in text else "unknown")
        if not mfa_sel:
            mfa_sel = await self._llm_code_input(page)
        logger.warning("Login: 2FA step shown (%s, code box %s, totp seed=%s) -> needs a person",
                       method, mfa_sel, "yes" if cred.totp_secret else "no")
        raise MfaRequired(method, input_selector=mfa_sel)

    async def _type_code(self, page, mfa_sel: str, code: str) -> None:
        await page.fill(mfa_sel, code)
        submit = await self._pick_button(page, "mfa_submit_button", web.MFA_SUBMIT_SELECTORS)
        if submit:
            await self._submit_and_wait(
                page, lambda: page.click(submit, no_wait_after=True), "2FA submit")
        else:
            await self._submit_and_wait(
                page, lambda: page.press(mfa_sel, "Enter", no_wait_after=True), "2FA submit")
        await _settle(page)
        self._learn("mfa_code_input", mfa_sel)

    async def person_finished_login(self, page) -> bool:
        """Read-only check of the page a person is logging in on (Live View): no
        password field, no code box, and either a logout link or an address that
        isn't a sign-in page. The sign-in check afterwards (is_authenticated)
        confirms it, unless a logout link was seen here."""
        try:
            if page.is_closed():
                return False
            if not host_allowed(page.url, self.hosts):
                return False              # e.g. still on a single sign-on page
            await self._stop_if_blocked(page)
            if await self._mfa_input(page) or await self._password_field(page):
                return False
            logout = await self._logout_link(page)
            if not logout and LOGIN_PATH.search(urlsplit(page.url).path.lower()):
                return False
        except LoginBlocked:
            raise
        except Exception as exc:          # the page is mid-navigation: look again later
            logger.debug("Live View check: %r", exc)
            return False
        self._signed_in(page, logout)
        return True

    accepts_person_logout = True

    @property
    def signed_out_check_url(self) -> str:
        """Where person_finished_logout looks: a page that shows the sign-in when
        signed out."""
        return self.probe_url or self.default_login_url

    async def person_finished_logout(self, page) -> bool:
        """Checked in a second tab of the same browser (same session cookies),
        so the person's page is never touched: no logout link there, and a
        sign-in form or Log in link."""
        url = self.signed_out_check_url
        if not url or not host_allowed(url, self.hosts):
            return False
        tab = await page.context.new_page()
        try:
            self.apply_timeouts(tab)
            await tab.goto(url, wait_until="domcontentloaded")
            await _settle(tab)
            if not host_allowed(tab.url, self.hosts):
                return False
            await self._stop_if_blocked(tab)
            await accept_cookies(tab)
            if await self._logout_link(tab):
                return False
            return bool(await self._password_field(tab) or await self._login_link(tab))
        except LoginBlocked:
            raise
        except Exception as exc:          # a slow page: look again later
            logger.debug("Logout check: %r", exc)
            return False
        finally:
            await tab.close()

    async def _llm_code_input(self, page) -> Optional[str]:
        """The box the 2FA code goes in, asked from the LLM when no rule found one."""
        elements = await self._elements(page)
        if not elements:
            return None
        index = await self._ask(lambda: self.extractor.pick_element(
            elements, "the text box where the login / verification code is typed"))
        if index is None:
            return None
        el = elements[index]
        if el.get("tag") != "input" or (el.get("type") or "").lower() not in (
                *_TEXT_INPUT_TYPES, "number"):
            logger.warning("LLM picked element %d as the code box, but it is a %s type=%s; "
                           "not used", index, el.get("tag"), el.get("type"))
            return None
        selector, save = self._selector_for(el)
        self.found_by.setdefault("mfa_code_input", "LLM")
        self._learn("mfa_code_input", save)
        return selector

    async def submit_mfa_code(self, page, cred: VendorCredential, code: str,
                              input_selector: str) -> None:
        """Type the code a person sent (sourcing/auth/hitl.py) and finish the login."""
        self.apply_timeouts(page)
        if not host_allowed(page.url, self.hosts):
            raise LoginBlocked(f"Refusing to type a 2FA code on {page.url}")
        logger.info("Login: typing the code a person sent into %s", input_selector)
        try:
            await self._type_code(page, input_selector, code)
            await self._after_submit(page, cred, totp_tried=True, code_given=True)
        except PlaywrightTimeoutError as exc:
            first_line = str(exc).splitlines()[0]
            raise LoginFailed(f"Timed out after the 2FA code at {page.url}: {first_line}",
                              retryable=False) from exc

    # -- not sold -------------------------------------------------------------
    @property
    def not_sold_phrases(self) -> tuple[str, ...]:
        """The vendor record's wording plus wording learned on earlier searches."""
        learned = json.loads(self._saved("not_sold_phrases") or "[]")
        return tuple(dict.fromkeys([*super().not_sold_phrases, *learned]))

    async def _ask_not_sold(self, page_text: str, part_number: str, usage) -> Optional[str]:
        """The LLM's quote saying this part isn't sold, used only when it is on the
        page next to the part. A confirmed wording is saved for later searches."""
        if self.not_sold_checks >= MAX_NOT_SOLD_CHECKS:
            return None
        self.not_sold_checks += 1
        quote, used = await self.extractor.not_sold_quote(page_text, part_number)
        usage.add(used)
        if not quote:
            return None
        norm = lambda t: " ".join(t.split()).lower()  # noqa: E731
        context = part_context(page_text, part_number, strict=True) or page_text
        if norm(quote) not in norm(context):
            logger.warning("Results: LLM says %s isn't sold, but its quote %r is not on the "
                           "page next to the part; not used", part_number, quote[:120])
            return None
        phrase = " ".join(re.sub(re.escape(part_number), " ", quote, flags=re.I).split())
        phrase = phrase.strip(" .:;,-")
        if len(phrase) >= MIN_NOT_SOLD_PHRASE and phrase.lower() not in {
                p.lower() for p in self.not_sold_phrases}:
            learned = json.loads(self._saved("not_sold_phrases") or "[]") + [phrase]
            self.learned["not_sold_phrases"] = json.dumps(learned)
            self.found_by.setdefault("not_sold_phrases", "LLM")
            logger.info("Results: learned how %s says a part isn't sold: %r",
                        self.display_name, phrase)
        return " ".join(quote.split())

    # -- logout -------------------------------------------------------------
    async def logout(self, page) -> bool:
        link = await self._logout_link(page) or await self._logout_in_menu(page)
        if not link:
            logger.info("Logout: no logout link on %s", page.url)
            return False
        logger.info("Logout: clicking %s", link)
        await self._submit_and_wait(page, lambda: page.click(link, no_wait_after=True), "Logout")
        await _settle(page)
        # Not "no logout link": one inside a menu is never visible.
        signed_out = (await self._password_field(page) or await self._login_link(page)
                      or not (await self._logout_link(page) or self._saved("account_menu")))
        logger.info("Logout: %s (now at %s)", "signed out" if signed_out else "not confirmed",
                    page.url)
        return bool(signed_out)

    # -- search -----------------------------------------------------------
    @property
    def part_url(self) -> str:
        """Direct results address with {part}: vendor record `part_url_template`
        ("" turns it off), else one learned this run or an earlier one."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return self._saved("part_url_template") or ""

    async def _signed_out_here(self, page) -> bool:
        """The page shows a sign-in form, or a Login link and no logout link."""
        if await self._password_field(page):
            return True
        return bool(await self._login_link(page)) and not await self._logout_link(page)

    async def search(self, page, row: RequestRow) -> SearchOutcome:
        """One part: the results address when one is known, else the site's search
        box. The results page is read by the extractor (every value checked
        against the page)."""
        if self.extractor is None:
            raise RuntimeError("Generic search needs an LLM to read the results page")
        self.apply_timeouts(page)
        results_url = await self._open_results_by_url(page, row)
        if results_url is None:
            results_url = await self._open_results_by_form(page, row)
        return await self._read_results(page, row, results_url)

    async def _open_results_by_url(self, page, row: RequestRow) -> Optional[str]:
        template = self.part_url
        if not template or self.part_url_failed:
            return None
        url = fill_part_url(template, row.part_number)
        if not host_allowed(url, self.hosts):
            self.part_url_failed = f"{url} is not on {', '.join(self.hosts)}"
            logger.warning("Search: direct address not used: %s", self.part_url_failed)
            return None
        logger.info("Search: opening %s directly", url)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out when opening the results address")
        status = response.status if response is not None else 0
        landed, wanted = urlsplit(page.url), urlsplit(url)
        if status >= 400 or (landed.hostname, landed.path.lower()) != (wanted.hostname,
                                                                       wanted.path.lower()):
            self.part_url_failed = f"{url} gave HTTP {status} at {page.url}"
            logger.warning("Search: direct address didn't open a results page (%s); using the "
                           "search box for the rest of this run", self.part_url_failed)
            return None
        return url

    async def _find_search_input(self, page) -> str:
        selector = await self._pick(page, "search_input", web.SEARCH_INPUT_SELECTORS)
        if selector:
            self._learn("search_input", selector, keep_first=True)
            return selector
        elements = await self._elements(page)
        index = await self._ask(lambda: self.extractor.pick_element(
            elements, "the text box used to search the whole catalog by part number (often "
                      "in the page header, with a placeholder such as 'Enter Part Number', "
                      "'Find your part' or 'Search'), not a filter of orders, quotes or "
                      "the cart"))
        if index is not None:
            el = elements[index]
            if el.get("tag") == "input" and (el.get("type") or "").lower() in (
                    "", "text", "search"):
                selector, save = self._selector_for(el)
                logger.info("LLM picked the search box: %s, saved as %s (%s)", selector, save,
                            json.dumps(el)[:200])
                self.found_by.setdefault("search_input", "LLM")
                self._learn("search_input", save)
                return selector
            logger.warning("LLM picked element %d as the search box, but it is a %s type=%s; "
                           "not used", index, el.get("tag"), el.get("type"))
        raise RuntimeError(f"No part search box found on {page.url}")

    async def _search_by_llm_steps(self, page, pn: str, box: str, tried: list[str],
                                   ran, submit, t0: float) -> bool:
        """When Enter and the search button didn't run the search, the LLM looks at
        the page and names one new step at a time (another button, a Multiple /
        bulk box, another page), up to MAX_SEARCH_STEPS. The code checks each step
        and does it; "results" counts only when the page changed and shows the
        part number outside the search box."""
        if self.extractor is None:
            return False
        last_box = box
        for _ in range(MAX_SEARCH_STEPS + 1):
            if time.monotonic() - t0 > SEARCH_FORM_BUDGET_SECONDS:
                tried.append(f"time limit ({SEARCH_FORM_BUDGET_SECONDS}s) reached")
                return False
            elements = await self._elements(page)
            step, usage = await self.extractor.next_search_step(
                elements, await web._body_text(page), pn, tried)
            self.usage.add(usage)
            action, index = step.get("action"), step.get("index")
            el = elements[index] if index is not None else None
            logger.info("Search %s: LLM suggests %s%s (%s)", pn, action,
                        f" {self._describe(el)}" if el else "", step.get("reason", ""))
            if action == "give_up":
                tried.append(f"LLM gave up: {step.get('reason', '')}")
                return False
            if action == "results":
                if await ran() and pn.lower() in (await web.content_text(page)).lower():
                    return True
                tried.append("LLM said results were shown, but the page doesn't show the part")
                continue
            if action == "enter":
                await submit(lambda: page.press(last_box, "Enter", no_wait_after=True),
                             f"pressed Enter in {last_box}")
                continue
            if el is None:
                tried.append(f"LLM suggested {action} without a usable element")
                continue
            selector, _ = self._selector_for(el)
            if action == "type":
                if not (el.get("tag") == "textarea" or (
                        el.get("tag") == "input"
                        and (el.get("type") or "").lower() in (*_TEXT_INPUT_TYPES, "search"))):
                    tried.append(f"LLM suggested typing into {self._describe(el)} (not a text box)")
                    continue
                await page.fill(selector, "")
                await page.locator(selector).first.press_sequentially(pn, delay=50)
                last_box = selector
                tried.append(f"typed the part number into {self._describe(el)}")
                logger.info("Search %s: typed into %s", pn, selector)
                continue
            words = " ".join(str(el.get(k) or "") for k in ("text", "aria_label", "title", "id"))
            href = el.get("href") or ""
            if (not self._clickable(el) or _NOT_A_SEARCH.search(words)
                    or (href.startswith("http") and not host_allowed(href, self.hosts))):
                tried.append(f"LLM suggested clicking {self._describe(el)}: refused")
                logger.warning("Search %s: not clicking %s", pn, self._describe(el))
                continue
            await submit(lambda: page.click(selector, no_wait_after=True),
                         f"clicked {self._describe(el)}")
            if not host_allowed(page.url, self.hosts):
                raise RuntimeError(f"Search for {pn} led off the vendor's site ({page.url})")
        tried.append(f"LLM step limit ({MAX_SEARCH_STEPS}) reached")
        return False

    @staticmethod
    def _describe(el: Optional[dict]) -> str:
        if not el:
            return "-"
        words = el.get("text") or el.get("aria_label") or el.get("title") or el.get("placeholder")
        return f"{el.get('tag')} {words!r}" if words else f"{el.get('tag')} #{el.get('index')}"

    async def _open_results_by_form(self, page, row: RequestRow) -> str:
        if self.search_failed:
            raise RuntimeError(f"Search for {row.part_number} not tried: earlier in this run, "
                               f"{self.search_failed}")
        start = (self.config.get("search_url") or self._saved("home_url")
                 or self.config.get("website"))
        has_box = await self._pick(page, "search_input", web.SEARCH_INPUT_SELECTORS)
        if not has_box and start and host_allowed(start, self.hosts):
            await page.goto(start, wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out before searching")
        t0 = time.monotonic()
        box = await self._find_search_input(page)
        pn = row.part_number
        # A button that ran the search before is used straight away: on such a
        # portal Enter does nothing and only costs a wait.
        saved_button = await self._saved_fit(page, "search_button")
        use_button = bool(saved_button and await _first_visible(page, [saved_button]))
        start_url, before = page.url, await web.content_text(page)
        tried: list[str] = []
        navigated = False

        async def ran() -> bool:
            return navigated or page.url != start_url or await web.content_text(page) != before

        async def submit(action, what: str) -> bool:
            nonlocal navigated
            tried.append(what)
            logger.info("Search %s: %s", pn, what)
            navigated = await self._submit_and_wait(page, action, f"Search {pn}") or navigated
            return await ran()

        async def put(keys: bool) -> None:
            await page.fill(box, "")
            if keys:          # some boxes ignore a pasted value (DASI, 2026-10-29)
                await page.locator(box).first.press_sequentially(pn, delay=50)
            else:
                await page.fill(box, pn)

        async def box_empty() -> bool:
            try:
                return not (await page.input_value(box)).strip()
            except Exception:
                return True

        enter = lambda: page.press(box, "Enter", no_wait_after=True)  # noqa: E731
        done = False
        if not use_button:
            await put(keys=False)
            done = await submit(enter, f"pasted the part number in {box}, pressed Enter")
            if not done:
                # The first Enter can only close a drop-down, or empty the box.
                emptied = await box_empty()
                if emptied:
                    await put(keys=True)
                done = await submit(enter, "pressed Enter again" +
                                    (" after typing the part number again (box emptied)"
                                     if emptied else ""))
        if not done:
            # Many portals run the search only from the button next to the box.
            await put(keys=True)
            button = (saved_button if use_button else None) or (
                await self._pick(page, "search_button", web.SEARCH_BUTTON_SELECTORS)
                or await self._llm_pick_one(page, SEARCH_BUTTON_QUESTION, "search_button"))
            if button:
                done = await submit(lambda: page.click(button, no_wait_after=True),
                                    f"typed the part number key by key, clicked {button}")
                if done:
                    self._learn("search_button", button, keep_first=True)
            else:
                tried.append("looked for a search button next to the box: none found")
                done = await submit(enter, "typed the part number key by key, pressed Enter")
        if not done:
            done = await self._search_by_llm_steps(page, pn, box, tried, ran, submit, t0)
        if not done:
            # Reading this page anyway would turn "the search didn't run" into a
            # wrong NOT_FOUND (DASI run 12 read My Orders as results, 2026-10-29).
            # The rest of the run's parts would fail the same way: stop them at once.
            self.search_failed = (f"no way found to run a part search on {start_url}; tried: "
                                  + "; ".join(tried))
            raise RuntimeError(f"Search for {pn} did not run: {self.search_failed}")
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out after searching")
        template = part_url_template(page.url, row.part_number)
        if (template and host_allowed(template, self.hosts) and not self.part_url_failed
                and "part_url_template" not in self.config):
            logger.info("Search: results address holds the part number; next searches open %s",
                        template)
            self.learned["part_url_template"] = template
        logger.info("Search: results for %s at %s", row.part_number, page.url)
        return page.url.split("#")[0]

    async def _grid_text(self, page, label: str) -> Optional[str]:
        """A jqGrid's own records, when the page has one with rows (many portals
        render results with it); None means read the page text."""
        self._last_grid_used = False     # page-change signal, read in _read_results
        try:
            result = await asyncio.wait_for(
                page.evaluate(GRID_RECORDS_JS, self.config.get("results_grid")),
                web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.debug("Grid read failed: %r", exc)
            return None
        parsed = parse_grid_result(result)
        if not parsed:
            return None
        text, count, total = parsed
        if total and total > count:
            logger.info("Results grid holds %d of %d results; reading the page text instead",
                        count, total)
            return None
        logger.info("Results: read %d records from the results grid", count)
        web.dump_grid(label, text, result)
        self._last_grid_used = True
        return text

    async def _results_text(self, page, label: str) -> str:
        """The text the LLM reads: grid records, else the page text. A vendor
        adapter built on this one adds what its pages need (vendors/dasi.py)."""
        return await self._grid_text(page, label) or await web.page_text(page, label)

    async def _read_results(self, page, row: RequestRow, results_url: str) -> SearchOutcome:
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", f"generic-{row.part_number}")
        self._last_grid_used = None       # set by _grid_text; None = no grid read attempted
        # Closed detail rows are missing from the page (on Procart: condition,
        # warehouses, due dates; seen 2026-10-15), so open them first when the page
        # has an "Expand all" control.
        await web.wait_grid_idle(page)
        await web.expand_all_details(page, click_timeout_ms=self.action_timeout_ms)
        raw_text = await self._results_text(page, label)
        if any(h in raw_text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(raw_text)
        if len(raw_text) > self.max_page_chars:
            logger.warning("Results page for %s is %d chars; sending the first %d",
                           row.part_number, len(raw_text), self.max_page_chars)
            raw_text = raw_text[: self.max_page_chars]
        found, kind, offers, usage = await self.extractor.extract_offers(
            page_text=raw_text, page_url=results_url, part_number=row.part_number,
            qty_needed=row.qty_needed, condition_wanted=row.condition_wanted,
            system=self.extract_system,
        )
        usage.add(self.usage)                 # login / discovery calls ride on this part
        self.usage = type(self.usage)()
        if kind == "login_page":
            raise SessionExpired("Extractor saw a login page")
        not_sold = self.not_sold_reason(raw_text, row.part_number, offers)
        if not not_sold and not has_real_offer(offers) and kind != "error":
            not_sold = await self._ask_not_sold(raw_text, row.part_number, usage)
        if not_sold:
            logger.info("Results: %s doesn't sell %s: %s", self.display_name,
                        row.part_number, not_sold)
            self._search_ok = True
            return SearchOutcome(found=False, usage=usage, not_sold=not_sold,
                                 page_url=results_url, page_kind=kind,
                                 grid_used=self._last_grid_used)
        if found and has_real_offer(offers):
            self._search_ok = True
        return SearchOutcome(found=found, offers=offers, usage=usage, page_url=results_url,
                             page_kind=kind, grid_used=self._last_grid_used,
                             alternates=self.listed_alternates(row))

    def listed_alternates(self, row: RequestRow) -> list[str]:
        """Part numbers the portal lists as alternates of the requested part, read
        from the page without opening anything. Only the vendors that turned
        substitutes off (Wencor, Aviall) fill this; they get a note, no entry."""
        return []
