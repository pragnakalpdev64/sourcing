"""Proponent (Procart) adapter.

Login selectors come from the MCP prototype's learned cache
(src/learned_sites.json -> "proponent"). A single-part search opens the
results address directly (`PartSearchResults.aspx?id=<part>`). If that address
doesn't work, the site's search form is used instead: the search box is found
by generic candidates, then by asking Claude once. The selector, and the
results address learned from where the form lands, are saved to Firestore
`selectors/proponent` and reused on later runs.

Page facts (checked against the public pages, 2026-10-09):
- The home page is public. Logged out, its header shows a `Login` button
  (`a#loginbutton`); that button is the "not signed in" marker.
- Login form: `#MainContent_txtUserName`, `#MainContent_txtPassword`,
  `#MainContent_btnLogin` (ASP.NET postback).
- Cookie bar `#catapult-cookie-bar`: its Accept button `#catapultCookie` sets a
  cookie and then calls `location.reload()`, so it must be accepted (and the
  reload waited for) BEFORE filling any form, or the reload wipes the inputs.
  The cookie is kept in the Browserbase Context, so later runs don't see it.

- Bulk search (home page): textarea `#MainContent_txtBulkPart`, one "PART QTY"
  per line, at most 500 lines (the page's own CheckLines check), submitted with
  `#MainContent_btnBulkSearch`; errors show in `#MainContent_lblBulkSearchMsg`.
  The results page (`PartSearchResults.aspx`) is paged: "Viewing results 1 - 10
  of 24", a "Results per page: 10" dropdown, and first/prev/"Page [n] of N"/
  next/last controls (seen 2026-10-12). The adapter raises the page size to fit
  the batch and reads any remaining pages; the pager's exact markup is still
  unmapped, so it is found by its text.

Vendor settings that can be overridden in Firestore `vendors/proponent`
(or `.local_store.json` locally):
  login_url, probe_url, search_url, logged_in_selector, logged_out_selector,
  part_url_template (results address with {part}, in the query or the path;
  "" = always use the search form),
  bulk_batch_size (parts per bulk search; 1 = off), bulk_search_url,
  logout_after_run (default true), logout_url
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..credentials.base import VendorCredential
from ..models import RequestRow, SearchOutcome
from .grid import GRID_RECORDS_JS, ROW_ID_KEYS, dumps, parse_grid_result
from .base import (
    _TRACKING_PARAMS, BulkResultsIncomplete, BulkSearchUnavailable, InvalidCredentials,
    CaptchaFailed, LoginBlocked, LoginFailed, MfaRequired, SessionExpired, VendorAdapter,
    check_rate_limited, fill_part_url, host_allowed, part_url_template,
)

logger = logging.getLogger("sourcing.vendors.proponent")

LOGIN_URL = "https://procart.proponent.com/Login.aspx"
HOME_URL = "https://procart.proponent.com/"
PART_RESULTS_URL = "https://procart.proponent.com/PartSearchResults.aspx?id={part}"

LOGGED_OUT_SELECTOR = "a#loginbutton"
# Cookie / consent popups. Procart shows two: its own Catapult bar (Accept
# reloads the page) and a Termly "Cookie Consent Prompt" <dialog> injected by
# script after load, which blocks every click until answered.
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
    "Accept all", "Accept", "I agree", "Agree", "OK", "Got it",
]

BULK_TEXTAREA = "#MainContent_txtBulkPart"
BULK_SUBMIT = "#MainContent_btnBulkSearch"
BULK_MESSAGE = "#MainContent_lblBulkSearchMsg"
BULK_MAX_LINES = 500          # the page refuses more (CheckLines)
BULK_DEFAULT_BATCH = 25       # keeps one results page readable in a single LLM call
BULK_MAX_RESULT_PAGES = 50    # safety stop when stepping through paged results
RESULTS_GRID = "#jQGridDemo"  # Procart's results grid (vendor config results_grid overrides)
GRID_WAIT_SECONDS = 20        # longest wait for the grid's rows to load
# Longest wait for the results rows' details (Part Condition, product notes) to
# load: each row shows "Loading..." until its details arrive, a few seconds
# after the page (3 of 19 had loaded when read, 2026-10-23).
DETAILS_WAIT_SECONDS = 45
# Longest wait for a result row to show another warehouse's numbers after its
# warehouse dropdown is changed, and the most dropdown changes per page.
WAREHOUSE_SWITCH_SECONDS = 8
WAREHOUSE_SWITCHES_MAX = 60
# Procart's empty results grid (vendor config no_results_text overrides). Seen
# 2026-10-14 for an in-stock part that showed its results 70 minutes later.
NO_RESULTS_TEXT = "No data found"
# How Procart says it doesn't sell a part: every wording in the saved runs
# (0684-12-28, 50831-1, 71550KE-10T-05SN-LC, 2026-10-12 to 14).
NOT_SOLD_PHRASES = ("not recognized in Proponent system", "not supported by Proponent",
                    "restricted for sale")
GRID_IDLE_TIMEOUT_MS = 30_000 # longest wait for the results grid's "Loading..." overlay
PAGE_SCRIPT_TIMEOUT = 30      # seconds; page.evaluate has no timeout of its own

LOGOUT_SELECTORS = [
    "a:has-text('Logout')", "a:has-text('Log out')", "a:has-text('Log Out')",
    "a:has-text('Sign out')", "a:has-text('Sign Out')",
    "button:has-text('Logout')", "button:has-text('Log out')", "button:has-text('Sign out')",
    "a[href*='logout' i]", "a[href*='logoff' i]", "a[href*='signout' i]",
]

# Links that open the login page from a home page, when the start URL isn't the
# login page itself (most specific first).
LOGIN_LINK_SELECTORS = [
    "a#loginbutton",
    "a:has-text('Log in')", "a:has-text('Login')", "a:has-text('Sign in')",
    "button:has-text('Log in')", "button:has-text('Login')", "button:has-text('Sign in')",
    "a[href*='login' i]", "a[href*='signin' i]",
]

USERNAME_SELECTORS = [
    "#MainContent_txtUserName",
    "input[id*='username' i]", "input[name*='username' i]",
    "input[type='email']", "input[autocomplete='username']",
]
PASSWORD_SELECTORS = ["#MainContent_txtPassword", "input[type='password']"]
SUBMIT_SELECTORS = [
    "#MainContent_btnLogin",
    "input[type='submit']", "button[type='submit']",
    "button:has-text('Log in')", "button:has-text('Login')", "button:has-text('Sign in')",
]
MFA_INPUT_SELECTORS = [
    "input[autocomplete='one-time-code']", "input[name*='otp' i]", "input[id*='otp' i]",
    "input[name*='code' i]", "input[id*='code' i]", "input[inputmode='numeric']",
]
MFA_SUBMIT_SELECTORS = [
    "input[type='submit']", "button[type='submit']",
    "button:has-text('Verify')", "button:has-text('Submit')", "button:has-text('Continue')",
]
SEARCH_BUTTON = "#MainContent_btnSearch"   # SEARCH under the PART NUMBER boxes
SEARCH_INPUT_SELECTORS = [
    "#MainContent_rptControls_txtPart_0",    # first PART NUMBER box on the home page
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
# A server-side failure after the login submit: the credentials were not judged,
# so one more attempt is safe.
SERVER_ERROR_HINTS = [
    "internal server error", "service unavailable", "bad gateway", "gateway timeout",
    "server error", "temporarily unavailable", "an unexpected error",
]
MFA_TEXT_HINTS = [
    "verification code", "authenticator", "one-time", "two-factor", "2-step",
    "security code", "we sent a code", "enter the code",
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

# Every <select> with its options, for finding the "Results per page" dropdown.
_LIST_SELECTS_JS = """
() => Array.from(document.querySelectorAll('select')).map((s, i) => {
  s.setAttribute('data-sourcing-sel', String(i));
  const r = s.getBoundingClientRect();
  const st = getComputedStyle(s);
  return {index: i, id: s.id || null, name: s.getAttribute('name'),
          title: s.getAttribute('title'), className: s.className || '',
          visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
                   st.display !== 'none' && s.offsetParent !== null,
          enabled: !s.disabled,
          options: Array.from(s.options).map(o => ({value: o.value, text: o.text.trim(),
                                                     selected: o.selected}))};
})
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
    if (row.closest('[data-sourcing-drop]')) continue;
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

# Mark a visible, not yet used "Expand All (Details)" control with
# data-sourcing-expand="1". Skipped when it already reads "Collapse All".
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

# Mark every visible "Show 1 Approved Substitutes for 424B200-7" button with
# data-sourcing-subs="<i>"; returns how many. Procart's results page since
# 2026-10-23 lists a part's approved substitutes only after this click (before,
# the jqGrid record said "This is an approved substitute part for part #...").
_MARK_SUBSTITUTE_BUTTONS_JS = """
() => {
  document.querySelectorAll('[data-sourcing-subs]').forEach(el => el.removeAttribute('data-sourcing-subs'));
  const buttons = Array.from(document.querySelectorAll('button, a')).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 &&
           /^show \\d+ approved substitutes? for /i.test((el.innerText || '').trim());
  });
  buttons.forEach((el, i) => el.setAttribute('data-sourcing-subs', String(i)));
  return buttons.length;
}
"""

# Mark every visible row toggle still showing "+" (icon-plus-new.svg) with
# data-sourcing-open="<i>"; returns how many. The substitute rows that "Show N
# Approved Substitutes" adds come closed: their Part Condition and price breaks
# are only in the details the "+" opens (2026-10-28, 426HE13-8 written as No
# Quote for want of a condition). "Expand All Details" opens only the rows
# already on the page.
_MARK_COLLAPSED_ROWS_JS = """
() => {
  document.querySelectorAll('[data-sourcing-open]').forEach(el => el.removeAttribute('data-sourcing-open'));
  const buttons = Array.from(document.querySelectorAll('tr > td:first-child button')).filter(b => {
    const img = b.querySelector('img');
    const r = b.getBoundingClientRect();
    return img && /icon-plus/i.test(img.getAttribute('src') || '') && r.width > 0 && r.height > 0 &&
           !b.closest('[data-sourcing-drop]');
  });
  buttons.forEach((b, i) => b.setAttribute('data-sourcing-open', String(i)));
  return buttons.length;
}
"""

# Hide (on our copy of the page) each "You entered 426EN10-8, closest match
# based on characters is 426EN9-8." row the portal won't sell: no price and no
# Add To Cart. Its part column shows the number asked for, so its stock was
# read as a second 426EN10-8 line and its "9 on hand" went into the real line's
# comment (2026-10-28). Hidden with its detail rows, as React redraws removed
# nodes. A look-alike that can be bought (ACHC3-13 for ACHC-3-13) shows its own
# number and stays; the pipeline leaves it out. Returns "asked -> suggested".
_HIDE_SUGGESTIONS_JS = """
() => {
  const out = [];
  for (const row of document.querySelectorAll('tr')) {
    if (row.closest('[data-sourcing-drop]')) continue;
    const cells = Array.from(row.children).filter(c => c.tagName === 'TD');
    if (cells.length < 3) continue;
    const m = (row.innerText || '').match(
      /You entered\\s+(\\S+?),\\s*closest match based on characters is\\s+(\\S+?)\\.?(?:\\s|$)/i);
    if (!m) continue;
    const details = [];
    for (let n = row.nextElementSibling;
         n && n.children.length === 1 && n.children[0].hasAttribute('colspan') &&
         !/approved substitutes? for/i.test(n.innerText || '');
         n = n.nextElementSibling) details.push(n);
    const priced = cells.some(td => /\\$\\s*[\\d.]/.test(td.innerText || ''));
    const cart = details.some(d => Array.from(d.querySelectorAll('button, a, input')).some(
      b => /add to cart/i.test(b.innerText || b.value || '')));
    if (priced || cart) continue;
    for (const el of [row, ...details]) {
      el.setAttribute('data-sourcing-drop', '1');
      el.style.display = 'none';
    }
    out.push(m[1] + ' -> ' + m[2]);
  }
  return out;
}
"""

# Warehouse dropdowns with more than one warehouse, marked data-sourcing-whs="<i>"
# in page order. A result row shows the numbers of the selected warehouse only
# (4066-0014, 2026-10-27: Brea, CA 17 shown, Singapore's 5 only after picking
# it; the row's quantity, 22, is both together).
_MARK_WAREHOUSE_SELECTS_JS = """
() => {
  const out = [];
  let i = 0;
  for (const s of document.querySelectorAll('select')) {
    const opts = Array.from(s.options);
    if (opts.length < 2 || !opts.every(o => /^warehouse:/i.test(o.text.trim()))) continue;
    if (s.closest('[data-sourcing-drop]')) continue;
    s.setAttribute('data-sourcing-whs', String(i));
    out.push({index: i, done: s.hasAttribute('data-sourcing-whs-done'), selected: s.value,
              options: opts.map(o => ({value: o.value, text: o.text.trim()}))});
    i++;
  }
  return out;
}
"""

# Pick a warehouse the way the page's own dropdown does: set the hidden select
# and fire its change event (React tracks the value, hence the native setter).
_SWITCH_WAREHOUSE_JS = """
([i, value]) => {
  const s = document.querySelector(`[data-sourcing-whs="${i}"]`);
  if (!s) return false;
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(s, value);
  s.dispatchEvent(new Event('input', {bubbles: true}));
  s.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}
"""

# The warehouse dropdown of the UI seen 2026-11-04 (debug_pages/20261104-175506-bulk.html):
# the <select> is hidden (display:none, tabindex=-1) inside a custom control,
# <div class="bb-custom-select-container"><span class="bb-custom-select-opener"
# role="combobox">... and a panel of <div role="option" data-value="SIN">. Setting
# the hidden select did nothing (17:54 run: "didn't return to its first warehouse"
# for all 6 dropdowns, ~10s each, other warehouses left out); a person's two
# clicks do.
WAREHOUSE_CONTROL = ".bb-custom-select-container:has([data-sourcing-whs='{i}'])"
WAREHOUSE_OPENER = ".bb-custom-select-opener, [role='combobox']"
WAREHOUSE_OPTION = "[role='option'][data-value='{value}']"

# The part a marked warehouse dropdown belongs to: the result row above the
# dropdown's detail row (its customer-part cell).
_WAREHOUSE_PART_JS = """
(i) => {
  const s = document.querySelector(`[data-sourcing-whs="${i}"]`);
  const block = s && s.closest('td[colspan]');
  const tr = block && block.closest('tr');
  const prev = tr && tr.previousElementSibling;
  const cell = prev && (prev.querySelector('.customer-part span') || prev.querySelector('.customer-part'));
  return cell ? cell.innerText.trim() : '';
}
"""

# The marked dropdown's warehouse row: Date Next In, Qty Due In and Qty On Hand
# (found by their headings), how many cells it has, and the warehouse named by
# the result's "Details for Warehouse: ..." when its details are open.
_READ_WAREHOUSE_ROW_JS = """
(i) => {
  const s = document.querySelector(`[data-sourcing-whs="${i}"]`);
  const row = s && s.closest('tr');
  if (!row) return null;
  const cells = Array.from(row.children).filter(c => c.tagName === 'TD');
  const table = row.closest('table');
  const heads = table ? Array.from(table.querySelectorAll('thead th')).map(th => th.innerText.trim()) : [];
  const at = (name, dflt) => { const k = heads.findIndex(h => new RegExp(name, 'i').test(h));
                               return k >= 0 ? k : dflt; };
  const cols = [at('next in', 1), at('due in', 2), at('on hand', 3)];
  const block = row.closest('td[colspan]');
  const m = block && (block.innerText || '').match(/Details for Warehouse:\\s*([^\\n]+)/i);
  return {values: cols.map(k => cells[k] ? (cells[k].innerText || '').trim() : ''), cols,
          width: cells.length, details: m ? m[1].trim() : ''};
}
"""

# Add the other warehouses' numbers below the marked dropdown's row, one row
# each as the page lays them out ("Warehouse: Singapore", dates, due in, on
# hand), and mark the dropdown done so a second pass leaves it alone.
_ADD_WAREHOUSE_ROWS_JS = """
([i, rows, cols, width]) => {
  const s = document.querySelector(`[data-sourcing-whs="${i}"]`);
  const row = s && s.closest('tr');
  if (!row) return 0;
  s.setAttribute('data-sourcing-whs-done', '1');
  let after = row;
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.setAttribute('data-sourcing-wh-extra', '1');
    for (let k = 0; k < width; k++) {
      const td = document.createElement('td');
      const text = k === 0 ? r.name : (cols.indexOf(k) >= 0 ? r.values[cols.indexOf(k)] : '');
      text.split('\\n').forEach((line, n) => {
        if (n) td.appendChild(document.createElement('br'));
        td.appendChild(document.createTextNode(line));
      });
      tr.appendChild(td);
    }
    after.after(tr);
    after = tr;
  }
  return rows.length;
}
"""

# How many result rows still show "Loading..." where their details go; the
# first one is scrolled into view in case details load only when on screen.
_DETAILS_LOADING_JS = """
() => {
  const cells = Array.from(document.querySelectorAll('td')).filter(td =>
    Array.from(td.childNodes).some(n => n.nodeType === 3 && n.textContent.trim() === 'Loading...'));
  if (cells.length) cells[0].scrollIntoView({block: 'center'});
  return cells.length;
}
"""

# A row's warehouse dropdown ("Warehouse: Netherlands" / "Warehouse: Singapore",
# seen 2026-10-23): the row's dates and quantities are for the selected
# warehouse only, but page text lists every option. Show only the selected one,
# so its quantity isn't given to the others. Changes our copy of the page only.
_SHOW_SELECTED_WAREHOUSE_JS = """
() => {
  let n = 0;
  for (const s of document.querySelectorAll('select')) {
    if (s.style.display === 'none') continue;
    const opts = Array.from(s.options);
    if (!opts.length || !opts.every(o => /^warehouse:/i.test(o.text.trim()))) continue;
    const chosen = opts[s.selectedIndex >= 0 ? s.selectedIndex : 0];
    const span = document.createElement('span');
    span.textContent = chosen.text.trim();
    s.parentNode.insertBefore(span, s);
    s.style.display = 'none';
    n++;
  }
  return n;
}
"""

# Set a <select>'s value and fire its change event (what a user's pick does).
_SET_SELECT_JS = """
([selector, value]) => {
  const s = document.querySelector(selector);
  if (!s || !Array.from(s.options).some(o => o.value === value)) return false;
  s.value = value;
  s.dispatchEvent(new Event('change', {bubbles: true}));
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

# Mark the pager's enabled "next page" control with data-sourcing-next="1".
# First by its id/name/title/label/value saying "next"; failing that, the first
# control after the "Page [n] of N" box (the pager reads first, prev, box, next, last).
_MARK_NEXT_JS = """
() => {
  document.querySelectorAll('[data-sourcing-next]').forEach(el => el.removeAttribute('data-sourcing-next'));
  const sel = 'a, button, input[type=submit], input[type=image], input[type=button], ' +
              'input[type=text], input[type=number], input:not([type])';
  const all = Array.from(document.querySelectorAll(sel));
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const disabled = el => el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                         /disabled/i.test(el.className || '');
  const isControl = el => !(el.tagName === 'INPUT' && /^(text|number)$/.test(el.type || 'text'));
  const words = el => [el.id, el.getAttribute('name'), el.getAttribute('title'),
                       el.getAttribute('aria-label'), el.getAttribute('alt'),
                       el.tagName === 'INPUT' ? el.value : el.innerText,
                       el.className].join(' ');
  let next = all.find(el => isControl(el) && visible(el) && /next/i.test(words(el)) &&
                            !/last|prev/i.test(words(el)));
  if (!next) {
    const box = all.find(el => !isControl(el) && visible(el) && /^\\d+$/.test(el.value || '') &&
                               el.parentElement &&
                               /page/i.test(el.parentElement.innerText || '') &&
                               /of\\s*\\d+/i.test(el.parentElement.innerText || ''));
    if (box) next = all.slice(all.indexOf(box) + 1).find(el => isControl(el) && visible(el));
  }
  if (!next || disabled(next)) return false;
  next.setAttribute('data-sourcing-next', '1');
  return true;
}
"""


class _NoNavigation(Exception):
    """Internal: a submit didn't start a page load."""


async def _evaluate(page, script: str, arg=None):
    """page.evaluate with a time limit: on a page whose script thread is stuck,
    evaluate otherwise waits forever."""
    try:
        return await asyncio.wait_for(page.evaluate(script, arg), PAGE_SCRIPT_TIMEOUT)
    except asyncio.TimeoutError:
        raise BulkResultsIncomplete(
            f"The results page stopped responding (script took over {PAGE_SCRIPT_TIMEOUT}s)"
        ) from None


# Open the detail rows of these grid rows (jqGrid subgrids). A row that is
# already open is left as it is.
_EXPAND_ROWS_JS = """
([selector, ids]) => {
  const $ = window.jQuery;
  if (!$ || !$.fn || !$.fn.jqGrid) return 0;
  let grid = selector ? $(selector) : $();
  if (!grid.length) grid = $('table.ui-jqgrid-btable').first();
  let opened = 0;
  for (const id of ids) {
    try { grid.jqGrid('expandSubGridRow', id); opened++; } catch (e) {}
  }
  return opened;
}
"""


def _rows_without_details(result) -> list[str]:
    """Ids of in-stock grid rows whose detail row (condition, shelf life) wasn't
    read: one record in run 03 (2026-10-13) came back without it."""
    if not isinstance(result, dict):
        return []
    details = result.get("details") or {}
    out = []
    for row in result.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rid = next((str(row[k]) for k in ROW_ID_KEYS if row.get(k) not in (None, "")), None)
        if rid and rid not in details and re.fullmatch(r"\s*0*[1-9][\d,]*\s*",
                                                         str(row.get("QtyAvailable") or "")):
            out.append(rid)
    return out


# Procart's price-break table in a part's detail row, laid out
# "Order Quantity / Price / 1-249 / $23.70 / 250-499 / $22.22 / 500+ / $20.92"
# (seen 2026-10-14; rows read off screen run it together: "Price1-59 $1.8560-118").
_BREAKS_START = re.compile(r"Order\s*Quantity\s*Price", re.IGNORECASE)
_BREAK = re.compile(r"\s*(\d[\d,]*)\s*(?:-\s*(\d[\d,]*)|\+)\s*\$\s*(\d[\d,]*\.\d{2}|\.\d{2})")
# "Minimum order quantity for this part is 50 EA" (seen 2026-10-19, AN901-4C)
_MOQ = re.compile(r"Minimum order quantity for this part is\s*(\d[\d,]*)", re.IGNORECASE)
# "Increasing order quantity to 250 will reduce your price to 22.22"
_NEXT_BREAK = re.compile(r"Increasing order quantity to\s*(\d[\d,]*)", re.IGNORECASE)


def _int(text: Optional[str]) -> Optional[int]:
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group().replace(",", "")) if match else None


def _own_record(page_text: str, part_number: str) -> Optional[str]:
    """The grid record whose Cust_part is this part (not a record that only
    mentions it, such as a substitute's). The exact number wins: ACHC3-13 and
    ACHC-3-13 are separate records with their own prices (seen 2026-10-19), so
    the number without punctuation is used only when one record alone has it."""
    def key(pn: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", pn.upper())

    records = []
    for block in re.split(r"\n\n(?=Record \d+:)", page_text):
        match = re.search(r"^\s*Cust_part:\s*(.+)$", block, re.MULTILINE)
        if match:
            records.append((match.group(1).strip(), block))
    exact = [b for pn, b in records if pn.upper() == part_number.strip().upper()]
    loose = [b for pn, b in records if key(pn) == key(part_number)]
    return exact[0] if exact else loose[0] if len(loose) == 1 else None


# A row the portal adds when it only has a look-alike number: "You entered
# 426EN10-8, closest match based on characters is 426EN9-8." Procart's own grid
# code treats such a row as a suggestion when it can't be added to the cart
# (no price, quantity blanked). Seen 2026-10-27: its Cust_part was the number
# asked for, so it was written as a second, priceless 426EN10-8 line. A
# closest-match row that can be bought (ACHC3-13 for ACHC-3-13) stays.
_CLOSEST_MATCH = re.compile(r"closest match based on characters is", re.IGNORECASE)
_RECORD_SPLIT = re.compile(r"\n\n(?=Record \d+:)")


def _drop_suggestions(text: str) -> str:
    """Grid record text without the portal's look-alike suggestion rows (record
    text leaves out False values, so "not buyable" = no "lAddToCart: True")."""
    blocks = _RECORD_SPLIT.split(text)
    kept = [b for b in blocks
            if not (b.startswith("Record ")
                    and _CLOSEST_MATCH.search(b)
                    and not re.search(r"^\s*lAddToCart:\s*True\s*$", b, re.MULTILINE))]
    dropped = len(blocks) - len(kept)
    if not dropped:
        return text
    parts = [re.search(r"^\s*Cust_part:\s*(.+)$", b, re.MULTILINE) for b in blocks if b not in kept]
    logger.info("Results: left out %d closest-match suggestion row(s) the portal won't sell: %s",
                dropped, ", ".join(m.group(1).strip() for m in parts if m) or "?")
    return "\n\n".join(kept)


# How Procart marks a substitute: "SWITCH<br>This is an approved substitute part
# for part #426EN9-8" in the substitute's own row (2026-10-27).
_SUBSTITUTE_NOTE = re.compile(
    r"(?:This is an\s+)?approved substitute part for part #\s*(\S+?)\.?(?=\s|$)", re.IGNORECASE)


def _listed_substitutes(page_text: str) -> set[tuple[str, str]]:
    """(substitute, requested part) pairs, upper case, that the page itself
    states: in a grid record the note's own Cust_part; in page text the part
    number that starts the note's line, or (note on a line of its own, below
    the description) the result row just above it."""
    pairs = set()
    for block in _RECORD_SPLIT.split(page_text):
        cust = re.search(r"^\s*Cust_part:\s*(.+)$", block, re.MULTILINE)
        lines = block.splitlines()
        for n, line in enumerate(lines):
            for m in _SUBSTITUTE_NOTE.finditer(line):
                if cust:
                    sub = cust.group(1)
                else:
                    head = line[:m.start()]
                    if not head.strip():
                        head = next((ln for ln in reversed(lines[max(0, n - 3):n])
                                     if ln.startswith("\t") and ln.strip()), "")
                    # Columns are tab-separated in page text: a part number with a
                    # space ("2758 CHARGED", 2026-11-04) is the whole first column,
                    # not its first word (it was dropped as "not listed").
                    head = head.strip()
                    sub = head.split("\t")[0] if "\t" in head else (head.split() or [""])[0]
                pairs.add((sub.strip().upper(), m.group(1).strip().upper()))
    return pairs


def _keep_listed_substitutes(offers, page_text: str, asked: str) -> None:
    """Only a part the page calls an approved substitute for the requested part
    stays a substitute; any other line marked so is a look-alike number, never
    written (client, 2026-10-28: the exact part or its substitutes only)."""
    pairs = _listed_substitutes(page_text)
    for offer in offers:
        if (offer.listing == "substitute"
                and ((offer.part_number or "").strip().upper(), asked.strip().upper()) not in pairs):
            logger.info("Results: %s isn't listed on the page as a substitute for %s; left out",
                        offer.part_number, asked)
            offer.listing = "other"


def _price_breaks(record: str) -> list[tuple[int, Optional[int], str]]:
    """(from qty, to qty or None for "500+", unit price) rows of a record's own
    price-break table; [] when the table isn't on the page."""
    start = _BREAKS_START.search(record)
    if not start:
        return []
    pos, breaks = start.end(), []
    while match := _BREAK.match(record, pos):
        low, high, price = match.groups()
        breaks.append((int(low.replace(",", "")),
                       int(high.replace(",", "")) if high else None, price.replace(",", "")))
        pos = match.end()
    return breaks


def single_unit_price(page_text: str, part_number: str) -> Optional[str]:
    """Price of one piece: the row of the part's price-break table that starts at
    1. The Price column shows this so a team member can compare it with Price at Qty
    Needed (asked 2026-10-16): the line's own price may already be the break
    price, since the bulk search sends the quantity needed. None without a table."""
    record = _own_record(page_text, part_number)
    for low, high, price in _price_breaks(record or ""):
        if low <= 1 and (high is None or high >= 1):
            return price
    return None


def price_at_qty(page_text: str, part_number: str, unit_price: Optional[str],
                 qty_needed: Optional[str]) -> Optional[str]:
    """Unit price for the quantity needed, from the part's own price-break table;
    None when it can't be told (the team member then checks the page)."""
    need = _int(qty_needed)
    record = _own_record(page_text, part_number)
    if not unit_price or need is None or need <= 1 or record is None:
        return None
    start = _BREAKS_START.search(record)
    if start:
        for low, high, price in _price_breaks(record):
            if low <= need and (high is None or need <= high):
                return price
    note = _NEXT_BREAK.search(record)
    if note and need < int(note.group(1).replace(",", "")):
        return unit_price             # below the first quantity break
    if not start and not note and "Price_breaks: Yes" not in record:
        return unit_price             # no quantity pricing for this part
    return None


def _use_single_unit_price(offer, page_text: str) -> None:
    """Price = one piece's price from the price-break table, for a line the model
    gave a price (value found on the page). The table row is page data,
    so the swap needs no further check."""
    single = offer.price and single_unit_price(page_text, offer.part_number)
    try:
        same = single and float(single) == float(offer.price.replace(",", ""))
    except ValueError:
        same = False
    if single and not same:
        logger.info("Price of %s: %s (1 piece, price-break table) instead of %s from the line",
                    offer.part_number, single, offer.price)
        offer.price = single


def _use_price_breaks(offer, page_text: str) -> None:
    """The line's whole price-break table for Internal Comments, read from its own
    grid record rather than the model's copy (same rows, no reading slips). The
    model's table stays when the record has none (e.g. the row didn't expand)."""
    if not offer.price:
        return   # no stock shown: no prices are written (prompt rule 2.2)
    breaks = _price_breaks(_own_record(page_text, offer.part_number) or "")
    if breaks:
        offer.price_breaks = [(str(low), "" if high is None else str(high), price)
                              for low, high, price in breaks]


# The part's own description; the portal adds its notes after it ("SEAL This is
# an approved substitute part for part #...", "RECEPTACLE You entered ACHC-3-13,
# closest match ... is ACHC3-13.", seen 2026-10-19), which are cut off.
_DESCRIPTION = re.compile(r"^\s*Item_description1:\s*(.+)$", re.MULTILINE)
_DESCRIPTION_NOTES = re.compile(r"\s+(?:This is an approved substitute|You entered )", re.IGNORECASE)
_UOM = re.compile(r"^\s*Um:\s*(\S+)\s*$", re.MULTILINE)


def _use_description_and_uom(offer, page_text: str) -> None:
    """Description and unit of measure from the part's own grid record (RFQ tab)."""
    record = _own_record(page_text, offer.part_number) or ""
    match = _DESCRIPTION.search(record)
    if match:
        offer.description = _DESCRIPTION_NOTES.split(match.group(1), 1)[0].strip() or None
    match = _UOM.search(record)
    if match:
        offer.uom = match.group(1)


def _use_moq(offer, page_text: str) -> None:
    """MOQ from the part's own grid record (the portal's fixed sentence), so it
    doesn't depend on the model; the model's value stays when the record has none."""
    match = _MOQ.search(_own_record(page_text, offer.part_number) or "")
    if match:
        offer.moq = match.group(1).replace(",", "")


GRID_EXTRAS_MAX_CHARS = 15_000
# DEBUG_DUMP_PAGES=1 saves every results page (text sent to the model + HTML)
# under sourcing-proto/debug_pages/ to check what the portal shows. Local
# debugging only: the files hold vendor prices.
DUMP_DIR = Path(__file__).resolve().parents[2] / "debug_pages"


async def _page_text(page, label: str) -> str:
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


def _css_attr(name: str, value: str) -> str:
    return f'[{name}="{value.replace(chr(34), chr(92) + chr(34))}"]'


async def _settle(page, timeout_ms: int = 10_000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


async def _first_visible(page, selectors: list[Optional[str]]) -> Optional[str]:
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


async def _consent_button(container) -> tuple[Optional[object], str]:
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


async def _body_text(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


class ProponentAdapter(VendorAdapter):
    vendor_id = "proponent"
    display_name = "Proponent"

    allowed_hosts = ("procart.proponent.com",)
    default_login_url = LOGIN_URL
    max_parts_per_search = BULK_MAX_LINES
    default_bulk_batch_size = BULK_DEFAULT_BATCH
    # job_main always sets bulk_min_parts (2 by default, None with --one-by-one), so this
    # only applies when the adapter is used without job_main (tests, scripts).
    # REMOVE-IF-PER-PART-DROPPED: delete this line to inherit the base default (2).
    default_bulk_min_parts = None
    # Procart is slow: after a submit the next page often only starts loading
    # 25-35s later, and the results grid's controls stay busy as long (seen in
    # session recordings 2026-10-12). 20s cut those off as failures.
    default_action_timeout_seconds = 60
    # A home-page search lands on this address (seen in logs 2026-10-13), so a
    # part's results open directly without the home page and its postback.
    default_part_url_template = PART_RESULTS_URL
    # Procart shows prices with "$" only, never a code (saved pages 2026-10-13);
    # it is Proponent's US portal, so USD. Vendor config `currency` overrides.
    default_currency = "USD"
    default_not_sold_phrases = NOT_SOLD_PHRASES
    grid_empty = False   # the last grid read found a grid with no rows

    @property
    def probe_url(self) -> str:
        return self.config.get("probe_url") or HOME_URL

    async def _on_login_page(self, page) -> bool:
        # Decide by the form, not the URL alone: an ASP.NET postback can render
        # the signed-in page while the address is still Login.aspx.
        if await _first_visible(page, PASSWORD_SELECTORS) is not None:
            return True
        return ("login.aspx" in page.url.lower()
                and await _first_visible(page, USERNAME_SELECTORS) is not None)

    @property
    def logged_out_selector(self) -> str:
        return self.config.get("logged_out_selector") or LOGGED_OUT_SELECTOR

    async def shows_logged_out(self, page) -> bool:
        """True if the page has the header's Login button (only shown logged out)."""
        return await _first_visible(page, [self.logged_out_selector]) is not None

    async def _submit_and_wait(self, page, action, what: str) -> bool:
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
                                              timeout=self.page_load_timeout_ms):
                await action()
                try:
                    await asyncio.wait_for(nav_started.wait(), self.action_timeout_ms / 1000)
                except asyncio.TimeoutError:
                    raise _NoNavigation() from None
                logger.info("%s: submitted; waiting up to %ds for the page", what,
                            self.page_load_timeout_ms // 1000)
            navigated = True
        except _NoNavigation:
            navigated = False
        except PlaywrightTimeoutError:
            logger.warning("%s: the page started loading but didn't finish within %ds (%s)",
                           what, self.page_load_timeout_ms // 1000, page.url)
            raise
        finally:
            page.remove_listener("request", on_request)
        await _settle(page, timeout_ms=15_000)
        if navigated:
            logger.info("%s: page ready after %.1fs (%s)", what, time.time() - started, page.url)
        else:
            logger.info("%s: no new page within %ds (updated in place, or the submit didn't "
                        "fire); now at %s", what, self.action_timeout_ms // 1000, page.url)
        return navigated

    async def is_authenticated(self, page) -> bool:
        self.apply_timeouts(page)
        await page.goto(self.probe_url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._on_login_page(page):
            logger.info("Auth probe %s: login form shown -> not signed in", page.url)
            return False
        if await self.shows_logged_out(page):
            logger.info("Auth probe %s: header shows the Login button (%s) -> not signed in",
                        page.url, self.logged_out_selector)
            return False
        marker = self.config.get("logged_in_selector")
        if marker and await _first_visible(page, [marker]) is None:
            logger.info("Auth probe %s: signed-in marker %s not found -> not signed in",
                        page.url, marker)
            return False
        logger.info("Auth probe %s: no Login button%s -> signed in", page.url,
                    f", marker {marker} present" if marker else "")
        return True

    # -- login ------------------------------------------------------------
    async def _form_fields(self, page) -> tuple[Optional[str], Optional[str]]:
        user = await _first_visible(page, [self.selectors.get("username_input"), *USERNAME_SELECTORS])
        pwd = await _first_visible(page, [self.selectors.get("password_input"), *PASSWORD_SELECTORS])
        return user, pwd

    async def _open_login_form(self, page, cred: VendorCredential):
        """Get to a login form, trying each start URL in turn (see
        VendorAdapter.login_url_candidates). If a start URL is a home page, follow
        its Login link. Returns (username_sel, password_sel), or None when the
        Context turns out to be signed in already."""
        candidates = self.login_url_candidates(cred)
        if not candidates:
            # A setup problem: trying again changes nothing.
            raise LoginFailed(f"No usable login URL for {self.display_name}: set `login_url` "
                              f"in the vendor config or `url` in {cred.secret_key}",
                              retryable=False, credentials_sent=False)
        tried = []
        for source, url in candidates:
            logger.info("Login: opening %s (from %s)", url, source)
            await page.goto(url, wait_until="domcontentloaded")
            await _settle(page)
            # Before touching the form: Accept reloads the page and would wipe it.
            await accept_cookies(page)
            if not host_allowed(page.url, self.hosts):
                logger.warning("Login: %s redirected to %s, outside %s; not using it",
                               url, page.url, ", ".join(self.hosts))
                tried.append(f"{url} (redirected off-site)")
                continue

            user_sel, pass_sel = await self._form_fields(page)
            if not (user_sel and pass_sel):
                link = await _first_visible(page, LOGIN_LINK_SELECTORS)
                if link:
                    logger.info("Login: no form at %s; following login link %s", page.url, link)
                    await self._submit_and_wait(
                        page, lambda: page.click(link, no_wait_after=True), "Login link")
                    await _settle(page)
                    await accept_cookies(page)
                    if not host_allowed(page.url, self.hosts):
                        logger.warning("Login: login link led to %s, outside %s; not using it",
                                       page.url, ", ".join(self.hosts))
                        tried.append(f"{url} (login link went off-site)")
                        continue
                    user_sel, pass_sel = await self._form_fields(page)

            if user_sel and pass_sel:
                if page.url.split("?")[0] != url.split("?")[0]:
                    logger.info("Login: login page is %s; saving it for next runs", page.url)
                    self.learned["login_page_url"] = page.url.split("#")[0]
                return user_sel, pass_sel
            if not await self._on_login_page(page) and not await self.shows_logged_out(page):
                logger.info("Login: no login form at %s and no Login button; already signed in",
                            page.url)
                return None
            logger.warning("Login: no login form found from %s (now at %s)", url, page.url)
            tried.append(url)
        raise LoginFailed("No login form found (tried: " + "; ".join(tried) + "). Set "
                          "`login_url` in the vendor config to the vendor's login page.",
                          credentials_sent=False)

    async def login(self, page, cred: VendorCredential) -> None:
        self.apply_timeouts(page)
        logger.info("Login: %s as %s", self.display_name, cred.username)
        try:
            await self._login(page, cred)
        except PlaywrightTimeoutError as exc:
            # e.g. an overlay we don't know intercepting a click
            first_line = str(exc).splitlines()[0]
            blocker = next((l.strip() for l in str(exc).splitlines()
                            if "intercepts pointer events" in l), "")
            logger.warning("Login: timed out at %s: %s%s", page.url, first_line,
                           f" | {blocker[:200]}" if blocker else "")
            raise LoginFailed(f"Timed out at {page.url}: {first_line}"
                              + (f" ({blocker[:150]})" if blocker else "")) from exc

    async def _login(self, page, cred: VendorCredential) -> None:
        fields = await self._open_login_form(page, cred)
        if fields is None:
            return
        user_sel, pass_sel = fields
        # Last check before typing: never send credentials to another site.
        if not host_allowed(page.url, self.hosts):
            raise LoginBlocked(f"Refusing to enter credentials on {page.url}")
        logger.info("Login: form found at %s (username=%s, password=%s)", page.url, user_sel,
                    pass_sel)
        await page.fill(user_sel, cred.username)
        await page.fill(pass_sel, cred.password)
        submit_sel = await _first_visible(page, [self.selectors.get("submit_button"), *SUBMIT_SELECTORS])
        if submit_sel:
            logger.info("Login: submitting with %s", submit_sel)
            await self._submit_and_wait(
                page, lambda: page.click(submit_sel, no_wait_after=True), "Login submit")
        else:
            logger.info("Login: no submit button found; pressing Enter")
            await self._submit_and_wait(
                page, lambda: page.press(pass_sel, "Enter", no_wait_after=True), "Login submit")
        await _settle(page)
        logger.info("Login: after submit, at %s", page.url)
        self.learned.update(username_input=user_sel, password_input=pass_sel,
                            **({"submit_button": submit_sel} if submit_sel else {}))
        await self._after_submit(page, cred, totp_tried=False)

    async def _after_submit(self, page, cred: VendorCredential, *, totp_tried: bool) -> None:
        text = await _body_text(page)
        blocked = next((h for h in BLOCKED_HINTS if h in text), None)
        if blocked:
            logger.warning("Login: page mentions %r -> stopping (lockout / CAPTCHA / block)",
                           blocked)
            raise LoginBlocked(f"Portal reports a lockout or block ({blocked!r})")
        if "captcha" in text:
            # For now tried again, not a stop (web.CAPTCHA_HINTS, 2026-11-03).
            logger.warning("Login: page mentions a CAPTCHA -> login failed, may be tried again")
            raise CaptchaFailed("Portal shows a CAPTCHA")

        mfa_sel = await _first_visible(page, [self.selectors.get("mfa_code_input"), *MFA_INPUT_SELECTORS])
        on_login = await self._on_login_page(page)
        if mfa_sel or (not on_login and any(h in text for h in MFA_TEXT_HINTS)):
            if cred.totp_secret and mfa_sel and not totp_tried:
                left = 30 - (time.time() % 30)
                if left < 3:  # don't submit a code that expires in flight
                    await asyncio.sleep(left + 0.5)
                code = cred.totp_now()
                logger.info("Submitting TOTP for %s", cred.secret_key)
                await page.fill(mfa_sel, code)
                submit = await _first_visible(page, MFA_SUBMIT_SELECTORS)
                if submit:
                    await self._submit_and_wait(
                        page, lambda: page.click(submit, no_wait_after=True), "2FA submit")
                else:
                    await self._submit_and_wait(
                        page, lambda: page.press(mfa_sel, "Enter", no_wait_after=True),
                        "2FA submit")
                await _settle(page)
                self.learned["mfa_code_input"] = mfa_sel
                await self._after_submit(page, cred, totp_tried=True)
                return
            method = self._mfa_method(text, totp_tried)
            logger.warning("Login: 2FA step shown (%s, input=%s, totp seed=%s) -> needs a person",
                           method, mfa_sel, "yes" if cred.totp_secret else "no")
            raise MfaRequired(method)

        bad = on_login and next((h for h in BAD_PASSWORD_HINTS if h in text), None)
        if bad:
            logger.warning("Login: portal says %r -> password needs updating", bad)
            raise InvalidCredentials(f"Portal rejected the username or password ({bad!r})")
        server_error = next((h for h in SERVER_ERROR_HINTS if h in text), None)
        if server_error:
            logger.warning("Login: server error page after submit (%r)", server_error)
            raise LoginFailed(f"Server error after the login submit ({server_error!r})")
        if on_login:
            # Most likely a rejection in words we don't know: a second submit could
            # be the failed attempt that locks the account.
            logger.warning("Login: still on the login form after submit, no error text found")
            raise LoginFailed("Still on the login page after submit, no error message; not "
                              "retried in case the portal counts it as a failed login",
                              retryable=False)
        if await self.shows_logged_out(page):
            logger.warning("Login: left the form but the header still shows the Login button")
            raise LoginFailed("Header still shows the Login button after submit")
        logger.info("Login: form gone and no Login button -> looks signed in (%s)", page.url)

    @staticmethod
    def _mfa_method(text: str, totp_tried: bool) -> str:
        if totp_tried:
            return "totp_rejected"
        if "authenticator" in text:
            return "totp_no_seed"
        if "text message" in text or "sms" in text or "phone" in text:
            return "sms"
        if "email" in text:
            return "email"
        return "unknown"

    # -- search -----------------------------------------------------------
    async def _find_search_input(self, page) -> str:
        candidates = [self.selectors.get("search_input"), *SEARCH_INPUT_SELECTORS]
        selector = await _first_visible(page, candidates)
        if not selector:
            await page.goto(self.config.get("search_url") or self.probe_url,
                            wait_until="domcontentloaded")
            await _settle(page)
            if await self._on_login_page(page):
                raise SessionExpired("Redirected to login while opening search")
            selector = await _first_visible(page, candidates)
        if selector:
            self.learned["search_input"] = selector
            return selector

        if self.extractor is None:
            raise RuntimeError("Search input not found and no LLM configured to locate it")
        inputs = await page.evaluate(_LIST_INPUTS_JS)
        index, usage = await self.extractor.pick_search_input(inputs)
        self.usage.add(usage)
        if index is None:
            raise RuntimeError("Could not identify the part search input on Proponent")
        chosen = inputs[index]
        if chosen.get("id"):
            selector = _css_attr("id", chosen["id"])
        elif chosen.get("name"):
            selector = _css_attr("name", chosen["name"])
        else:
            return _css_attr("data-sourcing-idx", str(index))  # this page only, not saved
        logger.info("Claude picked search input %s: %s", selector, json.dumps(chosen)[:200])
        self.learned["search_input"] = selector
        return selector

    async def search(self, page, row: RequestRow) -> SearchOutcome:
        """One part (not the bulk box). Opens the results address directly
        (PartSearchResults.aspx?id=<part>, see VendorAdapter.part_url): one page
        load instead of fill + submit + postback. Falls back to the site's own
        PART NUMBER search when there's no address or it doesn't work. The
        results address is the line's Source URL."""
        self.apply_timeouts(page)
        # The page's own data requests, logged when the grid comes back empty:
        # tells "portal answered nothing" from "its request failed".
        self.data_requests: list[str] = []

        def note(response) -> None:
            if response.request.resource_type in ("xhr", "fetch"):
                self.data_requests.append(f"HTTP {response.status} {response.url[:160]}")

        page.on("response", note)
        try:
            results_url = await self._open_results_by_url(page, row)
            if results_url is None:
                results_url = await self._open_results_by_form(page, row)
            return await self._read_results(page, row, results_url)
        finally:
            page.remove_listener("response", note)

    async def _open_results_by_url(self, page, row: RequestRow) -> Optional[str]:
        """Go straight to this part's results address. Returns it, or None to use
        the search form (no template, or it failed earlier in this run)."""
        template = self.part_url
        if not template or self.part_url_failed:
            return None
        url = fill_part_url(template, row.part_number)
        if not host_allowed(url, self.hosts):
            self.part_url_failed = f"{url} is not on {', '.join(self.hosts)}"
            logger.warning("Search: direct address not used: %s", self.part_url_failed)
            return None
        started = time.time()
        logger.info("Search: opening %s directly", url)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._on_login_page(page):
            raise SessionExpired("Redirected to login when opening the results address")
        if await self.shows_logged_out(page):
            # Public results pages still answer; their "no results" would be a false NOT_FOUND.
            raise SessionExpired("Header shows the Login button on the results page")
        status = response.status if response is not None else 0
        landed = urlsplit(page.url)
        wanted = urlsplit(url)
        if status >= 400 or (landed.hostname, landed.path.lower()) != (wanted.hostname,
                                                                       wanted.path.lower()):
            self.part_url_failed = f"{url} gave HTTP {status} at {page.url}"
            logger.warning("Search: direct address didn't open a results page (%s); using the "
                           "search form for the rest of this run", self.part_url_failed)
            return None
        logger.info("Search: results for %s opened directly in %.1fs", row.part_number,
                    time.time() - started)
        if "part_url_template" not in self.config:
            self.learned["part_url_template"] = template
        return url

    async def _open_results_by_form(self, page, row: RequestRow) -> str:
        """The site's own PART NUMBER search: fill the box, submit, wait for the
        results. Learns the results address template from where it lands."""
        await accept_cookies(page)
        if await self.shows_logged_out(page):
            raise SessionExpired("Header shows the Login button before searching")
        selector = await self._find_search_input(page)
        await page.fill(selector, row.part_number)
        before = await page.inner_text("body")
        button = await _first_visible(page, [self.selectors.get("search_button"), SEARCH_BUTTON])
        if button:
            logger.info("Search: %s in %s, submitting with %s", row.part_number, selector, button)
            navigated = await self._submit_and_wait(
                page, lambda: page.click(button, no_wait_after=True), f"Search {row.part_number}")
        else:
            logger.info("Search: %s in %s, pressing Enter", row.part_number, selector)
            navigated = await self._submit_and_wait(
                page, lambda: page.press(selector, "Enter", no_wait_after=True),
                f"Search {row.part_number}")
        if not navigated and await page.inner_text("body") == before:
            # Reading this page would report the search form as "results".
            raise RuntimeError(f"Search for {row.part_number} did not submit (page unchanged)")

        if await self._on_login_page(page):
            raise SessionExpired("Redirected to login after search")
        if await self.shows_logged_out(page):
            # Public search pages still answer; their "no results" would be a false NOT_FOUND.
            raise SessionExpired("Header shows the Login button on the results page")
        logger.info("Search: results for %s at %s", row.part_number, page.url)
        template = part_url_template(page.url, row.part_number)
        if (template and host_allowed(template, self.hosts) and not self.part_url_failed
                and "part_url_template" not in self.config):
            logger.info("Search: results address holds the part number; next searches open %s",
                        template)
            self.learned["part_url_template"] = template
        results_url = part_results_url(page.url, row.part_number)
        if results_url != page.url:
            logger.info("Search: Source URL for %s is %s (page address was %s)",
                        row.part_number, results_url, page.url)
        return results_url

    async def _shows_no_results(self, page) -> bool:
        """An empty results grid with the portal's own "nothing found" text."""
        if not self.grid_empty:
            return False
        wanted = (self.config.get("no_results_text") or NO_RESULTS_TEXT).lower()
        return wanted in (await page.inner_text("body")).lower()

    def _log_data_requests(self, part_number: str) -> None:
        seen = getattr(self, "data_requests", [])
        logger.info("Results: page data requests for %s: %s", part_number,
                    "; ".join(seen[-5:]) or "none")

    async def _read_results(self, page, row: RequestRow, results_url: str) -> SearchOutcome:
        label = f"single-{_safe(row.part_number)}"
        await self._prepare_results(page, 1)
        raw_text = await self._grid_text(page, label)
        if raw_text is None and await self._shows_no_results(page):
            # Once seen for a part in stock: load the page again before believing it.
            self._log_data_requests(row.part_number)
            logger.info("Results: %s shows no results; loading the page again", row.part_number)
            await page.goto(results_url if host_allowed(results_url, self.hosts) else page.url,
                            wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)
            if await self._on_login_page(page) or await self.shows_logged_out(page):
                raise SessionExpired("Signed out when loading the results again")
            await self._prepare_results(page, 1)
            raw_text = await self._grid_text(page, label)
            if raw_text is None and await self._shows_no_results(page):
                self._log_data_requests(row.part_number)
                logger.info("Results: %s still shows no results", row.part_number)
                usage, self.usage = self.usage, type(self.usage)()
                return SearchOutcome(found=False, usage=usage, empty_page=True,
                                     page_url=results_url, grid_used=False)
        # Page-change signal: did the LLM read the grid records or page-text fallback?
        grid_used = bool(raw_text)
        if not raw_text:
            await self._expand_all_details(page)
            raw_text = await _page_text(page, f"single-{_safe(row.part_number)}")
        if any(h in raw_text.lower() for h in EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(raw_text)

        if len(raw_text) > self.max_page_chars:
            logger.warning("Results page for %s is %d chars; sending the first %d",
                           row.part_number, len(raw_text), self.max_page_chars)
            raw_text = raw_text[: self.max_page_chars]
        found, kind, offers, usage = await self.extractor.extract_offers(
            page_text=raw_text, page_url=results_url, part_number=row.part_number,
            qty_needed=row.qty_needed, condition_wanted=row.condition_wanted,
        )
        usage.add(self.usage)
        self.usage = type(self.usage)()
        if kind == "login_page":
            raise SessionExpired("Extractor saw a login page")
        _keep_listed_substitutes(offers, raw_text, row.part_number)
        self._mark_gaps(offers)
        for offer in offers:
            _use_single_unit_price(offer, raw_text)
            _use_price_breaks(offer, raw_text)
            _use_moq(offer, raw_text)
            _use_description_and_uom(offer, raw_text)
            offer.qty_price = price_at_qty(raw_text, offer.part_number, offer.price,
                                           row.qty_needed)
        not_sold = self.not_sold_reason(raw_text, row.part_number, offers)
        if not_sold:
            logger.info("Results: %s doesn't sell %s: %s", self.display_name,
                        row.part_number, not_sold)
            return SearchOutcome(found=False, usage=usage, not_sold=not_sold,
                                 page_url=results_url, page_kind=kind, grid_used=grid_used)
        return SearchOutcome(found=found, offers=offers, usage=usage, page_kind=kind,
                             grid_used=grid_used)

    # -- bulk search --------------------------------------------------------
    bulk_unavailable: Optional[str] = None   # set once the bulk box fails this run

    async def search_many(self, page, rows: list[RequestRow]):
        if self.bulk_unavailable is not None:
            raise BulkSearchUnavailable(self.bulk_unavailable)
        try:
            return await self._search_many(page, rows)
        except BulkResultsIncomplete:
            raise  # this batch only; the bulk box itself works
        except BulkSearchUnavailable as exc:
            self.bulk_unavailable = str(exc)
            raise

    async def _search_many(self, page, rows: list[RequestRow]):
        self.apply_timeouts(page)
        url = self.config.get("bulk_search_url") or HOME_URL
        logger.info("Bulk search: %d parts in one search (%s)", len(rows), url)
        await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._on_login_page(page) or await self.shows_logged_out(page):
            raise SessionExpired("Not signed in on the bulk search page")
        if await _first_visible(page, [BULK_TEXTAREA]) is None:
            raise BulkSearchUnavailable(f"Bulk search box {BULK_TEXTAREA} not found at {page.url}")

        lines = [f"{r.part_number}\t{_qty(r.qty_needed)}" for r in rows]
        await page.fill(BULK_TEXTAREA, "\n".join(lines))
        before = await page.inner_text("body")
        navigated = await self._submit_and_wait(
            page, lambda: page.click(BULK_SUBMIT, no_wait_after=True), "Bulk search")
        if not navigated and await page.inner_text("body") == before:
            raise BulkSearchUnavailable("Bulk search did not submit (page unchanged)")
        logger.info("Bulk search: results at %s", page.url)

        if await self._on_login_page(page) or await self.shows_logged_out(page):
            raise SessionExpired("Logged out after the bulk search")
        try:
            message = (await page.locator(BULK_MESSAGE).first.inner_text(timeout=1_000)).strip()
        except Exception:
            message = ""
        if message:
            check_rate_limited(message)
            raise BulkSearchUnavailable(f"Portal message after bulk search: {message[:200]}")

        # The grid's own records first: every result, labeled, no paging. Page
        # text (page size, expand, paging) only when there's no readable grid.
        # Draw every result on one page and open its details first: the grid
        # holds all rows, but product details (e.g. the part condition) are only
        # loaded into rows that are drawn and expanded.
        await self._prepare_results(page, len(rows))
        grid_text = await self._grid_text(page, "bulk")
        pages = [grid_text] if grid_text else await self._read_result_pages(page, len(rows))
        for text in pages:
            check_rate_limited(text)
        return await self._offers_from_pages(rows, pages, grid_text, page.url)

    async def _offers_from_pages(self, rows: list[RequestRow], pages: list[str],
                                 grid_text: Optional[str], page_url: str):
        """Bulk results text -> {row_id: SearchOutcome}, usage. Separate from the
        browser steps so a saved results page can be read again the same way."""
        parts = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                  "condition_wanted": r.condition_wanted} for r in rows]
        by_part: dict[str, list] = {}
        usage = type(self.usage)()
        for chunk in _chunk_pages(pages, self.max_page_chars):
            kind, found, chunk_usage = await self.extractor.extract_offers_bulk(
                page_text=chunk, page_url=page_url, parts=parts)
            usage.add(chunk_usage)
            if kind == "login_page":
                raise SessionExpired("Extractor saw a login page")
            if kind == "error":
                raise BulkSearchUnavailable("Could not read the bulk results page")
            for pn, offers in found.items():
                by_part.setdefault(pn, []).extend(offers)
        usage.add(self.usage)
        self.usage = type(self.usage)()
        outcomes = {}
        page_text = "\n\n".join(pages)
        for r in rows:
            offers = by_part.get(r.part_number) or []
            _keep_listed_substitutes(offers, page_text, r.part_number)
            self._mark_gaps(offers)
            for offer in offers:
                if grid_text:
                    _use_single_unit_price(offer, grid_text)
                    _use_price_breaks(offer, grid_text)
                    _use_moq(offer, grid_text)
                    _use_description_and_uom(offer, grid_text)
                offer.qty_price = grid_text and price_at_qty(
                    grid_text, offer.part_number, offer.price, r.qty_needed) or None
            # Grid records only: page text can't be told apart per part.
            not_sold = grid_text and self.not_sold_reason(grid_text, r.part_number, offers)
            if not_sold:
                outcomes[r.row_id] = SearchOutcome(found=False, not_sold=not_sold,
                                                   page_url=page_url, grid_used=bool(grid_text))
            elif offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers,
                                                   grid_used=bool(grid_text))
        logger.info("Bulk search: offers for %d of %d parts", len(outcomes), len(rows))
        return outcomes, usage

    async def _results_text(self, page) -> str:
        await self._expand_all_details(page)
        text = await _page_text(page, "bulk")
        if any(h in text.lower() for h in EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        if await self._on_login_page(page) or await self.shows_logged_out(page):
            raise SessionExpired("Logged out while reading the bulk results")
        return text

    async def _read_result_pages(self, page, n_parts: int) -> list[str]:
        """Text of every bulk results page. The results are paged ("Viewing results
        1 - 10 of 24", "Results per page: 10"): first raise the page size so all
        results fit on one page, then step through any pages still left. Parts on
        an unread page would otherwise come back as false NOT_FOUNDs, so if the
        pages can't all be read this raises BulkSearchUnavailable and the run
        falls back to one search per part."""
        text = await self._results_text(page)
        pager = _pager(text)
        if pager is None or pager[1] >= pager[2]:
            return [text]  # everything is on this page

        start, end, total = pager
        if await self._raise_page_size(page, max(total, n_parts), total):
            text = await self._results_text(page)
            pager = _pager(text) or (1, total, total)
            start, end, total = pager
        pages = [text]
        logger.info("Bulk search: results %d-%d of %d on page 1", start, end, total)

        # Results that still don't fit: read the remaining pages one by one.
        max_pages = BULK_MAX_RESULT_PAGES
        while end < total:
            if len(pages) >= max_pages:
                raise BulkResultsIncomplete(
                    f"Bulk results run past {max_pages} pages ({end} of {total} read)")
            await self._wait_grid_idle(page)
            nxt = await self._mark_next_page_button(page)
            if nxt is None:
                raise BulkResultsIncomplete(
                    f"Bulk results show {end} of {total} but no next-page button was found")
            # The results grid pages in place (no new page load): click, then wait
            # until the pager shows the next range.
            await page.click(nxt, no_wait_after=True)
            new = await self._wait_for_pager(page, lambda p, s=start: p[0] > s)
            if new is None:
                raise BulkResultsIncomplete(
                    f"Bulk results did not move to the next page (still at {start}-{end} "
                    f"of {total})")
            text = await self._results_text(page)
            start, end, total = _pager(text) or new
            pages.append(text)
            logger.info("Bulk search: results %d-%d of %d on page %d", start, end, total,
                        len(pages))
        return pages

    async def _raise_page_size(self, page, wanted: int, total: int) -> bool:
        """Set the "Results per page" dropdown to the smallest size that holds
        `wanted` results (or its largest size). Returns True once the grid shows
        the bigger page. Any problem here is not fatal: the caller then reads
        the results page by page."""
        for attempt in (1, 2):
            await self._wait_grid_idle(page)
            selects = await _evaluate(page, _LIST_SELECTS_JS)
            pick = _page_size_choice(selects, wanted)
            if pick is None:
                logger.info("Bulk search: no 'results per page' dropdown found")
                return False
            selector, value, size, current = pick
            if current is not None and current >= size:
                return False
            logger.info("Bulk search: setting results per page to %d (%d results wanted)",
                        size, wanted)
            try:
                await page.select_option(selector, value=value, no_wait_after=True,
                                         timeout=self.action_timeout_ms)
            except BulkResultsIncomplete:
                raise
            except Exception as exc:
                # Playwright waits for the box to look visible and enabled; the grid
                # only listens for the change event, so set the value directly.
                logger.info("Bulk search: dropdown not clickable (%s); setting it directly",
                            str(exc).splitlines()[0])
                if not await _evaluate(page, _SET_SELECT_JS, [selector, value]):
                    logger.warning("Bulk search: could not change results per page "
                                   "(attempt %d)", attempt)
                    continue
            target = min(total, size)
            if await self._wait_for_pager(page, lambda p: p[1] >= target) is not None:
                return True
            logger.warning("Bulk search: results per page changed but the grid still shows "
                           "fewer than %d results (attempt %d)", target, attempt)
        logger.warning("Bulk search: reading the results page by page instead")
        return False

    async def _wait_for_pager(self, page, ok) -> Optional[tuple[int, int, int]]:
        """Poll the pager text until ok((start, end, total)) holds; works whether
        the grid updates in place or the page reloads. None on timeout."""
        deadline = time.time() + self.page_load_timeout_ms / 1000
        while time.time() < deadline:
            try:
                pager = _pager(await page.inner_text("body", timeout=5_000))
            except Exception:
                pager = None  # page in the middle of reloading
            if pager is not None and ok(pager):
                await self._wait_grid_idle(page)
                return pager
            await asyncio.sleep(0.5)
        return None

    async def _wait_grid_idle(self, page) -> None:
        """Wait while the results grid shows its "Loading..." overlay."""
        try:
            await page.wait_for_function(_GRID_IDLE_JS, polling=500, timeout=GRID_IDLE_TIMEOUT_MS)
        except Exception:
            logger.info("Bulk search: results grid still shows 'Loading' after %ds; going on",
                        GRID_IDLE_TIMEOUT_MS // 1000)

    async def _prepare_results(self, page, n_parts: int) -> None:
        """Best effort: one page big enough for every result, details expanded."""
        try:
            pager = _pager(await page.inner_text("body"))
            if pager and pager[1] < pager[2]:
                await self._raise_page_size(page, max(pager[2], n_parts), pager[2])
        except (SessionExpired, BulkResultsIncomplete):
            raise
        except Exception as exc:
            logger.info("Results: page size not changed (%s)", str(exc).splitlines()[0][:200])
        await self._expand_all_details(page)

    async def _grid_text(self, page, label: str) -> Optional[str]:
        """The results grid's records as labeled text, or None to fall back to
        page text: no grid on the page, no rows, or fewer rows than the pager
        says there are (never risk silently dropping parts)."""
        selector = self.config.get("results_grid", RESULTS_GRID)
        deadline = time.time() + GRID_WAIT_SECONDS
        last_count = None
        self.grid_empty = False
        while True:
            try:
                result = await _evaluate(page, GRID_RECORDS_JS, selector)
            except BulkResultsIncomplete:
                raise
            except Exception as exc:  # page reloading under us
                logger.debug("Grid read failed: %r", exc)
                result = None
            parsed = parse_grid_result(result)
            if parsed:
                count = parsed[1]
                if count == last_count:
                    break                       # same rows twice in a row: loaded
                last_count = count
            elif result is None:
                return None                     # no jqGrid on this page (built at page load)
            elif time.time() > deadline:
                # Grid still has no rows: let page text decide. 3s wasn't always
                # enough for its data to arrive (false NOT_FOUND, 2026-10-14).
                self.grid_empty = True
                return None
            if time.time() > deadline:
                if not parsed:
                    return None
                break
            await asyncio.sleep(0.5)

        missing = _rows_without_details(result)
        if missing:
            logger.info("Results: %d in-stock record(s) without their details; opening them",
                        len(missing))
            try:
                await _evaluate(page, _EXPAND_ROWS_JS, [selector, missing])
                await _settle(page, timeout_ms=10_000)
                await self._wait_grid_idle(page)
                again = await _evaluate(page, GRID_RECORDS_JS, selector)
                reparsed = parse_grid_result(again)
                if reparsed and reparsed[1] == parsed[1]:
                    result, parsed = again, reparsed
            except BulkResultsIncomplete:
                raise
            except Exception as exc:
                logger.info("Results: could not open the details (%s)",
                            str(exc).splitlines()[0][:200])
            still = _rows_without_details(result)
            if still:
                logger.warning("Results: %d in-stock record(s) still without details; their "
                               "condition may show as not shown", len(still))

        text, count, grid_total = parsed
        body = await page.inner_text("body")
        if any(h in body.lower() for h in EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        pager = _pager(body)
        expected = max(grid_total or 0, pager[2] if pager else 0)
        if expected > count:
            logger.warning("Results grid holds %d of %d results; reading the page text instead",
                           count, expected)
            return None
        logger.info("Results: read %d records from the results grid", count)
        if os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes"):
            try:
                DUMP_DIR.mkdir(exist_ok=True)
                stem = DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-grid"
                stem.with_suffix(".txt").write_text(text, encoding="utf-8")
                stem.with_suffix(".json").write_text(dumps(result), encoding="utf-8")
                logger.info("Saved grid records to %s.(txt|json)", stem)
            except Exception as exc:
                logger.warning("Could not save the grid records: %r", exc)
        return _drop_suggestions(text)

    async def _expand_all_details(self, page) -> None:
        """Open every result's detail rows before the page is read. Procart's
        "Expand All Details" shows each part's warehouses, next due dates and
        quantities due in; rows left closed don't have them in the page text.
        Other sites: any visible "Expand all" control. Then the substitute
        lists and their rows' details, look-alike suggestions hidden, and every
        warehouse's numbers. Each step leaves what it already did alone, so a
        second call changes nothing. Never fatal."""
        try:
            if await _evaluate(page, _MARK_EXPAND_ALL_JS):
                await page.click('[data-sourcing-expand="1"]', no_wait_after=True,
                                 timeout=self.action_timeout_ms)
                await _settle(page, timeout_ms=10_000)
                await self._wait_grid_idle(page)
                logger.info("Results: expanded all details")
        except BulkResultsIncomplete:
            raise
        except Exception as exc:
            logger.info("Results: could not expand details (%s); reading as shown",
                        str(exc).splitlines()[0][:200])
        await self._show_substitutes(page)
        await self._hide_suggestions(page)
        await self._open_collapsed_rows(page)
        await self._wait_details_loaded(page)
        await self._read_other_warehouses(page)
        try:
            shown = await _evaluate(page, _SHOW_SELECTED_WAREHOUSE_JS)
            if shown:
                logger.info("Results: %d warehouse dropdown(s) read as their selected warehouse",
                            shown)
        except Exception as exc:
            logger.info("Results: warehouse dropdowns read as shown (%s)",
                        str(exc).splitlines()[0][:200])

    async def _wait_details_loaded(self, page) -> None:
        """Wait until no result row shows "Loading..." any more: the part's
        condition ("Part Condition Factory New") is only in its loaded details.
        Rows still loading after DETAILS_WAIT_SECONDS are read as they are."""
        deadline = time.time() + DETAILS_WAIT_SECONDS
        waiting = None
        try:
            while True:
                waiting = await _evaluate(page, _DETAILS_LOADING_JS)
                if not waiting or time.time() > deadline:
                    break
                await asyncio.sleep(1)
        except Exception as exc:
            logger.info("Results: could not check the rows' details (%s)",
                        str(exc).splitlines()[0][:200])
            return
        if waiting:
            logger.warning("Results: %d row(s) still loading their details after %ds "
                           "(condition may be missing)", waiting, DETAILS_WAIT_SECONDS)

    async def _show_substitutes(self, page) -> None:
        """Open every "Show N Approved Substitutes for <part>" list, so the
        substitutes are in the page text under that heading. Never fatal."""
        try:
            count = await _evaluate(page, _MARK_SUBSTITUTE_BUTTONS_JS)
            for i in range(count or 0):
                await page.click(f'[data-sourcing-subs="{i}"]', no_wait_after=True,
                                 timeout=self.action_timeout_ms)
            if count:
                await _settle(page, timeout_ms=10_000)
                logger.info("Results: opened %d approved-substitute list(s)", count)
        except Exception as exc:
            logger.info("Results: could not open the substitute lists (%s); reading as shown",
                        str(exc).splitlines()[0][:200])

    async def _hide_suggestions(self, page) -> None:
        """Hide the portal's look-alike rows it won't sell (_HIDE_SUGGESTIONS_JS)."""
        try:
            hidden = await _evaluate(page, _HIDE_SUGGESTIONS_JS)
            if hidden:
                logger.info("Results: left out %d closest-match suggestion row(s) the portal "
                            "won't sell: %s", len(hidden), ", ".join(hidden))
        except BulkResultsIncomplete:
            raise
        except Exception as exc:
            logger.info("Results: could not check for closest-match rows (%s)",
                        str(exc).splitlines()[0][:200])

    async def _open_collapsed_rows(self, page) -> None:
        """Open each result row still closed ("+"), substitute rows above all,
        so its condition and price breaks are in the page text. Never fatal."""
        try:
            count = await _evaluate(page, _MARK_COLLAPSED_ROWS_JS) or 0
            opened = 0
            for i in range(count):
                try:
                    await page.click(f'[data-sourcing-open="{i}"]', no_wait_after=True,
                                     timeout=self.action_timeout_ms)
                    opened += 1
                except PlaywrightTimeoutError:
                    pass
            if count:
                await _settle(page, timeout_ms=10_000)
                logger.info("Results: opened the details of %d of %d closed row(s)", opened, count)
        except BulkResultsIncomplete:
            raise
        except Exception as exc:
            logger.info("Results: could not open the closed rows (%s); reading as shown",
                        str(exc).splitlines()[0][:200])

    async def _read_other_warehouses(self, page) -> None:
        """Every warehouse of a result, not only the selected one: pick each
        other warehouse in the row's dropdown, note its numbers, pick the first
        one again and add the others as rows below it (_ADD_WAREHOUSE_ROWS_JS),
        so the Warehouses cell and the comment list them all (client,
        2026-10-28). A warehouse whose numbers don't show is left out, never
        given the selected one's, and its part is flagged (_mark_gaps): the
        team (2026-11-04) wants complete results or a visible gap, never a quiet
        one. Never fatal."""
        if not hasattr(self, "_warehouse_gaps"):
            self._warehouse_gaps: dict[str, list[str]] = {}
        try:
            selects = [s for s in (await _evaluate(page, _MARK_WAREHOUSE_SELECTS_JS) or [])
                       if not s.get("done")]
        except BulkResultsIncomplete:
            raise
        except Exception as exc:
            logger.info("Results: warehouse dropdowns not read (%s)", str(exc).splitlines()[0][:200])
            return
        switches = added = 0
        missed = []
        for s in selects:
            i = s["index"]
            part = ""
            others: list[dict] = []
            try:
                part = (await _evaluate(page, _WAREHOUSE_PART_JS, i) or "").strip()
                shown = await _evaluate(page, _READ_WAREHOUSE_ROW_JS, i)
                if not shown:
                    continue
                others = [o for o in s["options"] if o["value"] != s["selected"]]
                if switches + len(others) > WAREHOUSE_SWITCHES_MAX:
                    logger.warning("Results: more than %d warehouse changes; the rest show the "
                                   "selected warehouse only", WAREHOUSE_SWITCHES_MAX)
                    self._gap(part, [o["text"] for o in others])
                    break
                first = next((o["text"] for o in s["options"] if o["value"] == s["selected"]), "")
                rows = []
                for o in others:
                    switches += 1
                    name = re.sub(r"^warehouse:\s*", "", o["text"], flags=re.IGNORECASE)
                    now = await self._pick_warehouse(
                        page, i, o["value"],
                        lambda r, name=name: (r["details"].lower() == name.lower()
                                              or r["values"] != shown["values"]),
                        label=o["text"], part=part)
                    if now:
                        rows.append({"name": o["text"], "values": now["values"]})
                    else:
                        missed.append(o["text"])
                        self._gap(part, [o["text"]])
                if others and not await self._pick_warehouse(
                        page, i, s["selected"], lambda r: r["values"] == shown["values"],
                        label=first, part=part):
                    logger.warning("Results: warehouse dropdown %d didn't return to its first "
                                   "warehouse; its other warehouses left out", i)
                    self._gap(part, [o["text"] for o in others])
                    continue
                added += await _evaluate(page, _ADD_WAREHOUSE_ROWS_JS,
                                         [i, rows, shown["cols"], shown["width"]]) or 0
            except BulkResultsIncomplete:
                raise
            except Exception as exc:
                logger.info("Results: warehouse dropdown %d not read (%s)", i,
                            str(exc).splitlines()[0][:200])
                self._gap(part, [o["text"] for o in others])
        if added or missed:
            logger.info("Results: read %d other warehouse(s)%s", added,
                        f"; no numbers shown for: {', '.join(missed)}" if missed else "")

    def _gap(self, part: str, warehouses: list[str]) -> None:
        """Warehouses of `part` whose numbers weren't read."""
        if not part or not warehouses:
            return
        names = self._warehouse_gaps.setdefault(part.upper(), [])
        for w in warehouses:
            name = re.sub(r"^warehouse:\s*", "", w, flags=re.IGNORECASE)
            if name not in names:
                names.append(name)

    def _mark_gaps(self, offers) -> None:
        """A line whose other warehouses weren't read says so: flag
        WAREHOUSES_INCOMPLETE (Flag column) and the names in Internal Comments."""
        for offer in offers:
            names = getattr(self, "_warehouse_gaps", {}).get(
                (offer.part_number or "").strip().upper())
            if not names:
                continue
            flags = [f for f in (offer.review or "").split(",") if f]
            if "WAREHOUSES_INCOMPLETE" not in flags:
                flags.append("WAREHOUSES_INCOMPLETE")
            offer.review = ",".join(flags)
            note = "OTHER WAREHOUSES NOT READ: " + ", ".join(n.upper() for n in names)
            offer.vendor_note = "\n".join(x for x in (offer.vendor_note, note) if x)

    async def _pick_warehouse(self, page, i: int, value: str, shows, *, label: str = "",
                              part: str = "") -> Optional[dict]:
        """Pick a warehouse in dropdown i and wait until its row `shows` it: the
        coded way (a click on the custom dropdown, else setting the select),
        then, when the row doesn't show it, a saved recipe or the LLM (assist.py)
        with the same check. The row as read once the page is quiet, or None."""
        if await self._click_warehouse(page, i, value) or await _evaluate(
                page, _SWITCH_WAREHOUSE_JS, [i, value]):
            now = await self._row_shows(page, i, shows)
            if now:
                return now

        async def check() -> bool:
            row = await _evaluate(page, _READ_WAREHOUSE_ROW_JS, i)
            return bool(row and shows(row))
        if label and await self.assist.act(
                page, key="proponent_warehouse_pick",
                goal=(f"show the numbers of '{label}' in the warehouse drop-down of this row"
                      + (f" (part {part})" if part else "")
                      + f": open the drop-down and choose '{label}'"),
                check=check, scope=f"[data-sourcing-whs='{i}']", closest="td",
                option_label=label):
            return await self._row_shows(page, i, shows)
        return None

    async def _row_shows(self, page, i: int, shows) -> Optional[dict]:
        deadline = time.time() + WAREHOUSE_SWITCH_SECONDS
        while True:
            await asyncio.sleep(0.3)
            now = await _evaluate(page, _READ_WAREHOUSE_ROW_JS, i)
            if now and shows(now):
                await _settle(page, timeout_ms=5_000)
                return await _evaluate(page, _READ_WAREHOUSE_ROW_JS, i) or now
            if time.time() > deadline:
                return None

    async def _click_warehouse(self, page, i: int, value: str) -> bool:
        """The custom dropdown (WAREHOUSE_CONTROL): click it open, click the
        warehouse. False when the page has no such control (the old layout)."""
        control = page.locator(WAREHOUSE_CONTROL.format(i=i)).first
        try:
            if not await control.count():
                return False
            option = control.locator(WAREHOUSE_OPTION.format(value=value)).first
            if not await option.count():
                return False
            await control.locator(WAREHOUSE_OPENER).first.click(timeout=self.action_timeout_ms)
            await option.click(timeout=self.action_timeout_ms)
            return True
        except Exception as exc:
            logger.info("Results: warehouse dropdown %d: pick by click failed (%s)", i,
                        str(exc).splitlines()[0][:200])
            return False

    async def _mark_next_page_button(self, page) -> Optional[str]:
        """Find the pager's enabled "next page" control and return a selector for it."""
        found = await _evaluate(page, _MARK_NEXT_JS)
        return '[data-sourcing-next="1"]' if found else None

    # -- logout ------------------------------------------------------------------
    async def logout(self, page) -> bool:
        self.apply_timeouts(page)
        await accept_cookies(page)
        link = await _first_visible(page, [self.selectors.get("logout_link"), *LOGOUT_SELECTORS])
        if not link:
            # The header with the Logout link may not be on the current page.
            await page.goto(self.config.get("logout_url") or HOME_URL,
                            wait_until="domcontentloaded")
            await _settle(page)
            await accept_cookies(page)
            if await self.shows_logged_out(page):
                logger.info("Logout: already signed out")
                return True
            link = await _first_visible(page, [self.selectors.get("logout_link"), *LOGOUT_SELECTORS])
        if not link:
            logger.warning("Logout: no Logout link found at %s", page.url)
            return False
        logger.info("Logout: clicking %s", link)
        await self._submit_and_wait(page, lambda: page.click(link, no_wait_after=True), "Logout")
        if await self._on_login_page(page) or await self.shows_logged_out(page):
            self.learned["logout_link"] = link
            logger.info("Logout: signed out (now at %s)", page.url)
            return True
        logger.warning("Logout: clicked %s but the page doesn't show the Login button (%s)",
                       link, page.url)
        return False


_PAGER_RE = re.compile(r"results\s+(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s+of\s+(\d[\d,]*)", re.I)


def part_results_url(url: str, part_number: str) -> str:
    """The results page address for this part. A search run from the results
    page is a postback: the page shows the new part but the address keeps the
    first search's ?id=, so `id` is set to this part number here. Analytics
    parameters (_gl=...) are dropped. An address without an `id` is kept."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    if not any(k.lower() == "id" for k, _ in query):
        return url
    query = [(k, part_number if k.lower() == "id" else v) for k, v in query]
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def _pager(text: str) -> Optional[tuple[int, int, int]]:
    """"Viewing results 1 - 10 of 24" -> (1, 10, 24); None if the page has no pager."""
    m = _PAGER_RE.search(text)
    if not m:
        return None
    return tuple(int(g.replace(",", "")) for g in m.groups())  # type: ignore[return-value]


def _page_size_choice(selects: list[dict], wanted: int):
    """From the page's <select>s, pick the "results per page" one and the option to
    use: the smallest size >= wanted, else the largest. Returns (selector, option
    value, size, current size) or None."""
    for s in selects:
        label = " ".join([s.get("id") or "", s.get("name") or "", s.get("title") or "",
                          s.get("className") or ""]
                         + [o["text"] for o in s["options"]]).lower()
        if not s.get("visible") or not s.get("enabled", True) or not re.search(
                r"per\s*page|page\s*size|pagesize|pg-selbox", label):
            continue
        sizes = []
        for o in s["options"]:
            m = re.search(r"\d+", o["text"]) or re.search(r"\d+", o["value"])
            if m:
                sizes.append((int(m.group()), o["value"], o["selected"]))
        if not sizes:
            continue
        current = next((n for n, _, sel in sizes if sel), None)
        fits = [x for x in sizes if x[0] >= wanted]
        size, value, _ = min(fits) if fits else max(sizes)
        # Marked by _LIST_SELECTS_JS: the exact element we looked at, even when
        # it has no id/name (jqGrid's pager box) or the page has several.
        selector = f'select[data-sourcing-sel="{s["index"]}"]'
        return selector, value, size, current
    return None


def _chunk_pages(pages: list[str], max_chars: int) -> list[str]:
    """Join result pages into as few LLM-sized chunks as possible. Grid records
    are split between records, each chunk keeping the grid's heading line."""
    chunks: list[str] = []
    for i, text in enumerate(pages, start=1):
        if len(text) > max_chars:
            # 25 parts gave 34 records = 85k chars (seen in logs 2026-10-14);
            # each record stands alone, so the records go in several calls.
            records = _split_records(text, max_chars)
            if records:
                chunks.extend(records)
                continue
            # A cut-off page would turn the parts past the cut into false NOT_FOUNDs.
            # This batch only: the bulk box works, so bulk stays on.
            raise BulkResultsIncomplete(
                f"Bulk results page {i} is {len(text)} chars (limit {max_chars}); "
                f"lower bulk_batch_size")
        block = text if len(pages) == 1 else f"--- results page {i} ---\n{text}"
        if chunks and len(chunks[-1]) + len(block) + 2 <= max_chars:
            chunks[-1] += "\n\n" + block
        else:
            chunks.append(block)
    return chunks


def _split_records(text: str, max_chars: int) -> Optional[list[str]]:
    """Grid records text -> chunks of whole records under max_chars, or None if
    the text isn't grid records or one record alone is too long."""
    head, *records = re.split(r"\n\n(?=Record \d+:)", text)
    if not records:
        return None
    chunks: list[str] = []
    for record in records:
        if len(head) + len(record) + 2 > max_chars:
            return None
        if chunks and len(chunks[-1]) + len(record) + 2 <= max_chars:
            chunks[-1] += "\n\n" + record
        else:
            chunks.append(f"{head}\n\n{record}")
    return chunks


def _safe(text: str) -> str:
    """A part number as a file-name piece."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:40]


def _qty(value: str) -> str:
    """Quantity for a bulk search line: a positive whole number, default 1."""
    try:
        qty = int(float(value))
    except (TypeError, ValueError):
        return "1"
    return str(qty) if qty > 0 else "1"
