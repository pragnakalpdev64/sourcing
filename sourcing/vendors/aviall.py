"""AviallAdapter: Aviall's store, now Boeing's (shop.boeing.com).

The whole job on Aviall, step by step (team, 2026-11-02). Each step has a goal
and a check that says it is done; the log names the step ("Aviall step ...").
When a step's own rules can't find the next element, the LLM is shown the goal,
the job and the page's visible elements (open pop-ups first) and picks one to
click or type into (_llm_step); it never picks a cart, order, delete or account
change, and what it picks is checked before it is used.

  1 sign in      login URL -> "Log in to Boeing Distribution" (a dialog) ->
                 "Log in with Boeing OneLogin ID" -> username -> password ->
                 2FA box: no code; the login URL again and both buttons again
                 done: the avatar beside the cart shows "Welcome, <name>" / Log Out
  2 search       several parts: "Multi-line Ordering" (a pop-over on the page) ->
                 paste "<part>, <qty>" lines into its Copy & Paste box -> Search
                 one part: boeingsearch?query=<part>
                 done: result cards, or the site's "unable to find" message
  3 read         close the feature tour; per card write in its full part number,
                 the Price Breaks and Stock By Location pop-ups, icon labels;
                 the LLM reads the page (aviall_prompt.py), every value checked
  4 finish       avatar -> Log Out -> "Are you sure you want to log out?" -> Log out
                 done: the home page's avatar no longer shows the signed-in menu
                 (not done after 2 tries: a person logs out through Live View)

Login (team's steps, 2026-11-02), from the login URL in the secret:
  1. click "Log in to Boeing Distribution"
  2. click "Log in with Boeing OneLogin ID" (wording roughly so)
  3. username page: type the username, submit
  4. password page: type the password, submit
  5. a 2FA code box shows: no code is typed; the login URL is opened again
     and that signs us in (the team's way)
  6. not signed in then (a sign-in form or a code box again): stops and asks
     the person who owns the login (the usual handoff, sourcing/auth/hitl.py).
Steps 1-2 are buttons, clicked only on Boeing's / Aviall's sites; if neither
wording is found the LLM picks the button. Step 1 opens a dialog on the same
page, not a new page (team, 2026-11-02): after a click the program waits for
whatever comes next (a dialog, a button, a form, or a new page that stopped
changing). Step 5 runs once per login, goes through steps 1-2 again and types
nothing: it only works while Boeing's sign-in from step 4 is valid.

Boeing's site is big and slow at times (team, 2026-11-02), so its waits are
longer than other vendors'. A login that times out after the password was
sent may still have signed us in: the pipeline and onboarding log out before
closing the browser (base.may_be_signed_in), as after any run; an open
session blocks the team's own sign-in.

Searching (team's links and screenshots, 2026-10-30):

  one part   boeingsearch?query=<part>   ("=" is part of Aviall part numbers:
             021200-21181=32, so it is sent escaped, never cut off)
  bulk       /bulksearch, "Multi-line Ordering" button -> dialog; its Copy &
             Paste box takes "<part>, <qty>" one per line (max 150 lines),
             then the dialog's Search. The results list on /bulksearch.

Before a results page is read, the program writes into each result card what
the card shows only on demand (screenshots: stock location pin, Shelf Life
info, certificate icons, Price Breaks pop-up):
  - icon labels (alt / title / aria-label) as "[icon: ...]"
  - the text a small icon shows on hover, as "(shown on hover: ...)"
  - the Price Breaks pop-up (opened with a click, closed again), as
    "(Price Breaks pop-up: ...)"
then reads the page text, which Aviall's own prompt (aviall_prompt.py) knows.
Never clicked: Add to Cart, Certifications "...", Manufacturer / Revision /
Quality Docs "Select" (order choices, not values), the AOG switch.

Not yet seen by the program on the live portal: selectors are from the
screenshots. Vendor config `home_url`, `distribution_login_button`,
`onelogin_button`, `bulk_search_url`, `bulk_open_button`, `bulk_input`,
`bulk_submit_button` (CSS selectors), `hover_max` and `reveal_seconds`
override the defaults. A run with --dump-pages saves what was read.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date
from typing import Optional
from urllib.parse import unquote

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..conditions import CONDITION_CODES
from ..credentials.base import VendorCredential
from ..models import RequestRow, SearchOutcome
from . import web
from .aviall_prompt import AVIALL_EXTRACT_SYSTEM
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, LoginBlocked, LoginFailed, MfaRequired,
    SessionExpired, check_rate_limited, has_real_offer, host_allowed,
)
from .generic import GenericAdapter
from .web import _first_visible, _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.aviall")

HOME_URL = "https://shop.boeing.com/"
SEARCH_URL = "https://shop.boeing.com/boeingsearch?query={part}"
BULK_URL = "https://shop.boeing.com/bulksearch"
_CLICKABLE = ("a", "button", "[role='button']", "[role='link']")
# Sign-in steps 1 and 2 (team, 2026-11-02: "something like" these words; the
# program hasn't seen the pages yet). Not web.first_visible_button: that one
# skips "log in with <another account>" buttons, which step 2 is.
ONELOGIN_SELECTORS = [f"{tag}:has-text('{words}')" for words in
                      ("OneLogin", "One Login", "Boeing ID") for tag in _CLICKABLE]
DISTRIBUTION_LOGIN_SELECTORS = [f"{tag}:has-text('{words}')" for words in
                                ("Log in to Boeing Distribution", "Login to Boeing Distribution",
                                 "Sign in to Boeing Distribution", "Log in to Boeing",
                                 "Sign in to Boeing") for tag in _CLICKABLE]
# Buttons clicked before the username page: step 1, step 2, and one spare
# for a cookie or region page in between.
ENTRY_CLICKS_MAX = 3
# A page counts as loaded at its "load" event (up to LOAD_WAIT_MS), plus a
# short wait for the network to go quiet. Not the generic wait for a quiet
# network: Boeing's pages never go quiet, so each wait used its whole 20-30s
# (run log 2026-11-02, ~30s between two clicks). Every step waits for its own
# element instead (_click_and_wait).
LOAD_WAIT_MS = 15_000
QUIET_WAIT_MS = 2_000
# A new page counts as arrived once its address stopped changing this long
# (sign-in redirects pass through several addresses).
URL_STABLE_SECONDS = 2
# How often a wait looks at the page again. Each look is one query to the
# remote browser; the waits are what made a login take ~2 minutes (Browserbase
# log 2026-11-02: 23s from the login page to the OneLogin click, 35s for the
# re-login, of which the site itself needed a few seconds).
POLL_SECONDS = 0.3
# After the 2FA box the login URL (/aviation-supply) only redirects here:
# opened directly, one redirect fewer. The OneLogin button can't be skipped by
# a link: what it opens carries a one-time code_challenge made by the page's
# own script (auth.boeing.com/as/authorization.oauth2?...&code_challenge=...).
DISTRIBUTION_URL = "https://shop.boeing.com/distribution"
# Boeing's sign-in pages as seen in the run log 2026-11-02 (auth.boeing.com):
# the username box and its "LOG IN" button (not a "Next", so the LLM was asked).
# Used like selectors saved by an earlier run; one that stops fitting is skipped.
KNOWN_SELECTORS = {"username_input": '[id="username"]', "next_button": '[id="submitButton"]'}
# Signed in, every shop page shows the "Shopping as: <account> - SKYLINK INC"
# bar; there is no Log out link (My Account is a link to accounts.boeing.com,
# saved pages 2026-11-02). Without this the run hunted menus and clicked
# "Sign in" again while signed in.
SIGNED_IN_SELECTORS = [".accounts-bar .account-detail", "[class*='accounts-bar' i] [class*='account-detail' i]",
                       ":text('Shopping as:')"]
# The avatar beside the cart (saved pages 2026-11-02): its dropdown shows
# "Welcome, <name>", the accounts and a Log out button when signed in. The
# sign-in is confirmed there (team, 2026-11-02: as on DASI), not by opening My
# Account, which is a link to accounts.boeing.com.
AVATAR_BUTTONS = ["#my-account-profile-dropdown button.dropdown-toggle",
                  "bc-header-my-account button.dropdown-toggle"]
AVATAR_MENU = "#my-account-profile"
AVATAR_LOGOUT = [f"{AVATAR_MENU} button:has-text('Log out')", f"{AVATAR_MENU} a:has-text('Log out')",
                 f"{AVATAR_MENU} button:has-text('Sign out')"]
AVATAR_SIGNED_IN = [f"{AVATAR_MENU} .account-details", f"{AVATAR_MENU} :text('Welcome,')"]
AVATAR_SIGNED_OUT = [f"{AVATAR_MENU} button:has-text('Log in')", f"{AVATAR_MENU} a:has-text('Log in')",
                     f"{AVATAR_MENU} button:has-text('Sign in')", f"{AVATAR_MENU} a:has-text('Sign in')"]
# Log out is also a button on Boeing's account page (run log 2026-11-02): the
# fallback when the avatar's Log out can't be used.
ACCOUNT_URL = "https://accounts.boeing.com/accountSettings/accountDetails"
# WalkMe's "Tour Our New Features!" pop-up on results pages: it covers the cards
# (0 Price Breaks read with it showing, 2026-11-02).
TOUR_CLOSE_SELECTORS = ["button.wm-visual-design-button:has-text('No thanks')",
                        "button:has-text('No thanks')"]
# The tour is WalkMe's (#walkme-player, #walkme-overlay-all): it opens a while
# after the page and its overlay takes the clicks meant for the cards, so it is
# also hidden for the run. A product tour, not a check of any kind.
HIDE_TOUR_CSS = ("#walkme-player, #walkme-overlay-all, [id^='walkme-visual-design'], "
                 "[class*='wm-visual-design'] { display: none !important; }")
# Log Out in the avatar asks "Are you sure you want to log out?" in a dialog on
# the same page (team's screenshot, 2026-11-02): Cancel / Log out. The dialog
# is drawn only then, so it isn't in any saved page; its Log out was not a
# button the rules knew (live run 2026-11-02), so it is found by its words:
# a visible "Log out" link or button outside the avatar's dropdown.
CONFIRM_LOGOUT_JS = """() => {
  document.querySelectorAll('[data-sourcing-confirm]').forEach(e => e.removeAttribute('data-sourcing-confirm'));
  const shown = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const isLogout = el => /^\\s*(log ?out|sign ?out|yes,? log ?out)\\s*$/i.test(el.innerText || el.value || '');
  const controls = box => Array.from(box.querySelectorAll(
      'button, a, [role=button], input[type=submit], input[type=button]')).filter(shown).filter(isLogout);
  // The dialog: the smallest box around the question that holds a Log out.
  const questions = Array.from(document.querySelectorAll('body *')).filter(el => shown(el)
      && /are you sure you want to log ?out/i.test(Array.from(el.childNodes)
           .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ')));
  for (const q of questions) {
    for (let box = q.parentElement; box && box !== document.body; box = box.parentElement) {
      if (box.closest('#my-account-profile, bc-header-my-account')) break;
      const hits = controls(box);
      if (hits.length) {
        const el = hits[hits.length - 1];
        el.setAttribute('data-sourcing-confirm', '');
        return {tag: el.tagName.toLowerCase(), href: el.getAttribute('href') || '', in_dialog: true};
      }
    }
  }
  return {question: questions.length > 0};
}"""
# Boeing's sign-out passes through these (seen 2026-11-02): only followed when
# logging out, never typed into.
SIGN_OUT_HOSTS = ("boeing.com", "aviall.com", "aviationid.com", "boeingservices.com")
# Confirmation dialogs answered in one logout ("asked again", team 2026-11-02).
CONFIRM_ROUNDS = 3
# For the LLM when it picks the next element of a step.
FLOW_CONTEXT = ("SourcingAI looks up part prices on Boeing's shop (Boeing Distribution, formerly "
                "Aviall) for the Skylink team: sign in, search the part numbers, read prices and "
                "stock, then log out. It never adds to the cart, orders, deletes anything or "
                "changes the account or its settings.")
# Never clicked when the LLM picks (an allowed word list per step adds e.g. log out).
_NEVER_CLICK = re.compile(r"cart|checkout|buy|purchase|order now|place order|pay\b|add to|delete|"
                          r"remove|clear|save|submit order|change|edit|settings|register|create|"
                          r"log ?out|sign ?out", re.I)
# Aviall shows no condition and sells new stock; the client's RFQ rule quotes no
# in-stock line without a condition code (2026-11-02: every Aviall line was "No
# Quote"). Vendor config `condition_not_shown` changes it ("" leaves it empty).
CONDITION_NOT_SHOWN = "NE"
# The supplier whose cards are read (vendor config `supplier`, "" = all).
SUPPLIER = "Boeing Distribution (formerly Aviall)"
# What a results page shows only signed in (price, Add to Cart), and its words
# signed out ("log in to view the price", team 2026-11-02; exact wording not
# seen yet).
SIGNED_IN_RESULT_SELECTORS = ["[data-testid=productListView] button:has-text('Add to Cart')",
                              "[data-testid=panda-price]"]
SIGNED_OUT_RESULT_SELECTORS = [":text-matches('(log ?in|sign ?in) to (view|see)', 'i')"]
# The sign-in check waits this long for the "Shopping as" bar or a sign-in
# button on the home page.
PROBE_WAIT_SECONDS = 30
# How long the login page may take to show a sign-in button (or the signed-in bar).
LOGIN_PAGE_WAIT_SECONDS = 60
# The selectors seen on the live pages (2026-11-02), looked for before the
# general lists. A check of the general lists took 3-40s a time on Browserbase
# (a broad guess matching a hidden element first sends it through ~70
# selectors, two round trips each): the 98s login page of 2026-11-02 15:32.
# One known selector takes ~0.3s.
KNOWN_DISTRIBUTION_BUTTON = "button:has-text('Log in to Boeing Distribution')"
KNOWN_ONELOGIN_BUTTON = "button:has-text('OneLogin')"
KNOWN_SIGNED_IN = ".accounts-bar .account-detail"
KNOWN_USERNAME = '[id="username"]'
KNOWN_PASSWORD = "input[type='password']"
KNOWN_MFA = "input[autocomplete='one-time-code']"
# Akamai's block page (local headless Chromium got it, 2026-11-02): stop at once.
ACCESS_DENIED = "h1:has-text('Access Denied')"
LOGIN_PAGE_KNOWN = [KNOWN_SIGNED_IN, KNOWN_ONELOGIN_BUTTON, KNOWN_DISTRIBUTION_BUTTON,
                    KNOWN_USERNAME, KNOWN_PASSWORD, ACCESS_DENIED]
# The general lists are only looked at after this long without a known one.
KNOWN_FIRST_SECONDS = 20
# The product page (/cpd/...) has "Lead Time (Days) 80" under Technical
# Information (team's screenshot, 2026-11-02); the results card shows a lead
# time only as "Available To Order (30 days lead time)". A line that can be
# quoted only with a delivery (priced, out of stock, no lead time) gets its
# product page read, in a second tab; at most PRODUCT_PAGES_MAX a search.
# Vendor config `product_pages`: "when_needed" (default) or "off".
PRODUCT_PAGES_MAX = 10
PRODUCT_PAGE_WAIT_SECONDS = 45
LEAD_TIME_DAYS = re.compile(r"Lead Time \(Days\)\s*:?\s*(\d+)", re.I)
# "On Order (Available 11/26/2026)" on a card (2026-11-02, MS20995C32=8D): a date,
# which the RFQ's Delivery (a number of days) can't use; it went out as "No Quote".
AVAILABLE_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# A Superseded card's "View Replacement" (target=_blank, /cpd/bdi_<part>, saved
# page 2026-11-02, 161A2114-1=IH -> 161A2114-1=AG). When the replacement has
# no card of its own in the results, its product page is read in a second tab
# and added as its own block: the replacement is a substitute for a requested
# part that is superseded (team, 2026-11-03). At most REPLACEMENTS_MAX a page.
REPLACEMENTS_MAX = 5
REPLACEMENT_HEADER = ("--- Replacement product page {n} of {m}: {url} (opened by the program "
                      "from 'View Replacement' on the Superseded card {card}) ---")
REPLACEMENTS_JS = """() => [...document.querySelectorAll(
    "a[data-testid=panda-viewReplacement], a.view-replacement-button")].map(a => {
  const card = a.closest('[data-testid=productListView]');
  const note = card && (card.innerText.match(/\\[full part number: ([^\\]]+)\\]/) || [])[1];
  return {href: a.href, card: note || ''};
})"""
# For a vendor with substitutes off: only the numbers, no page opened. Each
# Superseded card: its own full part number and "Superseded by part number X".
REPLACEMENT_PNS_JS = """() => [...document.querySelectorAll(
    "a[data-testid=panda-viewReplacement], a.view-replacement-button")].map(a => {
  const card = a.closest('[data-testid=productListView]');
  const text = card ? card.innerText : '';
  const own = (text.match(/\\[full part number: ([^\\]]+)\\]/) || [])[1];
  const by = (text.match(/Superseded by part number\\s+(\\S+)/i) || [])[1];
  return {card: own || '', by: by || '', href: a.href};
})"""
_FULL_PN = re.compile(r"\[full part number: ([^\]]+)\]")
# Step 1's dialog (team, 2026-11-02); the generic ones too, in case it is drawn differently.
DIALOG_SELECTORS = ["[role='dialog']", "dialog[open]", "[aria-modal='true']",
                    "[class*='modal' i]:not([class*='backdrop' i])"]

BULK_OPEN_SELECTORS = [
    "button:has-text('Multi-line Ordering')", "a:has-text('Multi-line Ordering')",
    "[role='button']:has-text('Multi-line Ordering')",
]
# "Multi-line Ordering" opens a <boeing-generic-popover> on the page (saved
# HTML, 2026-11-02): a Line-by-line table (max 20) and the Copy & Paste box
# textarea.bulkSearchTextArea (max 150 lines), then Clear All / Search. The
# Search button is looked for inside the box's own pop-over only: the page has
# its own search box and button.
BULK_BOX_SELECTORS = ["textarea.bulkSearchTextArea", "boeing-generic-popover textarea",
                      "textarea[placeholder*='150 lines' i]"]
# The dialog's X (saved HTML: svg.closeBtn in .genericPopoverHeader).
BULK_CLOSE_SELECTORS = ["boeing-generic-popover .genericPopoverHeader .closeBtn",
                        "boeing-generic-popover .closeBtn", "boeing-generic-popover [aria-label*='close' i]"]
# Results are on the page (cards, "6 results"), or the not-found bar.
RESULT_MARKERS = ["[data-testid=productListView]", ":text-matches('^[0-9]+ results?$')"]
BULK_NOT_FOUND = ":text('We were unable to find')"
BULK_DIALOG_SELECTORS = ["boeing-generic-popover", "mat-dialog-container", "ngb-modal-window",
                         "[role='dialog']", "[aria-modal='true']", ".cdk-overlay-pane", ".modal.show"]
# Marks the box's container: the nearest ancestor holding a Search button.
MARK_BULK_JS = """(box) => {
  document.querySelectorAll('[data-sourcing-bulk]').forEach(e => e.removeAttribute('data-sourcing-bulk'));
  const words = b => [b.innerText, b.value, b.getAttribute('aria-label')].filter(Boolean).join(' ');
  for (let el = box.parentElement; el && el !== document.body; el = el.parentElement) {
    const hit = Array.from(el.querySelectorAll('button, [role=button], input[type=submit]'))
      .some(b => b.getClientRects().length && /\\bsearch\\b/i.test(words(b)));
    if (hit) { el.setAttribute('data-sourcing-bulk', ''); return true; }
  }
  return false;
}"""
# What the LLM is shown when it picks a step's element: visible inputs and
# buttons, those in an open pop-up / dialog first (the page header alone has
# more than the 80 the generic list keeps).
STEP_ELEMENTS_JS = """() => {
  document.querySelectorAll('[data-sourcing-el]').forEach(el => el.removeAttribute('data-sourcing-el'));
  const visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const layer = '[role=dialog], [aria-modal=true], .modal, boeing-generic-popover, [class*=popover], ' +
                '[class*=dropdown-menu], [class*=modal]';
  const all = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, ' +
      '[contenteditable=true], button, a[href], [role=button], [role=tab]')).filter(visible);
  const top = all.filter(el => el.closest(layer)), rest = all.filter(el => !el.closest(layer));
  return [...top, ...rest].slice(0, 80).map((el, i) => {
    el.setAttribute('data-sourcing-el', String(i));
    return {index: i, tag: el.tagName.toLowerCase(), type: el.getAttribute('type'),
            role: el.getAttribute('role'), id: el.id || null, name: el.getAttribute('name'),
            placeholder: (el.getAttribute('placeholder') || '').slice(0, 80) || null,
            aria_label: el.getAttribute('aria-label'), in_popup: !!el.closest(layer),
            text: ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? '' : el.innerText || '')
                    .trim().replace(/\\s+/g, ' ').slice(0, 60) || null,
            href: el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 120) : null};
  });
}"""
# The Copy & Paste box (line numbers drawn beside it: may be an editor, not a
# plain textarea). The Line-by-line boxes next to it take one part each.
BULK_INPUT_IN_DIALOG = ["textarea", "[contenteditable='true']"]
# A "Copy & Paste" tab may have to be opened before its box shows.
COPY_PASTE_TAB = ["[role='tab']:has-text('Copy')", "button:has-text('Copy')", "a:has-text('Copy')",
                  "label:has-text('Copy')", "span:has-text('Copy & Paste')"]
# "Include 4 recently searched part numbers": left ticked, it adds parts nobody asked for.
RECENT_PARTS_CHECKBOX = ["label:has-text('recently searched') input[type='checkbox']",
                         "input[type='checkbox']:near(:text('recently searched'), 40)"]
# The dialog's Search button: by its words, not exact text (Angular buttons
# hold an icon name too, "search Search"). Last match first: dialogs put their
# action buttons at the bottom.
BULK_SUBMIT_JS = """(el) => {
  const buttons = Array.from(el.querySelectorAll('button, [role=button], input[type=submit]'))
    .filter(b => b.getClientRects().length && !b.disabled);
  const words = b => [b.innerText, b.value, b.getAttribute('aria-label')].filter(Boolean).join(' ');
  const hits = buttons.map((b, i) => [i, words(b)]).filter(([i, w]) => /\\bsearch\\b/i.test(w));
  return hits.map(([i, w]) => ({index: i, words: w.replace(/\\s+/g, ' ').trim()}));
}"""
BULK_BUTTONS = "button, [role=button], input[type=submit]"
FOCUS_JS = "el => { el.scrollIntoView({block: 'center'}); el.focus(); }"
# A paste event with the text, as the clipboard would send it; when the box
# leaves pastes to the browser (a synthetic one inserts nothing), the text is
# put in and an input event sent.
PASTE_JS = """(el, text) => {
  el.focus();
  const data = new DataTransfer();
  data.setData('text/plain', text);
  const handled = !el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data,
                                                                  bubbles: true, cancelable: true}));
  if (!handled) {
    if ('value' in el) el.value = text; else el.innerText = text;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertFromPaste', data: text}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
}"""
BULK_MAX_LINES = 150
# The bulk page is an ordering page: a button with these words is never clicked
# to search (generic._NOT_A_SEARCH plus "add").
_NOT_A_BULK_SEARCH = re.compile(r"cart|checkout|buy|purchase|order now|place order|pay|"
                                r"add to|delete|remove|clear|cancel|log ?out|sign ?out", re.I)

# Hover / pop-up reading: most icons hovered per page and per card, and the
# time it may take. Hovering is off: on the live pages (2026-11-02) it only
# showed the tooltip "Stock By Location"; the values are in pop-overs opened
# by a click (MARK_JS). Vendor config hover_max / hover_per_card turn it on.
HOVER_MAX = 0
HOVER_PER_CARD = 2
REVEAL_SECONDS = 90
HOVER_WAIT_MS = 400
POPUP_WAIT_MS = 700
RESULTS_WAIT_MS = 15_000

# Marks each result card (data-sourcing-card), the icons to hover in it
# (data-sourcing-hover), its Price Breaks link (data-sourcing-pb), and writes
# icon labels into the card. A card is the largest box around one "Request
# Support" link (every card in the screenshots has one) that holds no other.
# Also writes the card's full part number: the card shows "021200-21181" for
# 021200-21181=32, only its product link (/cpd/bdi_021200-21181%3D32, lower
# case) has the "=" suffix (saved pages, 2026-11-02).
MARK_JS = """([maxHover, perCard, supplier]) => {
  const own = el => Array.from(el.childNodes).filter(n => n.nodeType === 3)
                        .map(n => n.textContent).join(' ').replace(/\\s+/g, ' ').trim();
  let anchors = Array.from(document.querySelectorAll('a, button, span, div, p'))
    .filter(el => own(el) === 'Request Support' || el.textContent.trim() === 'Request Support'
                  && !Array.from(el.children).some(c => c.textContent.trim() === 'Request Support'));
  const inside = (box) => anchors.filter(a => box.contains(a)).length;
  // One product: links to at most one product page, and not the page header
  // (a single result grew to the whole page: 23 header icons hovered, 2026-11-02).
  // A Superseded card's "View Replacement" links to the replacement's page: not
  // the card's own product (it gave the card the wrong full number, 2026-11-02).
  const ownLinks = box => Array.from(box.querySelectorAll("a[href*='/cpd/']"))
                             .filter(a => !/replacement|superseded/i.test(a.innerText || ''));
  const products = box => new Set(ownLinks(box)
                                    .map(a => { const h = (a.getAttribute('href') || '').toLowerCase();
                                                try { return decodeURIComponent(h); } catch (e) { return h; } })).size;
  const grows = box => inside(box) === 1 && products(box) <= 1
                       && !box.querySelector('header, bc-header-my-account, [class*=accounts-bar]');
  // The site's own card (saved pages 2026-11-02): one per result. Grown from
  // "Request Support" otherwise; a single result then took in the breadcrumb
  // "Search : AV2539512=F1", which hid the card's missing "=F1".
  const cards = Array.from(document.querySelectorAll(
      "[data-testid=productListView], .brandx-plp-single-item-container"))
    .filter(c => !c.parentElement.closest("[data-testid=productListView], .brandx-plp-single-item-container"));
  if (!cards.length) for (const a of anchors) {
    let el = a;
    while (el.parentElement && el.parentElement !== document.body && grows(el.parentElement))
      el = el.parentElement;
    if (!cards.includes(el)) cards.push(el);
  }
  let hovers = 0, labels = 0, breaks = 0, fulls = 0;
  const links = [];
  // "/cpd/bdi_021200-21181%3D32" -> "021200-21181=32" ("bdsis4_" on BDSI parts)
  const slug = a => {
    try {
      return decodeURIComponent(a.getAttribute('href').split('/cpd/').pop().split(/[?#]/)[0])
               .replace(/^[a-z]+\\d*_/i, '').toUpperCase();
    } catch (e) { return ''; }
  };
  const near = icon => {            // stock / shipping words within 3 levels up
    let el = icon.parentElement;
    for (let k = 0; el && k < 3; k++, el = el.parentElement)
      if (/stock|ship|location|warehouse|available|lead time/i.test(own(el) || el.innerText || '')) return true;
    return false;
  };
  // Only the vendor's own supplier (team, 2026-11-02: Aviall = "Boeing
  // Distribution (formerly Aviall)"; the same search also lists "Boeing
  // Distribution Services"). Other suppliers' cards leave the page before it is
  // read, as the site's Supplier filter would.
  const norm = t => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const others = [];
  const kept = cards.filter(card => {
    const sup = card.querySelector('[data-testid=panda-supplierName]');
    if (!supplier || !sup || norm(sup.innerText) === norm(supplier)) return true;
    others.push(norm(sup.innerText));
    card.remove();
    return false;
  });
  kept.forEach((card, i) => {
    card.setAttribute('data-sourcing-card', String(i));
    const text = card.innerText || '';
    const link = ownLinks(card)[0];
    if (link) {
      const full = slug(link);
      if (full && full.includes('=') && !text.toUpperCase().includes(full)) {
        const s = document.createElement('span');
        s.setAttribute('data-sourcing-note', '');
        s.textContent = ' [full part number: ' + full + '] ';
        link.insertAdjacentElement('afterend', s);
        fulls++;
      }
    }
    const hoverable = [];
    card.querySelectorAll('svg, img, i, [role=img]').forEach(icon => {
      if (icon.parentElement && icon.parentElement.closest('svg')) return;     // part of an icon
      const r = icon.getBoundingClientRect();
      if (!r.width || !r.height || r.width > 48 || r.height > 48) return;     // product image
      if (icon.closest('input, [role=spinbutton], [role=switch]')) return;
      const btn = icon.closest('button, a');
      if (btn && (btn.innerText || '').trim()) return;                         // Add to Cart, links
      const label = (icon.getAttribute('alt') || icon.getAttribute('title') ||
                     icon.getAttribute('aria-label') || '').trim();
      if (label && !text.includes(label) && !/^image not available$/i.test(label)) {
        const s = document.createElement('span');
        s.setAttribute('data-sourcing-note', '');
        s.textContent = ' [icon: ' + label + '] ';
        icon.insertAdjacentElement('afterend', s);
        labels++;
      }
      if (near(icon)) hoverable.push(icon);
    });
    hoverable.slice(0, perCard).forEach(icon => {
      if (hovers < maxHover) { icon.setAttribute('data-sourcing-hover', ''); hovers++; }
    });
    // Pop-ups opened by a click (saved pages 2026-11-02): buttons that open an
    // Angular pop-over (popoverclass / ngbpopover: Price Breaks
    // "uom-popup-container", the stock pin "warehouse-stock-info" = Stock By
    // Location, the Shelf Life info) and icon-only buttons. Never a link, a
    // form's submit, the quantity +/-, a switch, or an order / list / cart
    // control: a click must leave the page where it is.
    card.querySelectorAll('button, [role=button], [ngbpopover]').forEach(el => {
      if (el.matches('a[href]') || el.closest('a[href]') || el.closest('[data-sourcing-pb]')) return;
      if (el.type === 'submit' && el.closest('form')) return;
      if (el.closest("[class*=quantity], [class*=qty], [role=switch], input")) return;
      const words = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      if (/add to|cart|list|request|support|share|select|aog|order|remove|delete/i.test(words)) return;
      const popover = el.getAttribute('popoverclass') || (el.hasAttribute('ngbpopover') ? 'pop-over' : '');
      const icon = el.querySelector('img, svg, i');
      if (!popover && !(icon && !words)) return;
      // Shelf Life pop-ups (its info text, its per-location table behind
      // "expand_more") are no field: skipped, they took the time the Stock By
      // Location ones needed (2026-11-02, 17:27 run).
      if (/shelflife/i.test(popover) || /^expand_(more|less)$/i.test(words)) return;
      const tip = el.querySelector('[ngbtooltip]');
      const name = words || (tip && tip.getAttribute('ngbtooltip')) || el.getAttribute('aria-label') ||
                   el.getAttribute('title') || (icon && (icon.getAttribute('alt') || icon.getAttribute('title'))) ||
                   popover || 'info';
      el.setAttribute('data-sourcing-pb', name.slice(0, 40)); breaks++;
      // Stock By Location and Price Breaks first: they fill warehouses / price breaks.
      el.setAttribute('data-sourcing-pb-rank',
                      /stock|warehouse|price|uom-popup/i.test(name + ' ' + popover) ? '0' : '1');
    });
    // The card's product page, for the step that reads its lead time.
    if (link) links.push({shown: (link.innerText || '').trim(), href: link.href,
                          full: slug(link)});
  });
  return {cards: kept.length, hovers, labels, breaks, fulls, links, others};
}"""
# Opens each marked pop-up button in the page itself, reads what opened, writes
# it into the card and closes it again: one round trip for all of them. One
# by one from here took ~9s a pop-up on Browserbase (10 of 18 read in the 90s,
# 2026-11-02 17:27). A click that leaves the page stops it.
READ_POPUPS_JS = """async ([sel, deadlineMs]) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const shown = () => Array.from(document.querySelectorAll(sel))
    .filter(e => e !== document.body && e.getClientRects().length)
    .map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim()).filter(t => t && t.length < 3000);
  const start = location.href, until = Date.now() + deadlineMs;
  const targets = Array.from(document.querySelectorAll('[data-sourcing-pb]'))
    .sort((a, b) => (a.getAttribute('data-sourcing-pb-rank') || '1') < (b.getAttribute('data-sourcing-pb-rank') || '1') ? -1 : 1);
  let read = 0, tried = 0;
  for (const el of targets) {
    if (Date.now() > until) break;
    tried++;
    const before = new Set(shown());
    el.click();
    // Wait for new pop-up text, then until it stops changing (the stock list
    // is fetched after the pop-up opens).
    let text = '', same = 0;
    for (let i = 0; i < 40 && same < 3; i++) {
      await sleep(100);
      const now = shown().filter(t => !before.has(t)).sort((a, b) => b.length - a.length)[0] || '';
      same = (now && now === text) ? same + 1 : 0;
      text = now || text;
    }
    if (location.href !== start) return {read, tried, total: targets.length, left: location.href};
    el.click();                                       // a second click closes it
    await sleep(150);
    // Still open (a pop-up that doesn't toggle): Escape, or its text would be
    // read as part of the next card.
    if (document.querySelector('ngb-popover-window') || (text && shown().includes(text))) {
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      await sleep(150);
    }
    if (text) {
      const s = document.createElement('span');
      s.setAttribute('data-sourcing-note', '');
      s.textContent = ' (' + el.getAttribute('data-sourcing-pb') + ' pop-up: ' + text + ') ';
      el.insertAdjacentElement('afterend', s);
      read++;
    }
  }
  return {read, tried, total: targets.length, left: null};
}"""
# What a hover or click opened: the text of a tooltip / pop-up that wasn't
# showing before (snapshot = the texts showing before), else the lines of the
# page that are new (counted, so a repeated "1" still shows).
POPUPS = ("[role=tooltip], [role=dialog], [class*='tooltip' i], [class*='popover' i], "
          "[class*='popup' i], [class*='modal' i], [class*='flyout' i]")
SNAPSHOT_JS = """(sel) => ({
  popups: Array.from(document.querySelectorAll(sel)).filter(e => e !== document.body && e.getClientRects().length)
            .map(e => (e.innerText || '').trim()),
  lines: (document.body.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean)})"""
OPENED_JS = """([sel, before]) => {
  const now = Array.from(document.querySelectorAll(sel)).filter(e => e !== document.body && e.getClientRects().length)
    .map(e => (e.innerText || '').trim())
    .filter(t => t && t.length < 3000 && !before.popups.includes(t));
  if (now.length) return now.sort((a, b) => b.length - a.length)[0];
  const seen = {};
  before.lines.forEach(l => { seen[l] = (seen[l] || 0) + 1; });
  const fresh = [];
  (document.body.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean).forEach(l => {
    if (seen[l]) seen[l]--; else fresh.push(l);
  });
  return fresh.join('\\n').slice(0, 1500);
}"""
# Writes what was read into the card, next to the element it came from.
NOTE_JS = """(el, text) => {
  const s = document.createElement('span');
  s.setAttribute('data-sourcing-note', '');
  s.textContent = ' ' + text + ' ';
  el.insertAdjacentElement('afterend', s);
}"""


async def _loaded(page) -> None:
    try:
        await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
    except PlaywrightTimeoutError:
        pass
    await _settle(page, QUIET_WAIT_MS)


async def _visible(locator) -> bool:
    try:
        return await locator.is_visible()
    except Exception:
        return False              # the page is navigating


class AviallAdapter(GenericAdapter):
    vendor_id = "aviall"
    display_name = "Aviall"
    allowed_hosts = ("boeing.com", "aviall.com")
    default_login_url = HOME_URL
    default_part_url_template = SEARCH_URL
    extract_system = AVIALL_EXTRACT_SYSTEM
    # Not Aviall's confirmed limit: the batch size used for Procart and DASI.
    max_parts_per_search = 25
    default_bulk_batch_size = 25
    default_bulk_min_parts = 2
    # No substitutes for Aviall for now (team, 2026-11-03): View Replacement pages
    # aren't opened and no substitute line is written. show_substitutes: true
    # in the vendor config turns them back on.
    default_show_substitutes = False
    # Boeing's site is slow at times (team, 2026-11-02); the generic 20s / 90s
    # timed out. Vendor config action_timeout_seconds / page_load_timeout_seconds
    # still override these.
    default_action_timeout_seconds = 45
    default_page_load_timeout_seconds = 180

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        for key, selector in KNOWN_SELECTORS.items():
            self.selectors.setdefault(key, selector)
        self.relogin_tried = False
        self.login_start_url: Optional[str] = None
        # Product page of each card read this run: full / shown part number -> URL.
        self.product_links: dict[str, str] = {}
        # Replacements of the Superseded cards on the page just read: full part
        # number (upper case) -> numbers (substitutes off, listed_alternates).
        self._replacements: dict[str, list[str]] = {}
        self._cookies_seen: set[str] = set()
        self._actions = 0
        self._tracing = False

    @property
    def home_url(self) -> str:
        return self.config.get("home_url") or HOME_URL

    @property
    def probe_url(self) -> str:
        # Not the page the login ended on (accounts.boeing.com, or none): the
        # sign-in check found "no page to check" and logged in again (2026-11-02).
        return self.config.get("probe_url") or self.home_url

    async def _logout_link(self, page) -> Optional[str]:
        """What proves the sign-in, as a logout link does elsewhere (the generic
        login, the Live View check): a Log out link, else the "Shopping as" bar.
        Never clicked to log out (logout() uses the account page)."""
        return await super()._logout_link(page) or await self._shopping_as(page)

    async def _shopping_as(self, page) -> Optional[str]:
        return await _first_visible(page, [self.config.get("logged_in_selector"),
                                           *SIGNED_IN_SELECTORS])

    async def _avatar(self, page, *, keep_open: bool = False) -> tuple[Optional[str], Optional[str]]:
        """Open the avatar's dropdown: ("signed_in", its Log out button or welcome
        line), ("signed_out", its Log in button), or (None, None) when there is no
        avatar or its dropdown shows neither. Closed again unless keep_open."""
        button = await _first_visible(page, [self.config.get("avatar_button"), *AVATAR_BUTTONS])
        if not button:
            return None, None
        try:
            if await page.locator(button).first.get_attribute("aria-expanded") != "true":
                self._act("click", button, page)
                await page.click(button, timeout=self.action_timeout_ms)
            seen = await self._wait_for_any(page, [*AVATAR_LOGOUT, *AVATAR_SIGNED_IN,
                                                   *AVATAR_SIGNED_OUT], 10)
        except Exception as exc:
            logger.info("Avatar %s: could not open it (%s)", button, str(exc).splitlines()[0])
            return None, None
        state = ("signed_in" if seen in AVATAR_LOGOUT or seen in AVATAR_SIGNED_IN
                 else "signed_out" if seen else None)
        if state == "signed_in" and seen not in AVATAR_LOGOUT:
            seen = await _first_visible(page, AVATAR_LOGOUT) or seen
        logger.info("Avatar %s: %s (%s)", button, state or "dropdown shows neither", seen)
        if not keep_open:
            await self._close_avatar(page, button)
        return state, seen

    async def _close_avatar(self, page, button: Optional[str] = None) -> None:
        try:
            await page.keyboard.press("Escape")
            button = button or await _first_visible(page, AVATAR_BUTTONS)
            if button and await page.locator(button).first.get_attribute("aria-expanded") == "true":
                self._act("click", button, page)
                await page.click(button, timeout=5000)
        except Exception:
            pass

    async def is_authenticated(self, page) -> bool:
        """Signed in? First the page the run is on (a results page shows the
        "Shopping as" bar and Add to Cart): no page to open, which cost ~20s
        after each part not found (2026-11-02, 18:09). Else the home page:
        the bar or the sign-in button, the avatar's menu only when neither shows."""
        self.apply_timeouts(page)
        if host_allowed(page.url, self.hosts):
            here = await _first_visible(page, [KNOWN_SIGNED_IN, *SIGNED_IN_RESULT_SELECTORS])
            if here:
                logger.info("Auth probe %s: %s on this page -> signed in", page.url, here)
                self._login_ok = True
                return True
        self._act("open", self.probe_url, page)
        await page.goto(self.probe_url, wait_until="domcontentloaded")
        seen = await self._wait_known(page, [KNOWN_SIGNED_IN, KNOWN_DISTRIBUTION_BUTTON, ACCESS_DENIED],
                                      [*SIGNED_IN_SELECTORS, *DISTRIBUTION_LOGIN_SELECTORS,
                                       *AVATAR_BUTTONS], PROBE_WAIT_SECONDS)
        # The "Shopping as" bar or the sign-in button answer it without a click;
        # only when neither shows is the avatar's menu opened.
        if seen in (KNOWN_SIGNED_IN, *SIGNED_IN_SELECTORS):
            state, proof = "signed_in", seen
        elif seen in (KNOWN_DISTRIBUTION_BUTTON, *DISTRIBUTION_LOGIN_SELECTORS, ACCESS_DENIED):
            state, proof = "signed_out", seen
        else:
            state, proof = await self._avatar(page)
            if state is None:
                proof = await self._shopping_as(page)
                state = "signed_in" if proof else "signed_out"
        logger.info("Auth probe %s: %s -> %s", page.url, proof or seen or "nothing known shown",
                    "signed in" if state == "signed_in" else "not signed in")
        if state == "signed_in":
            self._login_ok = True
        return state == "signed_in"

    def _act(self, what: str, target, page) -> None:
        """One numbered log line per action on the page (team, 2026-11-02: every
        click and step recorded, to see exactly what a run did)."""
        self._actions += 1
        logger.info("Aviall action %d: %s %s (at %s)", self._actions, what, target,
                    page.url[:120])

    def _watch(self, page) -> None:
        """Logs each address the page goes to, redirects included."""
        if getattr(page, "_sourcing_watched", False):
            return
        page._sourcing_watched = True

        def on_nav(frame) -> None:
            if frame == page.main_frame:
                logger.info("Aviall page: %s", frame.url[:160])
        page.on("framenavigated", on_nav)

    async def _start_trace(self, page) -> None:
        """DEBUG_TRACE=1: a Playwright trace of the run (each action with a
        screenshot and the page before / after), saved at logout or when the
        login fails; open it with `playwright show-trace <file>`."""
        if self._tracing or os.environ.get("DEBUG_TRACE", "").strip() not in ("1", "true", "yes"):
            return
        try:
            await page.context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True
            logger.info("Aviall: Playwright trace on")
        except Exception as exc:
            logger.info("Aviall: no Playwright trace (%r)", exc)

    async def _save_trace(self, page, label: str) -> None:
        if not self._tracing:
            return
        self._tracing = False
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-trace.zip"
        try:
            await page.context.tracing.stop(path=str(path))
            logger.info("Aviall: trace saved to %s (playwright show-trace %s)", path, path)
        except Exception as exc:
            logger.info("Aviall: trace not saved (%r)", exc)

    async def _wait_known(self, page, known: list[str], broad: list[str],
                          seconds: float = 0) -> Optional[str]:
        """The known selectors for KNOWN_FIRST_SECONDS, then the general lists
        too, up to `seconds` in all (default: the page-load timeout)."""
        total = seconds or self.page_load_timeout_ms / 1000
        started = time.time()
        seen = await self._wait_for_any(page, known, min(KNOWN_FIRST_SECONDS, total))
        if seen:
            return seen
        logger.info("Aviall: none of the known selectors after %.0fs at %s; looking wider",
                    time.time() - started, page.url)
        return await self._wait_for_any(page, [*known, *broad],
                                         max(1.0, total - (time.time() - started)))

    async def _cookies(self, page) -> None:
        """The cookie banner, once per page: each look at it costs round trips,
        and an unanswered one was looked at after every click (2026-11-02)."""
        key = page.url.split("?")[0].split("#")[0]
        if key in self._cookies_seen:
            return
        self._cookies_seen.add(key)
        await self._cookies(page)

    async def _wait_for_any(self, page, selectors: list[str], seconds: float = 0) -> Optional[str]:
        """The first of these to show, polled (Boeing's pages draw late), up to
        `seconds` (default: the page-load timeout)."""
        deadline = time.time() + (seconds or self.page_load_timeout_ms / 1000)
        while True:
            seen = await _first_visible(page, selectors)
            if seen or time.time() >= deadline:
                return seen
            await asyncio.sleep(POLL_SECONDS)

    def confirmed_learned(self) -> dict:
        out = super().confirmed_learned()
        if self._login_ok:
            for key in ("distribution_login_button", "onelogin_button"):
                if self.learned.get(key):
                    out[key] = self.learned[key]
        return out

    @property
    def part_url(self) -> str:
        """Vendor record `part_url_template`, else Aviall's search address."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return SEARCH_URL

    async def login(self, page, cred: VendorCredential) -> None:
        self.relogin_tried = False        # once per login, not once per run
        self._watch(page)
        await self._start_trace(page)
        try:
            await super().login(page, cred)
        except BaseException:
            await self._save_trace(page, "aviall-login-failed")
            raise

    @property
    def signed_out_check_url(self) -> str:
        return self.home_url

    async def logout(self, page) -> bool:
        """Log out on Boeing's account page (the shop pages have no Log out), then
        check the home page no longer shows "Shopping as". A session left open
        blocks the team's own sign-in on other devices (team, 2026-11-02)."""
        self.apply_timeouts(page)        # the longer waits, also after an error
        try:
            return await self._logout(page)
        finally:
            await self._save_trace(page, "aviall-run")

    async def _logout(self, page) -> bool:
        if await self._logout_from_avatar(page):
            return True
        url = self.config.get("logout_page_url") or ACCOUNT_URL
        self._act("open", url, page)
        await page.goto(url, wait_until="domcontentloaded")
        button = await self._wait_for_any(page, [*web.LOGOUT_SELECTORS, *DISTRIBUTION_LOGIN_SELECTORS,
                                                 *web.PASSWORD_SELECTORS, *web.USERNAME_SELECTORS], 60)
        if button in web.LOGOUT_SELECTORS:
            logger.info("Logout: clicking %s at %s", button, page.url)
            self._act("click", button, page)
            await page.click(button, timeout=self.action_timeout_ms)
            await self._confirm_logout(page)
        else:
            logger.info("Logout: no Log out button at %s (%s)", page.url, button or "nothing shown")
        return await self._signed_out_at_home(page)

    async def _logout_from_avatar(self, page) -> bool:
        """The avatar's Log out, from the home page (the page the run ended on
        may be stuck or have no header)."""
        try:
            self._act("open", self.home_url, page)
            await page.goto(self.home_url, wait_until="domcontentloaded")
            await self._wait_for_any(page, [*AVATAR_BUTTONS, *DISTRIBUTION_LOGIN_SELECTORS], 30)
            state, button = await self._avatar(page, keep_open=True)
            if state == "signed_out":
                await self._close_avatar(page)
                logger.info("Logout: the avatar says signed out already")
                return True
            if state != "signed_in" or button not in AVATAR_LOGOUT:
                await self._close_avatar(page)
                return False
            logger.info("Aviall step 4 (finish): clicking %s in the avatar's dropdown", button)
            self._act("click", button, page)
            await page.click(button, timeout=self.action_timeout_ms)
            await self._confirm_logout(page)
            return await self._signed_out_at_home(page)
        except Exception as exc:
            logger.warning("Logout from the avatar failed: %r; trying the account page", exc)
            return False

    async def _confirm_logout(self, page) -> None:
        """Each "Are you sure you want to log out?" dialog -> its own Log out
        (the team saw it asked again, 2026-11-02), then wait for Boeing's
        sign-out to finish (it passes through logout.boeingservices.com and
        auth.boeing.com)."""
        for n in range(1, CONFIRM_ROUNDS + 1):
            if not await self._confirm_once(page, n):
                return

    async def _confirm_once(self, page, n: int) -> bool:
        """One dialog answered: True. No dialog within its wait: False."""
        confirm, question, deadline = None, False, time.time() + (15 if n == 1 else 8)
        while time.time() < deadline and not confirm:
            await asyncio.sleep(1)
            try:
                found = await page.evaluate(CONFIRM_LOGOUT_JS)
            except Exception:
                continue                   # the page is navigating
            question = question or bool(found.get("question") or found.get("in_dialog"))
            href = found.get("href") or ""
            if found.get("in_dialog") and (not href.startswith("http")
                                           or host_allowed(href, SIGN_OUT_HOSTS)):
                confirm = "[data-sourcing-confirm]"
                logger.info("Logout: dialog %d's Log out is a %s %s", n, found["tag"],
                            href or "(no link)")
        if not confirm and question:
            confirm = await self._llm_step(page, "the button or link that confirms 'Are you sure "
                                                 "you want to log out?' (Log out, not Cancel)",
                                           allow=re.compile(r"log ?out|sign ?out", re.I),
                                           hosts=SIGN_OUT_HOSTS)
        if not confirm:
            if n == 1 or question:
                await self._dump_html(page, f"aviall-logout-dialog-{n}", "body")
            logger.info("Logout: no%s confirmation dialog at %s", " further" if n > 1 else "",
                        page.url)
            await _loaded(page)
            return False
        logger.info("Aviall step 4 (finish): confirming the logout in dialog %d", n)
        try:
            await self._submit_and_wait(page, lambda: page.click(confirm, no_wait_after=True,
                                                                 timeout=10_000),
                                        "Logout confirm")
        except PlaywrightTimeoutError:
            # Covered by the dialog's own layer: the element's own click.
            self._act("click from code", "(covered element)", page)
            await page.locator(confirm).first.evaluate("el => el.click()")
            await _loaded(page)
        return True

    async def _signed_out_at_home(self, page) -> bool:
        """Signed out only on proof: the login page shows "Log in to Boeing
        Distribution" again and no "Shopping as" bar. One page: the home page
        shows "Log In" buttons for other Boeing shops even when signed in (team's
        screenshot, 2026-11-02), so it proves nothing, and opening it as well
        cost ~15s a logout. The avatar's menu only when neither shows."""
        url = self.login_start_url or self.config.get("login_url") or DISTRIBUTION_URL
        if not host_allowed(url, self.hosts):
            url = self.home_url
        self._act("open", url, page)
        await page.goto(url, wait_until="domcontentloaded")
        seen = await self._wait_known(page, [KNOWN_DISTRIBUTION_BUTTON, KNOWN_SIGNED_IN],
                                      [*DISTRIBUTION_LOGIN_SELECTORS, *SIGNED_IN_SELECTORS,
                                       *AVATAR_BUTTONS], 30)
        bar = await self._shopping_as(page)
        if seen in (KNOWN_DISTRIBUTION_BUTTON, *DISTRIBUTION_LOGIN_SELECTORS) and not bar:
            signed_out = True
        elif bar:
            signed_out = False
        else:
            state, _ = await self._avatar(page)
            signed_out = state == "signed_out"
        logger.info("Logout: %s (%s shows %s)", "signed out" if signed_out else "not confirmed",
                    page.url, bar or seen or "neither the sign-in button nor the 'Shopping as' bar")
        return signed_out

    # -- sign-in steps 1-2: buttons before the username page --------------------
    async def _open_login_page(self, page, cred: VendorCredential) -> bool:
        """Open the login URL and click through to the username page. False when
        it turns out to be signed in already."""
        candidates = self.login_url_candidates(cred)
        if not candidates:
            raise LoginFailed(f"No usable login URL for {self.display_name}: set url in its "
                              "secret (on one of allowed_hosts)",
                              retryable=False, credentials_sent=False)
        tried = []
        for source, url in candidates:
            logger.info("Login: opening %s (from %s)", url, source)
            started = time.time()
            self._act("open", url, page)
            await page.goto(url, wait_until="domcontentloaded")
            seen = await self._wait_known(page, LOGIN_PAGE_KNOWN,
                                          [*SIGNED_IN_SELECTORS, *DISTRIBUTION_LOGIN_SELECTORS,
                                           *ONELOGIN_SELECTORS, *web.LOGIN_LINK_SELECTORS,
                                           *web.PASSWORD_SELECTORS, *web.USERNAME_SELECTORS,
                                           *web.ACCOUNT_MENU_SELECTORS], LOGIN_PAGE_WAIT_SECONDS)
            logger.info("Aviall step 1 (sign in): %s shows %s after %.1fs", page.url,
                        seen or "nothing known", time.time() - started)
            # A cookie banner or the feature tour lying over the sign-in button
            # made its click time out twice (2026-11-02, /aviation-supply now
            # goes to /distribution).
            await self._cookies(page)
            await self._hide_tour(page)
            if not host_allowed(page.url, self.hosts):
                logger.warning("Login: %s went to %s, outside %s; not used", url, page.url,
                               ", ".join(self.hosts))
                tried.append(f"{url} (off-site)")
                continue
            # Not saved as login_page_url (generic): the username page's address
            # comes from Boeing's sign-in chain and may not open on its own.
            self.login_start_url = url
            state = await self._click_to_form(page)
            if state == "form":
                return True
            if state == "signed_in":
                return False
            tried.append(f"{url} ({state})")
            await self._dump_html(page, "aviall-login-stuck", "body")
        raise LoginFailed("No sign-in form found (tried: " + "; ".join(tried) + ")",
                          credentials_sent=False)

    async def _click_to_form(self, page) -> str:
        """Click "Log in to Boeing Distribution", then "Log in with Boeing
        OneLogin ID" (in the dialog the first one opens), until a username or
        password field shows. "form", "signed_in", or why it stopped ("2FA box"
        among them)."""
        onelogin_clicked = False
        for _ in range(ENTRY_CLICKS_MAX + 1):
            await self._stop_if_blocked(page)               # lockout: stop; CAPTCHA: retried
            if await self._has_form(page) or (onelogin_clicked and await self._pick(
                    page, "username_input", web.USERNAME_SELECTORS)):
                logger.info("Login: sign-in form at %s", page.url)
                return "form"
            if await self._mfa_input(page):
                return "2FA box"
            button, step = await self._entry_button(page)
            # No sign-in button in view: signed in already? After the OneLogin
            # click with Boeing's sign-in still valid (the reopen after the 2FA
            # box), Boeing goes back to the same address, signed in (2026-11-02).
            if not button and await self._signed_in_here(page):
                return "signed_in"
            if not button:
                button, step = await self._login_link(page), "Log in link"
            if not button:
                button, step = await self._llm_login_button(page), "Log in button (LLM)"
            if not button:
                return f"no sign-in button at {page.url}"
            logger.info("Login: clicking %s (%s)", button, step)
            await self._click_and_wait(page, button, step)
            await self._cookies(page)
            if not host_allowed(page.url, self.hosts):
                logger.warning("Login: %s led to %s, outside %s; not used", step, page.url,
                               ", ".join(self.hosts))
                return f"{step} went off-site to {page.url}"
            onelogin_clicked = onelogin_clicked or step == "OneLogin ID button"
        return f"no sign-in form after {ENTRY_CLICKS_MAX} clicks"

    async def _entry_button(self, page) -> tuple[Optional[str], str]:
        """Step 2's button first (once it shows, step 1 is behind us), then step 1's.
        The known selectors in one look; the general lists only when neither shows."""
        # Step 2's button (known, then the general list) before step 1's: with
        # the dialog open, step 1's button is still on the page.
        button = (await _first_visible(page, [self.config.get("onelogin_button"),
                                              KNOWN_ONELOGIN_BUTTON])
                  or await self._onelogin_button(page))
        if button:
            self._learn("onelogin_button", button)
            return button, "OneLogin ID button"
        known = await _first_visible(page, [self.config.get("distribution_login_button"),
                                            KNOWN_DISTRIBUTION_BUTTON])
        if known:
            self._learn("distribution_login_button", known)
            return known, "Boeing Distribution login button"
        button = await self._onelogin_button(page)
        if button:
            self._learn("onelogin_button", button)
            return button, "OneLogin ID button"
        button = await _first_visible(page, [self.config.get("distribution_login_button"),
                                             await self._saved_fit(page, "distribution_login_button"),
                                             *DISTRIBUTION_LOGIN_SELECTORS])
        if button:
            self._learn("distribution_login_button", button)
            return button, "Boeing Distribution login button"
        return None, ""

    async def _onelogin_button(self, page) -> Optional[str]:
        return await _first_visible(page, [self.config.get("onelogin_button"),
                                           await self._saved_fit(page, "onelogin_button"),
                                           *ONELOGIN_SELECTORS])

    async def _llm_login_button(self, page) -> Optional[str]:
        """No wording known to the code found the button: the LLM's pick."""
        return await self._llm_pick_one(
            page, "the button or link to log in: 'Log in with Boeing OneLogin ID' (or similar) "
                  "if there is one, else 'Log in to Boeing Distribution' (or similar), else the "
                  "Log in / Sign in link. Never a 'create account', 'register' or 'forgot "
                  "password' link", "login_link")

    async def _click_and_wait(self, page, button: str, step: str) -> None:
        """Click a sign-in button and wait for what it opens: a dialog on the same
        page (step 1, team 2026-11-02) or a new page, which may redirect a few
        times. Waits up to the page-load timeout; if nothing new shows by then,
        the caller looks at the page as it is."""
        before = page.url
        clicked = page.locator(button).first
        try:
            await self._click(page, button)
        except Exception:
            await self._dump_html(page, "aviall-login-click", "body")
            raise
        known = [KNOWN_USERNAME, KNOWN_PASSWORD, KNOWN_MFA, KNOWN_SIGNED_IN]
        waiting_for = [*web.PASSWORD_SELECTORS, *web.USERNAME_SELECTORS,
                       *web.MFA_INPUT_SELECTORS, *web.LOGOUT_SELECTORS, *SIGNED_IN_SELECTORS]
        if step != "OneLogin ID button":
            # Step 1 opens a dialog holding step 2's button.
            known = [KNOWN_ONELOGIN_BUTTON, *known]
            waiting_for = [*ONELOGIN_SELECTORS, *DIALOG_SELECTORS, *waiting_for]
        started = time.time()
        deadline = started + self.page_load_timeout_ms / 1000
        last_url, url_since, moved = before, started, False
        while time.time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            if page.url != last_url:
                last_url, url_since, moved = page.url, time.time(), True
            wide = time.time() - started > KNOWN_FIRST_SECONDS
            seen = await _first_visible(page, [*known, *waiting_for] if wide else known)
            if seen:
                logger.info("%s: %s shows after %.1fs (%s)", step, seen, time.time() - started,
                            page.url)
                break
            if time.time() - url_since < URL_STABLE_SECONDS:
                continue
            # Arrived: a new address, or back at the same one after redirects
            # (Boeing's sign-in, 2026-11-02), or the dialog / button went away.
            if moved or not await _visible(clicked):
                logger.info("%s: page changed after %.1fs (%s)", step, time.time() - started,
                            page.url)
                break
        else:
            logger.warning("%s: nothing new after %ds at %s; reading the page as it is",
                           step, self.page_load_timeout_ms // 1000, page.url)
        # No wait for the whole page: the next step waits for its own element.
        await _settle(page, 1500)

    # -- Log in link in the avatar menu ---------------------------------------
    async def _visible_login_link(self, page) -> Optional[str]:
        marker, saved = (self.config.get("logged_out_selector"),
                         await self._saved_fit(page, "login_link"))
        return self._note("login_link", await _first_visible(
            page, [marker, saved, *web.LOGIN_LINK_SELECTORS]), saved=saved, record=marker)

    async def _login_link(self, page) -> Optional[str]:
        """The Log in link in view, else the one in the avatar menu (the menu is
        left open so the caller can click it)."""
        link = await self._visible_login_link(page)
        if link:
            return link
        menu = await self._open_avatar_menu(page)
        if not menu:
            return None
        link = await self._visible_login_link(page)
        if not link:
            await self._close_menu(page)
            return None
        logger.info("Login: Log in link %s is in the avatar menu %s", link, menu)
        self._learn("account_menu", menu)
        return link

    async def _open_avatar_menu(self, page) -> Optional[str]:
        """Click the avatar (vendor config `account_menu`, saved, generic list);
        the menu it opened, or None."""
        saved = await self._saved_fit(page, "account_menu")
        menu = await web.first_visible_button(page, [self.config.get("account_menu"), saved,
                                                     *web.ACCOUNT_MENU_SELECTORS])
        if not menu or not await self._menu_words_ok(page, menu):
            return None
        try:
            self._act("click", menu, page)
            await page.click(menu, timeout=5000)
        except Exception as exc:
            logger.info("Avatar menu %s: could not click it (%s)", menu, str(exc).splitlines()[0])
            return None
        await _settle(page, 3000)
        return menu

    async def _signed_out_here(self, page) -> bool:
        """As the generic one, without opening the avatar menu on every results
        page: signed out with a hidden Log in link, the results show no prices,
        and NOT_FOUND only comes after the sign-in check (pipeline)."""
        if await self._password_field(page):
            return True
        # Signed in, a result shows its price and Add to Cart; signed out the
        # site asks to log in to see them (team, 2026-11-02). Looked at before
        # anything else: no menu to open, one query.
        if await _first_visible(page, SIGNED_IN_RESULT_SELECTORS):
            return False
        if await _first_visible(page, SIGNED_OUT_RESULT_SELECTORS):
            return True
        if await self._shopping_as(page):
            return False
        return bool(await self._visible_login_link(page)
                    or await _first_visible(page, DISTRIBUTION_LOGIN_SELECTORS))

    # -- sign-in step 5: 2FA box -> open the login URL again ----------------------
    async def _mfa(self, page, cred: VendorCredential, mfa_sel: Optional[str], text: str,
                   totp_tried: bool, code_given: bool = False) -> None:
        # A code a person typed, or the 2FA box again after the reopen: the
        # usual way (TOTP from the secret, else ask the person).
        if self.relogin_tried or code_given:
            await super()._mfa(page, cred, mfa_sel, text, totp_tried, code_given)
            return
        self.relogin_tried = True
        url = self.config.get("relogin_url") or DISTRIBUTION_URL
        if not host_allowed(url, self.hosts):
            url = self.login_start_url or self.home_url
        logger.info("Aviall step 1 (sign in): 2FA box; opening %s again instead (team, "
                    "2026-11-02)", url)
        started = time.time()
        self._act("open", url, page)
        await page.goto(url, wait_until="domcontentloaded")
        seen = await self._wait_known(page, LOGIN_PAGE_KNOWN,
                                      [*ONELOGIN_SELECTORS, *DISTRIBUTION_LOGIN_SELECTORS,
                                       *SIGNED_IN_SELECTORS], 30)
        logger.info("Aviall step 1 (sign in): %s shows %s after %.1fs", page.url,
                    seen or "nothing known", time.time() - started)
        await self._cookies(page)
        await self._after_relogin(page, cred, totp_tried)

    async def _after_relogin(self, page, cred: VendorCredential, totp_tried: bool) -> None:
        """Signed in now, or a person is needed. Never a wrong-password verdict and
        nothing typed: a sign-in form here is a check for the person."""
        text = await self._stop_if_blocked(page)          # lockout: stop; CAPTCHA: retried
        if not host_allowed(page.url, self.hosts):
            # Off Boeing's and Aviall's sites: not ours to answer.
            logger.warning("Login: the login URL went to %s -> needs a person", page.url)
            raise MfaRequired("relogin", f"After the 2FA box the login URL went to {page.url}")
        # The buttons sign us in now that Boeing's sign-in is valid (clicks
        # only; a form means a person is needed). Signed in already, they
        # aren't there and _click_to_form looks for Log out instead.
        state = await self._click_to_form(page)
        logger.info("Login: after reopening the login URL: %s at %s", state, page.url)
        text = await self._stop_if_blocked(page)
        if not host_allowed(page.url, self.hosts):
            raise MfaRequired("relogin", f"After the 2FA box the sign-in went to {page.url}")
        mfa_sel = await self._mfa_input(page)
        if mfa_sel or any(h in text for h in web.MFA_TEXT_HINTS):
            logger.warning("Login: 2FA asked again after reopening the login URL")
            await super()._mfa(page, cred, mfa_sel, text, totp_tried)
            return
        if state == "form" or await self._password_field(page):
            raise MfaRequired("relogin", f"Reopening the login URL shows a sign-in form again "
                              f"at {page.url}; needs a person")
        if state == "signed_in" or await self._signed_in_here(page):
            return
        verdict = await self._ask(lambda: self.extractor.classify_login_page(text, page.url))
        logger.info("Login: after reopening the login URL the LLM says %s at %s",
                    verdict, page.url)
        if verdict == "signed_in":
            self._signed_in(page, None)
            return
        if verdict == "blocked":
            raise LoginBlocked("Page looks like a block / CAPTCHA after reopening the login URL "
                               "(read by the LLM)")
        raise MfaRequired("relogin", f"Couldn't tell whether reopening the login URL signed in "
                          f"at {page.url}; needs a person")

    async def _signed_in_here(self, page) -> bool:
        """The "Shopping as" bar or a Log out link in view (no click needed);
        neither, the avatar's dropdown (opening it took ~10s a login)."""
        marker = (await _first_visible(page, [KNOWN_SIGNED_IN])
                  or await self._logout_link(page))
        if not marker:
            state, marker = await self._avatar(page)
            if state != "signed_in":
                marker = None
        if not marker:
            return False
        logger.info("Login: signed in (%s at %s)", marker, page.url)
        self._proven(page, marker)
        return True


    # -- bulk search ------------------------------------------------------------
    async def search_many(self, page, rows: list[RequestRow]):
        self.apply_timeouts(page)
        url = self.config.get("bulk_search_url") or BULK_URL
        logger.info("Bulk search: %d parts in one search (%s)", len(rows), url)
        self._act("open", url, page)
        response = await page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_any(page, [*SIGNED_IN_SELECTORS, *BULK_OPEN_SELECTORS,
                                        *DISTRIBUTION_LOGIN_SELECTORS, *web.PASSWORD_SELECTORS])
        await self._cookies(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out when opening the bulk search page")
        status = response.status if response is not None else 0
        if status >= 400 or not host_allowed(page.url, self.hosts):
            raise BulkSearchUnavailable(f"Bulk page gave HTTP {status} at {page.url}")

        if len(rows) > BULK_MAX_LINES:
            raise BulkResultsIncomplete(f"{len(rows)} parts; the box takes {BULK_MAX_LINES}")
        dialog, box = await self._bulk_dialog(page)
        lines = [_bulk_line(r) for r in rows]
        await self._type_lines(page, box, lines)
        logger.info("Bulk search: %d line(s) in %s", len(lines), box)
        await self._untick_recent_parts(page, dialog)
        submit = await self._bulk_submit(page, dialog)
        if not submit:
            raise BulkSearchUnavailable(f"No Search button in the multi-line dialog at {page.url}")
        logger.info("Aviall step 2 (search): clicking Search %s", submit)
        # The dialog's own layer blocked every normal click (10s each), and the
        # results come on the same page: a wait for a new page cost 45s more a
        # search (runs 2026-11-02 18:01 / 18:09). The button's own click, then
        # a wait for the results themselves.
        self._act("click from code", submit, page)
        started = time.time()
        await page.locator(submit).first.evaluate("el => el.click()")
        seen = await self._wait_for_any(page, [*RESULT_MARKERS, BULK_NOT_FOUND,
                                               *SIGNED_OUT_RESULT_SELECTORS])
        logger.info("Bulk search: %s after %.1fs", f"results ({seen})" if seen else "no results shown",
                    time.time() - started)
        await self._close_bulk_dialog(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out after the bulk search")
        if not host_allowed(page.url, self.hosts):
            raise BulkSearchUnavailable(f"Bulk search went to {page.url}")

        text = await self._results_text(page, "aviall-bulk")
        if any(h in text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(text)
        if len(text) > self.max_page_chars:
            # The pipeline tries half the batch next.
            raise BulkResultsIncomplete(f"Bulk results are {len(text)} chars "
                                        f"(limit {self.max_page_chars})")
        parts = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                  "condition_wanted": r.condition_wanted} for r in rows]
        kind, part_offers, usage = await self.extractor.extract_offers_bulk(
            page_text=text, page_url=page.url, parts=parts, system=self.extract_system)
        if kind == "login_page":
            raise SessionExpired("Extractor saw a login page")
        if kind == "error":
            raise BulkResultsIncomplete("Could not read the bulk results (LLM answer "
                                        "missing or cut off)")
        usage.add(self.usage)
        self.usage = type(self.usage)()
        outcomes = {}
        for r in rows:
            offers = self._fill_condition(part_offers.get(r.part_number) or [])
            await self._lead_times(page, offers)
            if offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=page.url,
                                                   alternates=self.listed_alternates(r))
                self._search_ok = self._search_ok or has_real_offer(offers)
        logger.info("Bulk search: offers for %d of %d parts (results at %s)",
                    len(outcomes), len(rows), page.url)
        return outcomes, usage

    async def _bulk_dialog(self, page) -> tuple[str, str]:
        """Open "Multi-line Ordering"; (its pop-over, its Copy & Paste box)."""
        await self._hide_tour(page)
        box = await _first_visible(page, [self.config.get("bulk_input"), *BULK_BOX_SELECTORS])
        if not box:
            opener = (await self._wait_for_any(page, [self.config.get("bulk_open_button"),
                                                      *BULK_OPEN_SELECTORS], 30)
                      or await self._llm_step(page, "the 'Multi-line Ordering' button that opens "
                                                    "the box for searching many part numbers"))
            if not opener:
                raise BulkSearchUnavailable(f"No 'Multi-line Ordering' button at {page.url}")
            logger.info("Aviall step 2 (search): opening Multi-line Ordering with %s", opener)
            self._act("click", opener, page)
            await page.click(opener, timeout=self.action_timeout_ms)
            box = await self._wait_for_any(page, [self.config.get("bulk_input"), *BULK_BOX_SELECTORS,
                                                  *(f"{d} textarea" for d in BULK_DIALOG_SELECTORS)], 20)
        if not box:
            tab = await _first_visible(page, COPY_PASTE_TAB)
            if tab:
                logger.info("Bulk search: opening the Copy & Paste tab with %s", tab)
                self._act("click", tab, page)
                await page.click(tab, timeout=self.action_timeout_ms)
                box = await self._wait_for_any(page, BULK_BOX_SELECTORS, 10)
        if not box:
            box = await self._llm_step(page, "the Copy & Paste text box of the 'Multi-line Ordering' "
                                             "window, where part numbers are pasted one per line "
                                             "(not the Line-by-line 'part #' boxes, not the page's "
                                             "own search box)", kind="type")
        if not box:
            await self._dump_html(page, "aviall-bulk-no-box", "body")
            raise BulkSearchUnavailable(f"No Copy & Paste box after 'Multi-line Ordering' at {page.url}")
        if not await page.locator(box).first.evaluate(MARK_BULK_JS):
            await self._dump_html(page, "aviall-bulk-no-search", "body")
            raise BulkSearchUnavailable(f"No Search button around the Copy & Paste box at {page.url}")
        logger.info("Bulk search: Copy & Paste box %s", box)
        return "[data-sourcing-bulk]", box

    async def _close_bulk_dialog(self, page) -> None:
        """The Multi-line Ordering dialog stays open over the results after
        Search (saved page 2026-11-02, 14:08: its empty box first, the 6 results
        below): closed with its X, so the cards' pop-up buttons can be clicked."""
        try:
            close = await _first_visible(page, BULK_CLOSE_SELECTORS)
            if not close:
                return
            self._act("click", close, page)
            await page.click(close, timeout=5000)
            await page.wait_for_timeout(500)
        except Exception as exc:
            logger.info("Bulk search: dialog not closed (%r); reading the page as it is", exc)

    async def _bulk_submit(self, page, dialog: str) -> Optional[str]:
        """The dialog's Search button; never one that orders or adds to the cart."""
        configured = self.config.get("bulk_submit_button")
        if configured and await _first_visible(page, [configured]):
            return configured
        hits = await page.locator(dialog).first.evaluate(BULK_SUBMIT_JS)
        for hit in reversed(hits or []):
            if _NOT_A_BULK_SEARCH.search(hit["words"]):
                logger.info("Bulk search: button %r not clicked", hit["words"][:80])
                continue
            return f"{dialog} >> {BULK_BUTTONS} >> visible=true >> nth={hit['index']}"
        await self._dump_html(page, "aviall-bulk-dialog", dialog)
        return await self._llm_step(page, "the Search button of the 'Multi-line Ordering' window "
                                          "that searches the part numbers pasted in its box (not "
                                          "Clear All, not the page's own search button)")

    async def _llm_step(self, page, goal: str, *, kind: str = "click",
                        allow: Optional[re.Pattern] = None,
                        hosts: Optional[tuple] = None) -> Optional[str]:
        """A step's element the rules didn't find: the LLM picks it from the
        visible elements (open pop-ups first), told the job (FLOW_CONTEXT) and
        the step's goal. Checked before use: the right kind of element, on the
        vendor's sites, and never a cart / order / delete / account change
        (`allow` lets a step's own words through, e.g. "log out")."""
        if self.extractor is None:
            return None
        try:
            elements = await page.evaluate(STEP_ELEMENTS_JS)
        except Exception as exc:
            logger.warning("Aviall: page elements not listed for the LLM: %r", exc)
            return None
        if not elements:
            return None
        what = (f"{goal}. The job: {FLOW_CONTEXT} Elements with in_popup=true are in a "
                f"pop-up or dialog that is open now")
        index = await self._ask(lambda: self.extractor.pick_element(elements, what))
        if index is None:
            logger.info("Aviall: the LLM found no element for: %s", goal)
            return None
        el = elements[index]
        words = " ".join(str(el.get(k) or "") for k in ("text", "aria_label", "id", "name"))
        tag, typ = el.get("tag"), (el.get("type") or "").lower()
        typable = tag == "textarea" or (tag == "input" and typ in ("", "text", "search"))
        ok = typable if kind == "type" else (self._clickable(el) and not typable)
        href = el.get("href") or ""
        if (not ok or (kind == "click" and _NEVER_CLICK.search(words)
                       and not (allow and allow.search(words)))
                or (href.startswith("http") and not host_allowed(href, hosts or self.hosts))):
            logger.warning("Aviall: the LLM picked %s %r for %r; not used", tag, words.strip()[:80],
                           goal)
            return None
        selector, _ = self._selector_for(el)
        logger.info("Aviall: the LLM picked %s %r for: %s", selector, words.strip()[:80], goal)
        return selector

    async def _click(self, page, selector: str) -> None:
        """A click; when something lies over the element (the pop-over's own
        layers), the element's own click instead."""
        try:
            self._act("click", selector, page)
            await page.click(selector, no_wait_after=True, timeout=10_000)
        except PlaywrightTimeoutError:
            logger.info("Click on %s blocked; clicking it from code", selector)
            self._act("click from code", "(covered element)", page)
            await page.locator(selector).first.evaluate("el => el.click()")

    async def _dump_html(self, page, label: str, selector: str) -> None:
        """With DEBUG_DUMP_PAGES=1, save what the program couldn't use, to fix
        it: the element's HTML and a screenshot of the page as it was."""
        if os.environ.get("DEBUG_DUMP_PAGES", "").strip() not in ("1", "true", "yes"):
            return
        web.DUMP_DIR.mkdir(exist_ok=True)
        stem = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
        try:
            html = await page.locator(selector).first.evaluate("el => el.outerHTML")
            stem.with_suffix(".html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(stem.with_suffix(".png")), timeout=15_000)
            logger.info("Saved %s at %s to %s.(html|png)", selector, page.url, stem)
        except Exception as exc:
            logger.info("Not all saved (%s): %r", label, exc)

    async def _hide_tour(self, page) -> None:
        """The "Tour Our New Features!" pop-up: hidden for this page, and closed
        with No thanks when it shows (so the site stops showing it)."""
        try:
            await page.add_style_tag(content=HIDE_TOUR_CSS)
        except Exception as exc:
            logger.debug("Tour not hidden: %r", exc)
        await self._close_tour(page)

    async def _close_tour(self, page) -> None:
        close = await _first_visible(page, TOUR_CLOSE_SELECTORS)
        if not close:
            return
        try:
            self._act("click", close, page)
            await page.click(close, timeout=5000)
            logger.info("Closed the site's feature tour (%s)", close)
        except Exception as exc:
            logger.debug("Tour not closed: %r", exc)

    async def _type_lines(self, page, box: str, lines: list[str]) -> None:
        """Put the lines in the box as a person would: a paste, else typed text.
        A value set from code (fill) can look right yet never reach the site's
        own form, which then searches nothing (team, 2026-11-02)."""
        text = "\n".join(lines)
        target = page.locator(box).first
        # Focused from code, not clicked: the line-number editor lays its own
        # element over the box and a click on it timed out (live run 2026-11-02).
        await target.evaluate(FOCUS_JS)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        self._act("paste", f"{len(lines)} line(s) into {box}", page)
        await target.evaluate(PASTE_JS, text)
        how = "pasted"
        if not await self._lines_in(target, lines):
            self._act("fill", f"{len(lines)} line(s) into {box}", page)
            await target.fill(text, force=True)
            how = "filled"
        if not await self._lines_in(target, lines):
            await target.evaluate(FOCUS_JS)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            self._act("type", f"{len(lines)} line(s) into {box}", page)
            await page.keyboard.insert_text(text)
            how = "typed"
        await page.keyboard.press("End")              # a key event too, for forms that wait for one
        if not await self._lines_in(target, lines):
            raise BulkSearchUnavailable(f"The part numbers didn't go into {box}")
        logger.info("Bulk search: part numbers %s into the box", how)

    @staticmethod
    async def _lines_in(target, lines: list[str]) -> bool:
        shown = await target.evaluate("el => el.value ?? el.innerText ?? ''") or ""
        return all(line.split(",")[0] in shown for line in (lines[0], lines[-1]))

    async def _untick_recent_parts(self, page, dialog: str) -> None:
        for selector in RECENT_PARTS_CHECKBOX:
            box = page.locator(f"{dialog} >> {selector}").first
            try:
                if await box.count() and await box.is_checked():
                    await box.uncheck(timeout=3000)
                    logger.info("Bulk search: unticked 'Include recently searched part numbers'")
                return
            except Exception as exc:
                logger.debug("Recent parts checkbox %s: %r", selector, exc)

    # -- reading a results page ---------------------------------------------------
    async def _results_text(self, page, label: str) -> str:
        """The page text, with what each card shows only on hover or in its Price
        Breaks pop-up written into the card first."""
        await self._wait_for_results(page)
        await self._reveal(page)
        text = _without_noise(await web.page_text(page, label))
        if not self.show_substitutes:
            await self._note_replacements(page)
            return text
        return text + await self._replacement_pages(page, text)

    async def _note_replacements(self, page) -> None:
        """Substitutes off (team, 2026-11-03/04): the replacement each Superseded
        card names is only remembered, for the requested part's Internal Comments;
        its page isn't opened and it gets no entry."""
        self._replacements = {}
        try:
            links = await page.evaluate(REPLACEMENT_PNS_JS)
        except Exception as exc:
            logger.debug("Replacement numbers not read: %r", exc)
            return
        for link in links:
            card = link["card"].strip().upper()
            href_pn = unquote(link["href"].rstrip("/").rsplit("/", 1)[-1]).split("_", 1)[-1]
            pn = (link["by"] or href_pn).strip().upper()
            if card and pn and pn != card:
                found = self._replacements.setdefault(card, [])
                if pn not in found:
                    found.append(pn)

    def listed_alternates(self, row: RequestRow) -> list[str]:
        """The replacements of the Superseded card(s) of the requested part."""
        if self.show_substitutes:
            return []
        asked = row.part_number.strip().upper()
        found: list[str] = []
        for card, pns in self._replacements.items():
            if card == asked or card.split("=")[0] == asked:
                found += [pn for pn in pns if pn not in found and pn != asked]
        return found

    async def _replacement_pages(self, page, text: str) -> str:
        """The product page of each replacement a Superseded card names that has no
        card of its own here, as its own block after the results."""
        try:
            links = await page.evaluate(REPLACEMENTS_JS)
        except Exception as exc:
            logger.debug("Replacement links not read: %r", exc)
            return ""
        shown = {pn.strip().upper() for pn in _FULL_PN.findall(text)}
        todo, seen = [], set()
        for link in links:
            href = link["href"]
            pn = unquote(href.rstrip("/").rsplit("/", 1)[-1]).split("_", 1)[-1].upper()
            if pn in shown or href in seen or not host_allowed(href, self.hosts):
                continue
            seen.add(href)
            todo.append((link, pn))
        out = []
        todo = todo[:REPLACEMENTS_MAX]
        for n, (link, pn) in enumerate(todo, 1):
            body = await self._product_page_text(page, link["href"])
            if body:
                logger.info("Aviall: replacement %s of Superseded %s read from %s", pn,
                            link["card"] or "?", link["href"])
                out.append(REPLACEMENT_HEADER.format(n=n, m=len(todo), url=link["href"],
                                                     card=link["card"] or "(no number)")
                           + "\n" + body)
        return ("\n\n" + "\n\n".join(out)) if out else ""

    async def _product_page_text(self, page, url: str) -> Optional[str]:
        """A product page's text, read in a second tab (the results stay as they are)."""
        tab = await page.context.new_page()
        try:
            self.apply_timeouts(tab)
            await tab.goto(url, wait_until="domcontentloaded")
            if not host_allowed(tab.url, self.hosts):
                logger.warning("Aviall: product page %s went to %s; not read", url, tab.url)
                return None
            try:
                await tab.wait_for_function(
                    "() => /Lead Time \\(Days\\)|\\$[0-9]|Add to Cart|not approved/i"
                    ".test(document.body.innerText)",
                    timeout=PRODUCT_PAGE_WAIT_SECONDS * 1000)
            except Exception:
                logger.info("Aviall: product page %s showed no price or lead time", url)
            return _without_noise(await tab.inner_text("body"))
        except Exception as exc:
            logger.warning("Aviall: product page %s not read: %r", url, exc)
            return None
        finally:
            try:
                await tab.close()
            except Exception:
                pass

    async def _read_results(self, page, row: RequestRow, results_url: str) -> SearchOutcome:
        outcome = await super()._read_results(page, row, results_url)
        self._fill_condition(outcome.offers or [])
        await self._lead_times(page, outcome.offers or [])
        return outcome

    async def _lead_times(self, page, offers: list) -> None:
        """Lead Time (Days) from the product page, for the lines that need it."""
        for offer in offers:
            self._date_to_days(offer)
        if self.config.get("product_pages", "when_needed") == "off":
            return
        wanted = [o for o in offers if o.price and not (o.lead_time or "").strip()
                  and _out_of_stock(o.qty_available)]
        for offer in wanted[:int(self.config.get("product_pages_max", PRODUCT_PAGES_MAX))]:
            url = self.product_links.get((offer.part_number or "").strip().upper())
            if not url:
                logger.info("Aviall: no product page known for %s; lead time left empty",
                            offer.part_number)
                continue
            days = await self._product_page_lead_time(page, url)
            if days is not None:
                offer.lead_time = f"{days} days"
                logger.info("Aviall: %s lead time %s days (product page %s)",
                            offer.part_number, days, url)

    @staticmethod
    def _date_to_days(offer, today: Optional[date] = None) -> None:
        """An availability date as a lead time in days from today ("Available
        11/26/2026" on 2026-11-02 -> "24 days"); a date already past is no lead
        time (the product page's is read instead)."""
        match = AVAILABLE_DATE.search(offer.lead_time or "")
        if not match:
            return
        month, day, year = (int(g) for g in match.groups())
        try:
            when = date(year, month, day)
        except ValueError:
            return
        days = (when - (today or date.today())).days
        logger.info("Aviall: %s %r -> %s", offer.part_number, offer.lead_time,
                    f"{days} days" if days >= 0 else "date passed, lead time from the product page")
        offer.lead_time = f"{days} days" if days >= 0 else None

    async def _product_page_lead_time(self, page, url: str) -> Optional[int]:
        """Read in a second tab, closed again: the results page stays as it is."""
        tab = await page.context.new_page()
        try:
            self.apply_timeouts(tab)
            await tab.goto(url, wait_until="domcontentloaded")
            if not host_allowed(tab.url, self.hosts):
                logger.warning("Aviall: product page %s went to %s; not read", url, tab.url)
                return None
            try:
                await tab.wait_for_function(
                    "() => /Lead Time \\(Days\\)/i.test(document.body.innerText)",
                    timeout=PRODUCT_PAGE_WAIT_SECONDS * 1000)
            except Exception:
                logger.info("Aviall: no 'Lead Time (Days)' on %s", url)
                return None
            match = LEAD_TIME_DAYS.search(await tab.inner_text("body"))
            return int(match.group(1)) if match else None
        except Exception as exc:
            logger.warning("Aviall: product page %s not read: %r", url, exc)
            return None
        finally:
            try:
                await tab.close()
            except Exception:
                pass

    def _fill_condition(self, offers: list) -> list:
        code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN)
        for offer in offers:
            if code and (offer.condition or "").strip().upper() not in CONDITION_CODES:
                offer.condition = code
        return offers

    async def _wait_for_results(self, page) -> None:
        """Cards are drawn by script after the page loads."""
        try:
            await page.wait_for_function(
                "() => /Request Support|\\d+ results?|unable to find|could not find|no results/i"
                ".test(document.body.innerText)", timeout=RESULTS_WAIT_MS)
        except Exception:
            logger.warning("Aviall: no result cards or result count at %s", page.url)

    async def _reveal(self, page) -> None:
        """Never fatal: what couldn't be read is logged and the page is read as is."""
        hover_max = int(self.config.get("hover_max", HOVER_MAX))
        per_card = int(self.config.get("hover_per_card", HOVER_PER_CARD))
        await self._hide_tour(page)
        deadline = time.monotonic() + float(self.config.get("reveal_seconds", REVEAL_SECONDS))
        try:
            supplier = self.config.get("supplier", SUPPLIER)
            marked = await asyncio.wait_for(page.evaluate(MARK_JS, [hover_max, per_card, supplier]),
                                            web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Aviall: result cards not marked (%r); reading the page as is", exc)
            return
        logger.info("Aviall step 3 (read): %d card(s), %d full part number(s), %d icon label(s), "
                    "%d pop-up button(s), %d icon(s) to hover", marked["cards"], marked["fulls"],
                    marked["labels"], marked["breaks"], marked["hovers"])
        if marked.get("others"):
            logger.info("Aviall: %d card(s) of other suppliers left out (%s); only %r read",
                        len(marked["others"]), ", ".join(sorted(set(marked["others"]))), supplier)
        for link in marked.get("links") or []:
            for key in (link.get("full"), link.get("shown")):
                if key and link.get("href") and host_allowed(link["href"], self.hosts):
                    self.product_links.setdefault(key.strip().upper(), link["href"])
        read = {"hover": 0, "pb": 0}
        if marked["breaks"]:
            seconds = float(self.config.get("reveal_seconds", REVEAL_SECONDS))
            self._act("open and read", f"{marked['breaks']} pop-up(s) in the page", page)
            try:
                got = await asyncio.wait_for(
                    page.evaluate(READ_POPUPS_JS, [POPUPS, int(seconds * 1000)]), seconds + 30)
                read["pb"] = got["read"]
                if got["tried"] < got["total"]:
                    logger.warning("Aviall: %d of %d pop-up(s) opened in %ss", got["tried"],
                                   got["total"], seconds)
                if got.get("left"):
                    logger.warning("Aviall: a pop-up click went to %s; going back", got["left"])
                    self._act("back", "", page)
                    await page.go_back(wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning("Aviall: pop-ups not read (%r)", exc)
        for kind, selector in (("hover", "[data-sourcing-hover]"),):
            targets = page.locator(selector)
            for i in range(await targets.count()):
                if time.monotonic() > deadline:
                    logger.warning("Aviall: stopped reading hover text / pop-ups after %ss",
                                   self.config.get("reveal_seconds", REVEAL_SECONDS))
                    break
                if await self._reveal_one(page, targets.nth(i), kind):
                    read[kind] += 1
        logger.info("Aviall: read %d pop-up(s) (Price Breaks, Stock By Location, ...) and %d "
                    "hover text(s)", read["pb"], read["hover"])

    async def _reveal_one(self, page, target, kind: str) -> bool:
        try:
            before = await page.evaluate(SNAPSHOT_JS, POPUPS)
            url = page.url
            if kind == "pb":
                self._act("click", "pop-up button", page)
                await target.click(timeout=3000, no_wait_after=True)
                await page.wait_for_timeout(POPUP_WAIT_MS)
            else:
                await target.hover(timeout=2000)
                await page.wait_for_timeout(HOVER_WAIT_MS)
            if page.url.split("#")[0] != url.split("#")[0]:
                # A click must leave the page where it is: back, and nothing read.
                logger.warning("Aviall: a pop-up click went to %s; going back", page.url)
                if not host_allowed(page.url, self.hosts):
                    self._act("open", url, page)
                    await page.goto(url, wait_until="domcontentloaded")
                else:
                    self._act("back", "", page)
                    await page.go_back(wait_until="domcontentloaded")
                return False
            text = " ".join((await page.evaluate(OPENED_JS, [POPUPS, before]) or "").split())
            await self._close_popup(page, kind, target)
            if not text:
                return False
            name = (await target.get_attribute("data-sourcing-pb")) or "Price Breaks"
            note = (f"({name} pop-up: {text})" if kind == "pb"
                    else f"(shown on hover: {text})")
            await target.evaluate(NOTE_JS, note)
            return True
        except SessionExpired:
            raise
        except Exception as exc:
            logger.debug("Aviall: %s not read: %r", kind, exc)
            return False

    async def _close_popup(self, page, kind: str, target=None) -> None:
        if kind == "hover":
            await page.mouse.move(0, 0)
            return
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        # An Angular pop-over closes with a second click on its own button.
        if target is not None and await page.locator("ngb-popover-window").count():
            try:
                self._act("click", "pop-up button again (closes it)", page)
                await target.click(timeout=2000, no_wait_after=True)
                await page.wait_for_timeout(200)
            except Exception:
                pass
        close = page.locator(f"{POPUPS} >> [aria-label*='close' i], {POPUPS} >> button:has-text('×')")
        try:
            if await close.count() and await close.first.is_visible():
                self._act("click", "pop-up close", page)
                await close.first.click(timeout=2000)
        except Exception:
            pass


# Page text that is never a result: the mini-cart (someone's cart item with
# its price and unit, 2026-11-02), the recommendations under the results, and
# the site's feature tour.
_NOISE = [re.compile(p, re.S) for p in (
    r"Products in Cart \(\d+\).*?Continue Shopping",
    r"You may also be interested in.*?(?=DISCLAIMER:|Shop Boeing|$)",
    r"Tour Our New Features!.*?Show me!",
)]


def _out_of_stock(qty: Optional[str]) -> bool:
    text = (qty or "").strip().lower()
    return "out of stock" in text or text in ("0", "0.0")


def _without_noise(text: str) -> str:
    for pattern in _NOISE:
        text = pattern.sub(" ", text)
    return text


def _bulk_line(row: RequestRow) -> str:
    """'<part>, <qty>' as the Copy & Paste box asks; the part alone without a
    whole-number quantity."""
    qty = (row.qty_needed or "").strip()
    return f"{row.part_number}, {qty}" if qty.isdigit() else row.part_number
