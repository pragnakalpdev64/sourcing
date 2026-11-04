"""Browser helpers every vendor adapter uses: finding visible elements, waiting
after a submit, answering cookie popups, reading page text, and the generic
selector and wording lists for login, 2FA, logout and search.

Moved out of proponent.py (2026-10-15) so a generic adapter can log in to and
search a vendor it has no code for (docs/GENERIC_VENDOR_PLAN.md). Vendor
adapters put their own exact selectors in front of these lists.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("sourcing.vendors.web")

PAGE_SCRIPT_TIMEOUT = 30      # seconds; page.evaluate has no timeout of its own

# Cookie / consent popups. Procart shows two: its own Catapult bar (Accept
# reloads the page) and a Termly "Cookie Consent Prompt" <dialog> injected by
# script after load, which blocks every click until answered. The others are
# common consent tools other portals use.
CONSENT_CONTAINERS = [
    "dialog[aria-label*='cookie' i]", "dialog[aria-label*='consent' i]",
    "[role='dialog'][aria-label*='cookie' i]", "[role='dialog'][aria-label*='consent' i]",
    "#termly-code-snippet-support",
    "#onetrust-banner-sdk",
    "#catapult-cookie-bar",
    "[id*='cookie' i][class*='banner' i]", "[class*='cookie-banner' i]",
]
# Most privacy-preserving answer first; Accept only when nothing else is offered.
CONSENT_BUTTON_TEXTS = [
    "Decline all", "Decline", "Reject all", "Reject", "Only necessary", "Necessary only",
    "Accept all", "Accept", "Allow all", "Allow", "I agree", "Agree", "OK", "Got it",
]

LOGOUT_SELECTORS = [
    "a:has-text('Logout')", "a:has-text('Log out')", "a:has-text('Log Out')",
    "a:has-text('Sign out')", "a:has-text('Sign Out')",
    "button:has-text('Logout')", "button:has-text('Log out')", "button:has-text('Sign out')",
    "a[href*='logout' i]", "a[href*='logoff' i]", "a[href*='signout' i]",
]

# Buttons that open the menu holding the logout link when it isn't on the page
# itself: a profile avatar or the user's name (DASI's store after sign-in,
# 2026-10-29; Notion, 2026-10-22). Buttons only: clicking one must not leave the page.
ACCOUNT_MENU_SELECTORS = [
    *(f"{tag}[{attr}*='{word}' i]"
      for word in ("profile", "account", "user menu", "avatar")
      for attr in ("aria-label", "title")
      for tag in ("button", "[role='button']")),
    "button[class*='avatar' i]", "button[class*='profile' i]", "button[class*='user-menu' i]",
    "button[aria-haspopup]:has(img)",
]

# Links that open the login page from a home page, when the start URL isn't the
# login page itself (most specific first).
# has-text matches part of a text, so "Login" also matched Monroe's "Forgot Login"
# (2026-11-04): links that recover or register are never the login link.
_NOT_LOGIN = ":not(:has-text('forgot')):not(:has-text('reset')):not(:has-text('register'))"
LOGIN_LINK_SELECTORS = [
    *(f"{tag}:has-text('{word}'){_NOT_LOGIN}" for tag in ("a", "button")
      for word in ("Log in", "Login", "Sign in")),
    # Not a logout address that happens to contain "login": Boeing's SSO logout is
    # .../accesslogin/Logout.faces, and following it signed the run out (2026-11-02).
    *(f"a[href*='{word}' i]:not([href*='logout' i]):not([href*='signout' i])"
      ":not([href*='logoff' i]):not([href*='sign-out' i]):not([href*='forgot' i])"
      ":not([href*='reset' i])" for word in ("login", "signin")),
]

USERNAME_SELECTORS = [
    "input[id*='username' i]", "input[name*='username' i]",
    "input[type='email']", "input[autocomplete='username']",
]
# Not a box hidden from people (aria-hidden, out of the tab order): Notion's
# email page keeps one for password managers (seen 2026-10-22).
PASSWORD_SELECTORS = ["input[type='password']:not([aria-hidden='true']):not([tabindex='-1'])"]
SUBMIT_SELECTORS = [
    "input[type='submit']", "button[type='submit']",
    "button:has-text('Log in')", "button:has-text('Login')", "button:has-text('Sign in')",
]
MFA_INPUT_SELECTORS = [
    "input[autocomplete='one-time-code']", "input[name*='otp' i]", "input[id*='otp' i]",
    "input[name*='code' i]", "input[id*='code' i]", "input[placeholder*='code' i]",
    "input[aria-label*='code' i]", "input[inputmode='numeric']",
]
MFA_SUBMIT_SELECTORS = [
    "input[type='submit']", "button[type='submit']",
    "button:has-text('Verify')", "button:has-text('Submit')", "button:has-text('Continue')",
    "[role='button']:has-text('Continue')",
]
# The "Next" / "Continue" button of a two-step login (username first, then a
# password or code page). Exact text first: "Continue with Google" also has
# "Continue" in it (Notion's login page, 2026-10-22).
NEXT_SELECTORS = [
    "button:text-is('Next')", "button:text-is('Continue')",
    "[role='button']:text-is('Next')", "[role='button']:text-is('Continue')",
    "button:has-text('Next')", "button:has-text('Continue')",
    # Notion's Continue is a <div role="button"> with its text in a child, which
    # :text-is() doesn't match (2026-10-22); the other-account filter guards these.
    "[role='button']:has-text('Next')", "[role='button']:has-text('Continue')",
    "input[type='submit'][value*='Next' i]", "input[type='submit'][value*='Continue' i]",
    "button[type='submit']", "input[type='submit']",
]
# Buttons that sign in with another account (Google, Apple, SSO, passkey...).
# Never clicked as a form's Next / Submit: they leave the vendor's login.
OTHER_SIGN_IN_WORDS = (
    "google", "apple", "microsoft", "facebook", "github", "linkedin", "sso", "single sign-on",
    "saml", "passkey", "okta",
)
# The button that runs a part search (used when Enter in the box does nothing).
# The button next to the search box. Not a bulk / multiple / advanced search
# (DASI's "Multiple" button has title "Search Advanced", 2026-10-29).
_NOT_SIMPLE = ":not([title*='advanced' i]):not([aria-label*='advanced' i])" \
              ":not([title*='multiple' i]):not([aria-label*='multiple' i])" \
              ":not([title*='bulk' i]):not([aria-label*='bulk' i])"
SEARCH_BUTTON_SELECTORS = [
    f"button:text-is('Search'){_NOT_SIMPLE}", f"button:has-text('Search'){_NOT_SIMPLE}",
    "input[type='submit'][value*='Search' i]",
    *(f"{tag}[{attr}*='search' i]{_NOT_SIMPLE}"
      for attr in ("aria-label", "title") for tag in ("button", "[role='button']")),
    "button:text-is('Go')", "button:text-is('Find')",
]
SEARCH_INPUT_SELECTORS = [
    "input[id*='partnumber' i]", "input[name*='partnumber' i]",
    "input[id*='part' i][type='text']", "input[placeholder*='part' i]",
    "input[type='search']", "input[id*='search' i]", "input[name*='search' i]",
    "input[placeholder*='search' i]",
]

BAD_PASSWORD_HINTS = [
    "invalid password", "incorrect password", "invalid username or password",
    "invalid login", "login failed", "invalid credentials", "password is incorrect",
    "the user name or password provided is incorrect",
]
BLOCKED_HINTS = [
    "account is locked", "account has been locked", "locked out", "too many attempts",
    "account disabled", "access denied",
    # The portal flags the traffic itself: stop, never try again on our own.
    "too many requests", "unusual activity", "suspicious activity", "verify you are human",
    "are you a robot",
]
# CAPTCHA wording: for now (2026-11-03) not a stop but a login failure that the
# login manager tries again (MAX_LOGIN_ATTEMPTS); AAR said "reCAPTCHA
# verification failed." on the direct login page. Put "captcha" back in
# BLOCKED_HINTS to stop on it again.
CAPTCHA_HINTS = ["captcha"]
# A server-side failure after the login submit: the credentials were not judged,
# so one more attempt is safe.
SERVER_ERROR_HINTS = [
    "internal server error", "service unavailable", "bad gateway", "gateway timeout",
    "server error", "temporarily unavailable", "an unexpected error",
]
MFA_TEXT_HINTS = [
    "verification code", "authenticator", "one-time", "two-factor", "2-step",
    "security code", "we sent a code", "enter the code",
    # Code instead of a password, sent by email (Notion, 2026-10-22).
    "login code", "sign-in code", "temporary code", "sent you a code", "check your inbox",
    "check your email",
]
EXPIRED_HINTS = ["session has expired", "session expired", "please log in again"]

_LIST_INPUTS_JS = """
() => Array.from(document.querySelectorAll(
    'input:not([type=hidden]):not([type=password]):not([type=checkbox])' +
    ':not([type=radio]):not([type=submit]):not([type=button]):not([type=image])'))
  .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .slice(0, 40)
  .map((el, i) => {
    el.setAttribute('data-sourcing-idx', String(i));
    const label = el.labels && el.labels[0] ? el.labels[0].innerText.trim().slice(0, 80) : null;
    return {index: i, id: el.id || null, name: el.getAttribute('name'), type: el.type,
            placeholder: el.placeholder || null, aria_label: el.getAttribute('aria-label'),
            label: label};
  })
"""


# Visible inputs, buttons and links for the LLM to pick from on a page the code
# doesn't know (generic login). Metadata only: an input's typed value is never
# read, so a password can't end up in a prompt. Each element gets
# data-sourcing-el="<index>" so an element without id/name can still be used
# on this page.
_LIST_ELEMENTS_JS = """
() => {
  document.querySelectorAll('[data-sourcing-el]').forEach(el => el.removeAttribute('data-sourcing-el'));
  const visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const text = el => ((el.tagName === 'INPUT' ? '' : el.innerText) || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
  // Not inputs hidden from people (aria-hidden / out of the tab order): Notion keeps
  // a password box like that for password managers (2026-10-22).
  const forPeople = el => el.getAttribute('aria-hidden') !== 'true'
    && !(el.tagName === 'INPUT' && el.tabIndex < 0);
  const els = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, button, a[href], [role=button]'))
    .filter(visible).filter(forPeople).slice(0, 80);
  return els.map((el, i) => {
    el.setAttribute('data-sourcing-el', String(i));
    const label = el.labels && el.labels[0] ? el.labels[0].innerText.trim().slice(0, 60) : null;
    const isButtonInput = el.tagName === 'INPUT' && /^(submit|button|image)$/.test(el.type);
    return {index: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type'),
            role: el.getAttribute('role'),
            id: el.id || null, name: el.getAttribute('name'),
            placeholder: el.getAttribute('placeholder'), autocomplete: el.getAttribute('autocomplete'),
            aria_label: el.getAttribute('aria-label'), label: label,
            // An avatar button often has no text, only a title or a picture's alt.
            title: el.getAttribute('title') || (el.querySelector('img[alt]') || {}).alt || null,
            text: isButtonInput ? (el.value || '').slice(0, 60) : text(el) || null,
            href: el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 120) : null};
  });
}
"""
# What innerText leaves out of a results page: cells hidden by CSS (jqGrid hides
# columns that way), tooltips / icon labels (a condition shown as an icon or a
# hover text), and links (each line's own part page, for its Source URL). One
# line per table row that has any, keyed by its visible text, then part/product
# links outside tables.
_GRID_EXTRAS_JS = """
(limit) => {
  const out = [];
  const colName = (td, i, row) => {
    const d = td.getAttribute('aria-describedby');
    if (d) return d.replace(/^.*?_/, '');
    const table = row.closest('table');
    const th = table && table.querySelectorAll('thead th, tr.jqgfirstrow td')[i];
    return (th && th.innerText.trim()) || ('col' + (i + 1));
  };
  for (const row of document.querySelectorAll('tr')) {
    const cells = Array.from(row.children).filter(c => c.tagName === 'TD');
    if (!cells.length) continue;
    const extras = [];
    cells.forEach((td, i) => {
      const hidden = getComputedStyle(td).display === 'none';
      const text = (td.textContent || '').trim().replace(/\\s+/g, ' ');
      if (hidden && text) extras.push(colName(td, i, row) + ' (hidden): ' + text.slice(0, 400));
      td.querySelectorAll('[title], img[alt], [aria-label], [data-original-title]').forEach(el => {
        const t = (el.getAttribute('title') || el.getAttribute('alt') ||
                   el.getAttribute('aria-label') || el.getAttribute('data-original-title') || '').trim();
        if (t && !(td.innerText || '').includes(t)) extras.push(colName(td, i, row) + ': ' + t.slice(0, 400));
      });
    });
    row.querySelectorAll('a[href]').forEach(a => {
      const href = a.href || '';
      if (/^https?:/i.test(href) && !href.startsWith(location.href.split('#')[0] + '#'))
        extras.push('link: ' + href);
    });
    if (extras.length) {
      const key = (row.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
      out.push(key + ' | ' + Array.from(new Set(extras)).join('; '));
    }
    if (out.join('\\n').length > limit) break;
  }
  // Part / product links outside table rows (card layouts on other sites).
  const cards = Array.from(document.querySelectorAll('a[href]'))
    .filter(a => !a.closest('tr') && /^https?:/i.test(a.href) &&
                 /part|product|item|detail|sku|catalog/i.test(a.href))
    .slice(0, 100)
    .map(a => ((a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80) || '(no text)') +
              ' | link: ' + a.href);
  return out.concat(cards).join('\\n');
}
"""

GRID_EXTRAS_MAX_CHARS = 15_000
GRID_IDLE_TIMEOUT_MS = 30_000  # longest wait for a results grid's "Loading..." overlay

# Mark a visible "Expand all" / "Expand all details" control (Procart's results
# grid has one) with data-sourcing-expand="1".
_MARK_EXPAND_ALL_JS = """
() => {
  document.querySelectorAll('[data-sourcing-expand]').forEach(el => el.removeAttribute('data-sourcing-expand'));
  const els = Array.from(document.querySelectorAll('a, button, input[type=button], input[type=submit], span, div'));
  const label = el => (el.tagName === 'INPUT' ? el.value : el.innerText || '').trim();
  const el = els.find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && /^expand all( details)?$/i.test(label(e));
  });
  if (!el) return false;
  el.setAttribute('data-sourcing-expand', '1');
  return true;
}
"""

# True when no "Loading..." overlay of the results grid (jqGrid: #load_<grid>,
# .loading) is showing.
_GRID_IDLE_JS = """
() => !Array.from(document.querySelectorAll(".ui-jqgrid [id^='load_'], .ui-jqgrid .loading"))
  .some(el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' &&
                       st.visibility !== 'hidden' && /loading/i.test(el.innerText || ''); })
"""


# DEBUG_DUMP_PAGES=1 saves every results page (text sent to the model + HTML)
# under sourcing-proto/debug_pages/ to check what the portal shows. Local
# debugging only: the files hold vendor prices.
DUMP_DIR = Path(__file__).resolve().parents[2] / "debug_pages"


async def page_text(page, label: str) -> str:
    """Body text plus what innerText drops (hidden cells, tooltips, links)."""
    text = await page.inner_text("body")
    try:
        extras = await asyncio.wait_for(page.evaluate(_GRID_EXTRAS_JS, GRID_EXTRAS_MAX_CHARS),
                                        PAGE_SCRIPT_TIMEOUT)
    except Exception as exc:
        logger.debug("Grid details not read: %r", exc)
        extras = ""
    if extras:
        text += ("\n\n--- Page details not shown as text (hidden columns, tooltips, "
                 "icon labels and links, one line per row) ---\n" + extras)
    if os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes"):
        try:
            DUMP_DIR.mkdir(exist_ok=True)
            stem = DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
            stem.with_suffix(".txt").write_text(text, encoding="utf-8")
            stem.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
            logger.info("Saved results page to %s.(txt|html)", stem)
        except Exception as exc:
            logger.warning("Could not save the results page: %r", exc)
    return text


def loose_key(part_number: str) -> str:
    """A part number without spaces, dashes, slashes and dots, upper case: two
    spellings of one number compare equal."""
    return re.sub(r"[\s\-/.]", "", (part_number or "").upper())


class _NoNavigation(Exception):
    """Internal: a submit didn't start a page load."""


async def submit_and_wait(page, action, what: str, *, action_timeout_ms: int,
                          page_load_timeout_ms: int) -> bool:
    """Run a click/press that submits a form, then wait for what it loads.

    Two stages: did the submit START a page load (waits up to the action
    timeout)? If so, wait for that page as long as it needs (up to the page-load
    timeout). If nothing started (an in-place update, or the submit didn't fire),
    wait for the network to go quiet instead. Returns True if a new page loaded.
    Raises PlaywrightTimeoutError if a page load started but never finished."""
    started = time.time()
    nav_started = asyncio.Event()

    def on_request(request) -> None:
        if request.is_navigation_request() and request.frame == page.main_frame:
            nav_started.set()

    page.on("request", on_request)
    try:
        async with page.expect_navigation(wait_until="domcontentloaded",
                                          timeout=page_load_timeout_ms):
            await action()
            try:
                await asyncio.wait_for(nav_started.wait(), action_timeout_ms / 1000)
            except asyncio.TimeoutError:
                raise _NoNavigation() from None
            logger.info("%s: submitted; waiting up to %ds for the page", what,
                        page_load_timeout_ms // 1000)
        navigated = True
    except _NoNavigation:
        navigated = False
    except PlaywrightTimeoutError:
        logger.warning("%s: the page started loading but didn't finish within %ds (%s)",
                       what, page_load_timeout_ms // 1000, page.url)
        raise
    finally:
        page.remove_listener("request", on_request)
    await _settle(page, timeout_ms=15_000)
    if navigated:
        logger.info("%s: page ready after %.1fs (%s)", what, time.time() - started, page.url)
    else:
        logger.info("%s: no new page within %ds (updated in place, or the submit didn't "
                    "fire); now at %s", what, action_timeout_ms // 1000, page.url)
    return navigated


def _css_attr(name: str, value: str) -> str:
    return f'[{name}="{value.replace(chr(34), chr(92) + chr(34))}"]'


async def _settle(page, timeout_ms: int = 10_000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


# Selectors a "a, b, c" list can't hold: chained (>>), engine-prefixed (text=, xpath=).
_NOT_JOINABLE = re.compile(r">>|^\s*(?:[a-z_-]+=|//)", re.I)


async def _none_visible(page, selectors: list[str]) -> bool:
    """True when no selector in the list matches a visible element: one query
    instead of two per selector. Each one is a round trip to the remote browser
    (Browserbase), and a list of 30 selectors took ~10s on Aviall (2026-11-02).
    False when unsure (a selector the list form can't take)."""
    if len(selectors) < 3 or any(_NOT_JOINABLE.search(s) for s in selectors):
        return False
    try:
        return not await page.locator(", ".join(selectors)).filter(visible=True).count()
    except Exception:
        return False


async def _first_visible(page, selectors: list[Optional[str]]) -> Optional[str]:
    selectors = [s for s in selectors if s]
    if await _none_visible(page, selectors):
        return None
    for selector in selectors:
        if not selector:
            continue
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                return selector
        except Exception:
            continue
    return None


async def first_visible_button(page, selectors: list[Optional[str]]) -> Optional[str]:
    """Like _first_visible, for a button to click: the first visible match whose
    text, value or label doesn't sign in with another account (Google, SSO...).
    A later match of the same selector is returned as "<selector> >> nth=<i>"."""
    selectors = [s for s in selectors if s]
    if await _none_visible(page, selectors):
        return None
    for selector in selectors:
        if not selector:
            continue
        try:
            matches = page.locator(selector)
            for i in range(min(await matches.count(), 10)):
                el = matches.nth(i)
                if not await el.is_visible():
                    continue
                label = " ".join(filter(None, [
                    await el.inner_text(), await el.get_attribute("value"),
                    await el.get_attribute("aria-label")])).lower()
                if any(word in label for word in OTHER_SIGN_IN_WORDS):
                    continue
                return selector if i == 0 else f"{selector} >> nth={i}"
        except Exception:
            continue
    return None


# OneTrust's own button ids (Aviall's banner, 2026-11-02: its wording matched none
# of the texts). Reject first, as with the texts.
ONETRUST_BUTTONS = [("#onetrust-reject-all-handler", "Reject all (OneTrust)"),
                    ("#onetrust-accept-btn-handler", "Accept (OneTrust)"),
                    ("#accept-recommended-btn-handler", "Allow all (OneTrust)"),
                    (".onetrust-close-btn-handler", "Close (OneTrust)")]


async def _consent_button(container) -> tuple[Optional[object], str]:
    for selector, text in ONETRUST_BUTTONS:
        button = container.locator(selector).first
        try:
            if await button.count() and await button.is_visible():
                return button, text
        except Exception:
            continue
    for text in CONSENT_BUTTON_TEXTS:
        button = container.locator(
            f"button:has-text('{text}'), [role='button']:has-text('{text}'), a:has-text('{text}')"
        ).first
        try:
            if await button.count() and await button.is_visible():
                return button, text
        except Exception:
            continue
    return None, ""


async def accept_cookies(page, max_banners: int = 3) -> int:
    """Answer any cookie/consent popups (several can stack). Picks Decline/Reject
    when offered, else Accept. Waits for a reload after each click (Procart's
    Catapult Accept reloads the page). Returns how many popups were answered."""
    answered = 0
    for _ in range(max_banners):
        container_sel = await _first_visible(page, CONSENT_CONTAINERS)
        if not container_sel:
            break
        container = page.locator(container_sel).first
        button, text = await _consent_button(container)
        if button is None:
            logger.warning("Consent popup %s is showing but has no button we recognise "
                           "(%s); leaving it", container_sel, ", ".join(CONSENT_BUTTON_TEXTS))
            break
        logger.info("Consent popup %s: clicking %r", container_sel, text)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=5_000):
                await button.click(timeout=5_000)
            logger.info("Consent popup answered; page reloaded")
        except PlaywrightTimeoutError:
            logger.info("Consent popup answered; no reload")
        await _settle(page, timeout_ms=5_000)
        answered += 1
        try:
            if await page.locator(container_sel).first.is_visible():
                logger.warning("Consent popup %s still visible after clicking %r",
                               container_sel, text)
                break
        except Exception:
            pass
    return answered


# Page text without what opens under a search box on typing or Enter (recent
# searches, suggestions): that is not a search result (DASI's history drop-down
# made a search that never ran look like one, 2026-10-29).
_CONTENT_TEXT_JS = """() => {
  const b = document.body.cloneNode(true);
  b.querySelectorAll('script, style, noscript, [role=listbox], [role=option], [class*=dropdown i], '
    + '[class*=history i], [class*=suggest i], [class*=autocomplete i], [class*=typeahead i]')
    .forEach(e => e.remove());
  return (b.textContent || '').replace(/\\s+/g, ' ').trim();
}"""


async def content_text(page) -> str:
    try:
        return await page.evaluate(_CONTENT_TEXT_JS)
    except Exception:
        return ""


async def _body_text(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


async def wait_grid_idle(page, timeout_ms: int = GRID_IDLE_TIMEOUT_MS) -> None:
    """Wait while a jqGrid results grid shows its "Loading..." overlay. Never fatal."""
    try:
        await page.wait_for_function(_GRID_IDLE_JS, polling=500, timeout=timeout_ms)
    except Exception:
        logger.info("Results grid still shows 'Loading' after %ds; going on", timeout_ms // 1000)


async def expand_all_details(page, *, click_timeout_ms: int = 20_000) -> bool:
    """Click a visible "Expand all (details)" control, if the page has one, and wait
    for the rows to open: detail rows left closed (warehouses, condition, due
    dates on Procart) are missing from the page. True when one was clicked.
    Never fatal."""
    try:
        found = await asyncio.wait_for(page.evaluate(_MARK_EXPAND_ALL_JS), PAGE_SCRIPT_TIMEOUT)
        if not found:
            return False
        await page.click('[data-sourcing-expand="1"]', no_wait_after=True,
                         timeout=click_timeout_ms)
        await _settle(page, timeout_ms=10_000)
        await wait_grid_idle(page)
        logger.info("Results: expanded all details")
        return True
    except Exception as exc:
        logger.info("Results: could not expand details (%s); reading as shown",
                    str(exc).splitlines()[0][:200])
        return False


def dump_grid(label: str, text: str, records) -> None:
    """With DEBUG_DUMP_PAGES=1, save grid records the model was given (text + raw
    JSON) under debug_pages/, as page_text() does for page text."""
    if os.environ.get("DEBUG_DUMP_PAGES", "").strip() not in ("1", "true", "yes"):
        return
    try:
        from .grid import dumps

        DUMP_DIR.mkdir(exist_ok=True)
        stem = DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-grid"
        stem.with_suffix(".txt").write_text(text, encoding="utf-8")
        stem.with_suffix(".json").write_text(dumps(records), encoding="utf-8")
        logger.info("Saved grid records to %s.(txt|json)", stem)
    except Exception as exc:
        logger.warning("Could not save the grid records: %r", exc)
