"""TextronAdapter: Textron Aviation Parts & Distribution (ww2.txtav.com).

From the team's notes (2026-11-12) and the public pages seen that day (ASP.NET MVC,
jQuery; part pages open without a login but say "Log in or Register as a New User
to view pricing and availability". Signed in (onboarding 2026-11-12, local headed
browser, 2FA typed by a person): "Your Price: $42.01 / 37,43 €", "LIST PRICE: $70.01 /
62,38 € DISCOUNT: 40%", "160 IN STOCK" or "0 IN STOCK" + "Order today with estimated
availability 12/27/2026.", "Ships from Wichita"; DEBUG_DUMP_PAGES=1 saves
debug_pages/*-textron-*-read.txt):

  1 sign in   the login page is the secret's `url` (https://ww2.txtav.com/Account/Login;
              none in the code, as Monroe). Email (#Email), Password (#password-field),
              Log In (#btnSignIn). The Log In button stays disabled until Google's
              invisible reCAPTCHA v3 has scored the browser; a low score shows the
              "I'm not a robot" box instead (the page's own script) -> a person through
              Live View (MfaRequired("captcha")), never solved by the program.
  2 2FA       after Log In the portal asks for a code (team, 2026-11-12): the person
              types it in the Browserbase Live View; the run waits (hitl.py) and
              carries on once the portal's dashboard opens signed in.
  3 signed in /Dashboard fetched with the browser's cookies: 200 = signed in, a redirect
              to /Account/Login = signed out (seen signed out 2026-11-12). The page's
              hidden #logoutForm is there signed out too, so it proves nothing.
  4 one part  Parts/PartSearch/PartsDetail/<part> (team's example A-7079B-24). A part
              Textron doesn't have still opens, with "Part Not Found." (HTTP 200)
  5 bulk      Parts/PartSearch/Multiple: one box per part (6 shown, "Add Another" adds
              one, at most 20), the requested quantity in the row's quantity box when
              the form has one (team: part and qty per row; the signed-out form has
              part boxes only; the whole form is filled by one script in the
              page, 2026-11-13), Search -> PartsDetail?PageType=Multiple, one block per
              part. A part with no block there, or every part when the form can't be
              used, is opened by its PartsDetail address in its own tab, 3 at once
              (team, 2026-11-12). Vendor config `bulk_mode: "tabs"` skips the form.
  6 read      per part block: its text (title, description, unit, stock line, price)
              and the values the page carries in its own hidden fields: YourPriceUS_n /
              YourPriceEUR_n, [n].Uom, [n].MinOrder, [n].iMROCondition (NEW),
              [n].PotentialLeadTime, [n].ErrorMsg, [n].Substitutes[k] (alternates:
              "Cessna Alternate", "Beech-One-Way Supersedure"). Read by Textron's
              prompt (textron_prompt.py); then the page's own values are set on the
              line (_finish)
  7 service   each block's Service Center Stock link opens a Location / On Hand
    centers   Quantity dialog (team, 2026-11-13): its rows go into Internal Comments
              ("SERVICE CENTER STOCK: <location> QTY <n>"), read by a script in the
              page for every part at once; never qty / warehouses

Alternates are not lines of their own (default_show_substitutes off, as Wencor and
Aviall): their part numbers go into the requested part's Internal Comments
(listed_alternates). Never opened: cart, lists, quotes, orders, Remove Item,
Kit to Cart, Parts List Upload.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote, urlsplit

from ..conditions import CONDITION_CODES, normalize_condition
from ..config import captcha_solving_allowed
from ..credentials.base import VendorCredential
from ..extract import NOT_PRESENT
from ..models import RequestRow, SearchOutcome
from . import web
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, LoginBlocked, LoginFailed, MfaRequired,
    SessionExpired, check_rate_limited, has_real_offer, host_allowed,
)
from .generic import GenericAdapter
from .textron_prompt import TEXTRON_EXTRACT_SYSTEM
from .web import _first_visible, _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.textron")

SITE = "https://ww2.txtav.com"
HOME_URL = f"{SITE}/"
DETAIL_URL = SITE + "/Parts/PartSearch/PartsDetail/{part}"
MULTIPLE_URL = f"{SITE}/Parts/PartSearch/Multiple"
# Signed out it redirects to /Account/Login?ReturnUrl=%2FDashboard (2026-11-12).
ACCOUNT_URL = f"{SITE}/Dashboard"
DETAIL_PATH = "/parts/partsearch/partsdetail"

# The sign-in form (Account/Login, 2026-11-12). The page's other text boxes are the
# header's part search (#menu_partdesc) and the feedback pop-up's: pinned here, over
# any selector saved from an earlier run.
LOGIN_FORM = "form#thisLoginForm"
KNOWN_SELECTORS = {"username_input": f"{LOGIN_FORM} input#Email",
                   "password_input": f"{LOGIN_FORM} input#password-field",
                   "submit_button": "button#btnSignIn",
                   # Signed in, the header's Log Off runs the page's logout(); its
                   # wording isn't seen yet, so the onclick is what is matched.
                   "logout_link": "a[onclick*='logout()']"}
# Log In is enabled by the page's script once reCAPTCHA v3 answers (score >= 0.5);
# a lower score shows #divReCaptchaV2 (the checkbox) and keeps it disabled.
SIGN_IN_STATE_JS = """() => {
  const v2 = document.querySelector('#divReCaptchaV2');
  if (v2 && getComputedStyle(v2).display !== 'none' && v2.getBoundingClientRect().height > 0)
    return 'captcha';
  const b = document.querySelector('#btnSignIn');
  if (!b) return 'no_button';
  return b.disabled ? 'waiting' : 'ready';
}"""
SIGN_IN_WAIT_SECONDS = 25
# DEV_ALLOW_CAPTCHA_SOLVING only: Browserbase's docs say solving takes 5-30s.
SOLVER_WAIT_SECONDS = 45
# The 2FA page after Log In (team, 2026-11-12): choose Email, click Send Code, then a
# code box; the person types only the code. Its markup isn't seen yet: the usual
# forms of the choice and the button, then the LLM (assist.py), checked by the code
# box appearing. --dump-pages saves the page (debug_pages/*-textron-2fa*).
EMAIL_CHOICES = ("input[type='radio'][value*='mail' i]", "input[type='radio'][id*='mail' i]",
                 "input[type='radio'][name*='mail' i]", "label:has-text('Email') input[type='radio']",
                 "label:has-text('E-mail') input[type='radio']")
EMAIL_BUTTONS = ("label:has-text('Email')", "button:has-text('Email')", "a:has-text('Email')",
                 "[role='button']:has-text('Email')", "[role='radio']:has-text('Email')")
# Seen 2026-11-13: select#SelectedProvider (Email), button#btnSendCode, input#Code.
SEND_CODE_BUTTONS = ("button#btnSendCode", "button:has-text('Send Code')", "button:has-text('Send code')",
                     "input[type='submit'][value*='Send' i]", "input[type='button'][value*='Send' i]",
                     "a:has-text('Send Code')", "button:has-text('Send')", "a:has-text('Send')",
                     "[role='button']:has-text('Send')")
CODE_BOXES = ("form#frmVerifyCode input#Code", *web.MFA_INPUT_SELECTORS, "input[name*='token' i]:not([type=hidden])",
              "input[type='tel']", "input[type='number']")
CODE_BOX_WAIT_SECONDS = 30
# The 2FA pages (2026-11-13): /Account/SendCode (#SelectedProvider: Email, Send Code)
# and /Account/VerifyCode (#Code, Verify Code, "I didn't receive the code. Take me to
# the previous step." -> back to SendCode). Back on SendCode this long without a new
# code, or back on the login form, the sign-in has ended: the Live View wait stops.
SEND_CODE_IDLE_SECONDS = 180
# The site's own logout(): submits the hidden #logoutForm (POST /Account/LogOff with
# its anti-forgery token), the same as the header's Log Off.
LOGOUT_JS = """() => { const f = document.getElementById('logoutForm');
  if (!f) return false; f.submit(); return true; }"""

# Multiple Part Search (2026-11-12): <form action="/Parts/PartSearch/Multiple"
# method="post"> with #SearchRows .form-group > input[name=AddParts]. The feedback
# pop-up's form posts to the same address, so the form is the one holding #SearchRows.
MULTIPLE_FORM = "form:has(#SearchRows)"
PART_BOXES = "#SearchRows input[name='AddParts']"
ROWS = "#SearchRows .form-group"
SEARCH_BUTTON = f"{MULTIPLE_FORM} button[type='submit']"
# The quantity box of a row, when the form has one (signed in; not seen signed out).
QTY_IN_ROW = ("input[name*='qty' i]:not([type=hidden]), input[id*='qty' i]:not([type=hidden]), "
              "input[name*='quant' i]:not([type=hidden]), input[placeholder*='qty' i], "
              "input[placeholder*='quant' i]")
MAX_PARTS = 20
TABS_PARALLEL = 3
# The whole form filled by one script in the page (2026-11-13, team, from the run 27
# recording): one Playwright call per "Add Another" click and per box took 1m51s for
# 19 parts on Browserbase. The page's own Add Another is clicked (its AddPartNoRow
# adds the row), then every box set with input/change events, as typing would.
FILL_FORM_JS = """async ({parts, qtys, qtySel, custom}) => {
  const rowsBox = document.querySelector('#SearchRows');
  if (!rowsBox) return {error: 'no #SearchRows'};
  const form = rowsBox.closest('form') || document;
  const boxes = () => [...rowsBox.querySelectorAll('input[name="AddParts"]')];
  const add = [...document.querySelectorAll('a, button')]
    .find(a => /^\\s*add another\\s*$/i.test(a.textContent || ''));
  while (boxes().length < parts.length) {
    const before = boxes().length;
    if (!add) return {error: "no 'Add Another'", boxes: before};
    add.click();
    for (let i = 0; i < 20 && boxes().length <= before; i++) await new Promise(r => setTimeout(r, 50));
    if (boxes().length <= before) return {error: "'Add Another' added no part box", boxes: before};
  }
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const set = (el, v) => { setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true})); };
  const all = boxes();
  const anyQty = custom ? [...document.querySelectorAll(custom)] : [...form.querySelectorAll(qtySel)];
  const typed = {}; let noQty = 0;
  all.forEach((box, i) => {
    set(box, i < parts.length ? parts[i] : '');
    if (i >= parts.length || !qtys[i]) return;
    const row = box.closest('.form-group') || box.parentElement;
    const q = (!custom && row.querySelector(qtySel)) || anyQty[i];
    if (!q) { noQty++; return; }
    set(q, qtys[i]); typed[i] = qtys[i];
  });
  const inputs = noQty ? [...rowsBox.querySelectorAll('input, select')].slice(0, 6)
    .map(i => [i.tagName, i.type, i.name, i.id, i.placeholder].join('|')) : [];
  return {boxes: all.length, values: all.map(b => b.value), typed, noQty, inputs};
}"""

# Service Center Stock (team, 2026-11-13): the link under "Ships from ..." opens a
# dialog with a Location / On Hand Quantity table (stock at Textron's service centers);
# its rows go into the line's Internal Comments. Its markup isn't seen yet: the link
# is found by its exact wording in the part's block ("No Service Center Stock
# Available" has no link), clicked by a script in the page, the dialog read once its
# text stops changing, then closed. A link with a real address is fetched instead
# (nothing clicked). One script for every part: a round trip per part is slow on
# Browserbase. Vendor config `service_center_stock: false` turns it off.
SERVICE_CENTER_LINE = re.compile(r"^\s*Service Center Stock\s*$", re.I | re.M)
SERVICE_CENTER_PART_SECONDS = 8
SERVICE_CENTER_SECONDS = 120         # for every part of one page together
SERVICE_CENTER_ROWS_MAX = 10
SERVICE_CENTER_JS = """async ({ns, perPart, budget, maxRows}) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
  const visible = el => { if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0; };
  const DIALOGS = '.modal, [role="dialog"], .ui-dialog, .popover, .k-window';
  const shown = () => [...document.querySelectorAll(DIALOGS)]
    .filter(d => visible(d) && /on hand|location|service center/i.test(d.innerText || ''));
  const rowsOf = root => [...root.querySelectorAll('tr')]
    .map(tr => [...tr.querySelectorAll('td')].map(td => clean(td.textContent)))
    .filter(cells => cells.some(c => c)).slice(0, maxRows);
  const wording = e => clean(e.textContent).toLowerCase() === 'service center stock';
  const start = Date.now(), out = {};
  for (const n of ns) {
    if (Date.now() - start > budget) { out[n] = {error: 'time limit for the page reached'}; continue; }
    const scopes = [`[data-sai-side="${n}"]`, `[data-sai-left="${n}"]`]
      .map(s => document.querySelector(s)).filter(Boolean);
    let link = null;
    for (const sel of ['a, button, [onclick], [data-toggle], [data-bs-toggle]', 'span, div, li, p']) {
      for (const s of scopes) { link = [...s.querySelectorAll(sel)].find(wording); if (link) break; }
      if (link) break;
    }
    if (!link) { out[n] = {error: 'no Service Center Stock link'}; continue; }
    const info = {link: link.outerHTML.slice(0, 400)};
    const href = link.tagName === 'A' ? (link.getAttribute('href') || '') : '';
    if (href && !href.startsWith('#') && !/^javascript:/i.test(href)) {
      try {
        const r = await fetch(href, {credentials: 'include'});
        const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
        info.rows = rowsOf(doc); info.text = clean(doc.body && doc.body.textContent).slice(0, 1500);
        info.how = 'fetched ' + href;
      } catch (e) { info.error = 'fetch failed: ' + e; }
      out[n] = info; continue;
    }
    link.click();
    const t0 = Date.now(); let dlg = null, last = null, same = 0;
    while (Date.now() - t0 < perPart) {
      await sleep(250);
      const d = shown().pop();
      if (!d) continue;
      const txt = clean(d.innerText);
      if (/loading|please wait/i.test(txt)) { same = 0; last = txt; continue; }
      if (txt === last) same++; else { same = 0; last = txt; }
      if (same >= 2 && Date.now() - t0 >= 700) { dlg = d; break; }
    }
    if (!dlg) {
      dlg = shown().pop();
      if (!dlg) { info.error = 'no dialog opened'; out[n] = info; continue; }
      info.unsettled = true;
    }
    info.rows = rowsOf(dlg); info.text = clean(dlg.innerText).slice(0, 1500);
    info.dialog = dlg.outerHTML.slice(0, 6000); info.how = 'dialog';
    const closer = [...dlg.querySelectorAll('[data-dismiss], [data-bs-dismiss], .close, .btn-close, button')]
      .find(b => visible(b) && (b.matches('[data-dismiss], [data-bs-dismiss], .close, .btn-close')
                                || /^(×|x|close)$/i.test(clean(b.textContent))));
    if (closer) closer.click();
    else document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, bubbles: true}));
    for (let i = 0; i < 14 && visible(dlg); i++) await sleep(150);
    if (visible(dlg)) {
      // Left open, it would be read again for the next part: hidden on this page only.
      info.close_failed = true; dlg.style.display = 'none';
      document.querySelectorAll('.modal-backdrop').forEach(b => b.remove());
      document.body.classList.remove('modal-open');
    }
    out[n] = info;
  }
  return out;
}"""
_SKIP_RESOURCES = ("image", "font", "media")

# One block per part on PartsDetail (single and multiple): the left column holds the
# hidden fields ([n].Uom ...) and the details, the next column the stock line, the
# price (signed in) and the alternates.
BLOCKS_JS = """() => {
  const clean = t => (t || '').replace(/[ \\t\\u00a0]+/g, ' ').replace(/ *\\n */g, '\\n')
                               .replace(/\\n{2,}/g, '\\n').trim();
  const val = id => ((document.getElementById(id) || {}).value || '').trim();
  return [...document.querySelectorAll('input[id^="ProductPartNumber_"]')].map(inp => {
    const n = inp.id.slice('ProductPartNumber_'.length);
    const prefix = '[' + n + '].';
    const data = {}, subs = {};
    document.querySelectorAll('input[name^="' + prefix + '"]').forEach(i => {
      const key = i.name.slice(prefix.length);
      const m = key.match(/^Substitutes\\[(\\d+)\\]\\.(\\w+)$/);
      if (m) (subs[m[1]] = subs[m[1]] || {})[m[2]] = (i.value || '').trim();
      else if (!key.includes('[')) data[key] = (i.value || '').trim();
    });
    const left = inp.closest('[class*="col-sm-8"]') || inp.parentElement;
    let side = left.nextElementSibling;
    while (side && !/(^|\\s)col-/.test(side.className || '')) side = side.nextElementSibling;
    // Marked for the Service Center Stock read (SERVICE_CENTER_JS).
    left.setAttribute('data-sai-left', n);
    if (side) side.setAttribute('data-sai-side', n);
    return {n, pn: (inp.value || '').trim(), data, subs: Object.values(subs),
            price_us: val('YourPriceUS_' + n), price_eur: val('YourPriceEUR_' + n),
            description: clean((document.getElementById('partDescription_' + n) || {}).innerText),
            main: clean(left.innerText), side: side ? clean(side.innerText) : ''};
  });
}"""
# A part block signed out (2026-11-12): "Want to see more? Log in or Register as a
# New User to view pricing and availability."
SIGNED_OUT_TEXT = "to view pricing and availability"
_NOT_FOUND = re.compile(r"part not found", re.I)
# The signed-in block's stock and shipping lines (onboarding 2026-11-12: "160 IN
# STOCK" / "0 IN STOCK" + "Order today with estimated availability 12/27/2026.",
# "Ships from Wichita").
_STOCK = re.compile(r"^\s*([\d,]+)\s+IN STOCK\s*$", re.I | re.M)
# NOTES wordings that change what is bought (onboarding 2026-11-12), copied to
# Internal Comments by the program (the extraction schema has no note field).
_PURCHASE_NOTES = ("SOLD BY THE POUND", "PRICE VALID UNTIL STOCK DEPLETED")
_SHIPS_FROM = re.compile(r"^\s*Ships from (.+?)\s*$", re.I | re.M)
# More block wordings from the 19-part run (2026-11-12): read by the program.
_MULTIPLE_OF = re.compile(r"Must be a Multiple of:\s*([\d,]+)", re.I)
_SHELF_LIFE = re.compile(r"Maximum Shelf Life:\s*([^\n]+)", re.I)
_ESTIMATE = re.compile(r"estimated availability (\d{1,2}/\d{1,2}/\d{4})", re.I)
# A part Textron no longer sells, with its replacement listed below it
# (M83461-1-122, C3317-230, C352TS: "NO LONGER AVAILABLE. PLEASE SEE OPTIONS BELOW.").
_DISCONTINUED = re.compile(r"NO LONGER AVAILABLE\.?(?:\s*PLEASE SEE OPTIONS BELOW\.?)?", re.I)
# Price 0 in the page's data + this line = no price shown (101-320234-3).
_CONTACT_PRICING = "PLEASE CONTACT US FOR ASSISTANCE WITH PRICING"
# Line notes the program copies to Internal Comments: (wording on the page, note).
_LINE_NOTES = (("HAZARDOUS MATERIAL", "HAZARDOUS MATERIAL"),
               ("EXTENDED INACTIVITY", "EXTENDED INACTIVITY: QUOTE MAY TAKE LONGER"))
# The site's error page, HTTP 404 (2026-11-12): what it answered a headless local
# Chromium on every address that a normal browser opens. Never read as "part not
# found": that is a 200 page with the part's block and "Part Not Found.".
REFUSED_TEXT = "this page could not be found"
REFUSED = ("Textron Aviation answered {url} with its 'Page Could Not Be Found' page (HTTP "
           "{status}), as it did for a headless browser on 2026-11-12: the site is refusing "
           "this browser. Nothing was read; a person should look (try the Browserbase browser)")
# Noise inside a block: the aircraft list and the OEM sentence on every part.
_AIRCRAFT = re.compile(r"Applicable Aircraft.*?List not comprehensive, may be applicable on "
                       r"other models\.?", re.S)
_BOILERPLATE = ("Each delivered OEM part will meet the original manufacturer’s specification or, "
                "if the part is not an OEM part, it will meet or exceed the original "
                "manufacturer’s specification.")
# "Service Center Stock" is the dialog link the program reads itself (_read_service_center).
_SIDE_DROP = re.compile(r"^(Remove Item|Add to List|Want to see more\?|Service Center Stock)$", re.I | re.M)
BLOCK_TEXT_MAX = 4000
SUBSTITUTES_MAX = 5
# Textron's iMROCondition is NEW on what was seen; an empty one gets this code
# (vendor config `condition_not_shown`, "" leaves it empty), as Monroe / Spruce.
CONDITION_NOT_SHOWN = "NE"

HEADER = "--- Part {n}: {link} ({how}) ---"
FACTS_HEADER = "--- What the program read for part {n} (the page's own data fields) ---"
TEXT_HEADER = "--- Page text of part {n} ---"
NOTHING = "--- Textron Aviation: {part} not found (\"{message}\" on its part page) ---"


def detail_url(part_number: str) -> str:
    return DETAIL_URL.format(part=quote(" ".join(part_number.split()), safe="-._"))


def _key(value: str) -> str:
    """A part number without spaces, dashes, slashes or dots, upper case."""
    return re.sub(r"[\s\-/.]", "", (value or "").upper())


def _clean_url(url: str) -> str:
    return (url or "").split("#")[0].split("?")[0]


def requested_qty(qty_needed: str) -> Optional[str]:
    """The row's quantity as typed into a quantity box ("5", "10 EA" -> "10"); None if none."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", qty_needed or "")
    if not match:
        return None
    value = float(match.group(0).replace(",", ""))
    return f"{value:g}" if value > 0 else None


def _number(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def block_facts(block: dict) -> dict:
    """What one part block states in the page's own hidden fields."""
    data = block.get("data") or {}
    min_order = _number(data.get("MinOrder"))
    error = (data.get("ErrorMsg") or "").strip()
    whole = (block.get("main") or "") + "\n" + (block.get("side") or "")
    flat = " ".join(whole.upper().split())
    multiple = _MULTIPLE_OF.search(whole)
    shelf = _SHELF_LIFE.search(whole)
    discontinued = _DISCONTINUED.search(whole)
    estimate = (_ESTIMATE.search(whole) or _ESTIMATE.search(data.get("PotentialLeadTime") or ""))
    notes = [n for n in _PURCHASE_NOTES if n in flat]
    if multiple:
        notes.append(f"SOLD IN MULTIPLES OF {multiple.group(1).replace(',', '')}")
    if shelf:
        notes.append(f"MAX SHELF LIFE {shelf.group(1).strip().upper()}")
    notes += [note for wording, note in _LINE_NOTES if wording in flat]
    if block.get("service_center"):
        notes.append("SERVICE CENTER STOCK: " + "; ".join(block["service_center"]))
    return {
        "pn": block.get("pn") or "",
        "description": block.get("description") or "",
        "uom": (data.get("Uom") or "").strip(),
        "moq": f"{min_order:g}" if min_order and min_order > 1 else None,
        "condition": (data.get("iMROCondition") or data.get("Condition") or "").strip(),
        "lead_note": (data.get("PotentialLeadTime") or "").strip(),
        # 0 in the price fields = no price shown (2026-11-12: "PLEASE CONTACT US FOR
        # ASSISTANCE WITH PRICING", and EUR 0 when only a dollar price is shown).
        "price_us": _price(block.get("price_us")),
        "price_eur": _price(block.get("price_eur")),
        "contact_pricing": _CONTACT_PRICING in flat,
        "discontinued": " ".join(discontinued.group(0).upper().split()) if discontinued else "",
        "estimate": estimate.group(1) if estimate else "",
        "error": error,
        "not_found": bool(_NOT_FOUND.search(error)),
        "alternates": [s for s in block.get("subs") or [] if s.get("PartNumber")],
        # The stock lines sit just after NOTES in the read text (2026-11-12): looked
        # for in the whole block, the right-hand column first.
        "stock": _stock(block.get("side") or "") or _stock(block.get("main") or ""),
        "ships_from": (_SHIPS_FROM.search(block.get("side") or "")
                       or _SHIPS_FROM.search(block.get("main") or "") or [None, ""])[1],
        "notes": notes,
    }


def _price(value) -> str:
    """A price field's value; "" for none or 0."""
    number = _number(value)
    return "" if number is None or number == 0 else str(value).strip()


def _stock(side: str) -> Optional[str]:
    """The block's "<n> IN STOCK" as a number ("160"); None when not shown."""
    match = _STOCK.search(side)
    return match.group(1).replace(",", "") if match else None


def service_center_rows(rows: list[list[str]]) -> list[str]:
    """The dialog's table rows as "WICHITA, KS QTY 4"; a row with 0 on hand is left out."""
    out = []
    for cells in rows or []:
        cells = [c for c in cells if c]
        if not cells:
            continue
        qty = _number(cells[-1]) if len(cells) > 1 else None
        if qty is not None:
            if qty <= 0:
                continue
            out.append(f"{' '.join(cells[:-1]).upper()} QTY {qty:g}")
        else:
            out.append(" ".join(cells).upper())
    return list(dict.fromkeys(out))


def block_section(block: dict) -> str:
    """The block's own text without the aircraft list, the OEM sentence and its
    buttons (Remove Item, Add to List)."""
    main = _AIRCRAFT.sub("", block.get("main") or "").replace(_BOILERPLATE, "")
    side = _SIDE_DROP.sub("", block.get("side") or "")
    text = "\n".join(t.strip() for t in (main, side) if t.strip())
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:BLOCK_TEXT_MAX].strip()


def _fact_lines(facts: dict, requested: str, qty_typed: Optional[str]) -> list[str]:
    lines = [f"Part number: {facts['pn']}",
             f"Is the requested part {requested}: "
             + ("yes" if _key(facts["pn"]) == _key(requested) else "no (another part number)")]
    if facts["description"]:
        lines.append(f"Description: {facts['description']}")
    if facts["uom"]:
        lines.append(f"Unit of measure: {facts['uom']}")
    if facts["moq"]:
        lines.append(f"Minimum order: {facts['moq']}")
    if facts["condition"]:
        lines.append(f"Condition: {facts['condition']}")
    if facts["price_us"] or facts["price_eur"]:
        prices = [f"USD {facts['price_us']}" if facts["price_us"] else "",
                  f"EUR {facts['price_eur']}" if facts["price_eur"] else ""]
        lines.append("Your price in the page's data: " + " / ".join(p for p in prices if p))
    else:
        lines.append("Your price in the page's data: none"
                     + (" (PLEASE CONTACT US FOR ASSISTANCE WITH PRICING)"
                        if facts["contact_pricing"] else ""))
    if facts["stock"] is not None:
        lines.append(f"Stock shown: {facts['stock']} IN STOCK")
    if facts["ships_from"]:
        lines.append(f"Ships from: {facts['ships_from']}")
    if facts["lead_note"]:
        lines.append(f"Availability note in the page's data: {facts['lead_note']}")
    if facts["error"]:
        lines.append(f"Message on the part: {facts['error']}")
    if facts["discontinued"]:
        lines.append(f"Message on the part: {facts['discontinued']}")
    for note in facts["notes"]:
        lines.append(f"Note: {note}")
    if qty_typed:
        lines.append(f"Quantity typed into the Multiple Part Search: {qty_typed}")
    for alt in facts["alternates"]:
        words = " ".join(v for v in (alt.get("SubDesc"), f"({alt['Substitle']})"
                                     if alt.get("Substitle") else "") if v)
        lines.append(f"Alternate listed: {alt['PartNumber']}" + (f" {words}" if words else ""))
    return lines


class TextronAdapter(GenericAdapter):
    vendor_id = "textron_aviation"
    display_name = "Textron Aviation"
    allowed_hosts = ("txtav.com",)
    # No built-in login address: the login page is the `url` in the vendor's secret
    # (sourcing-vendor-textron-aviation), as for Monroe and Aircraft Spruce.
    default_login_url = ""
    extract_system = TEXTRON_EXTRACT_SYSTEM
    max_parts_per_search = MAX_PARTS
    default_bulk_batch_size = MAX_PARTS
    default_bulk_min_parts = 2
    # Alternates go to Internal Comments, not lines (as Wencor / Aviall, team 2026-11-04).
    default_show_substitutes = False
    # The sign-in runs Google's invisible reCAPTCHA v3: fields typed key by key, as AAR.
    default_human_typing = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.selectors.update(KNOWN_SELECTORS)
        self.bulk_unavailable: Optional[str] = None
        self.form_unavailable: Optional[str] = None
        self._product_text: Optional[str] = None
        # Per part link: what its block states (block_facts), set on every offer the
        # LLM returns for it (_finish).
        self._facts: dict[str, dict] = {}
        # Requested part (key) -> alternates' part numbers (listed_alternates).
        self._alternates: dict[str, list[str]] = {}
        self._code_requested = False
        self._code_page_seen = False
        self._send_code_since: Optional[float] = None
        self._last_account_state: Optional[bool] = "unset"   # type: ignore[assignment]

    # -- settings -----------------------------------------------------------------
    @property
    def probe_url(self) -> str:
        return self.config.get("probe_url") or ACCOUNT_URL

    @property
    def bulk_mode(self) -> str:
        """"form" (Multiple Part Search, tabs for what it misses) or "tabs" (each
        part's address in its own tab)."""
        mode = str(self.config.get("bulk_mode") or "form").strip().lower()
        return mode if mode in ("form", "tabs") else "form"

    @property
    def tabs_parallel(self) -> int:
        return max(1, int(self.config.get("tabs_parallel") or TABS_PARALLEL))

    # -- signed in / out ------------------------------------------------------------
    async def _account_state(self, page) -> Optional[bool]:
        """True signed in, False signed out, None can't tell. A request with the
        browser's cookies, so the open page (or a person's Live View) is left alone."""
        try:
            resp = await page.request.get(ACCOUNT_URL, max_redirects=0,
                                          timeout=self.action_timeout_ms)
        except Exception as exc:
            logger.info("Textron account check failed: %r", exc)
            return None
        location = resp.headers.get("location", "")
        redirect = resp.status in (301, 302, 303, 307, 308)
        if redirect and "/account/login" in location.lower():
            state = False
        elif resp.status == 200:
            state = True
        elif redirect and (location.startswith("/") or host_allowed(location, self.hosts)):
            # Signed in, a portal may send the dashboard on to a page of its own (a
            # terms page, CheckEULA); only the login page means signed out (Monroe
            # lesson, 2026-11-04). Logged so the first run shows where it went.
            state = True
        else:
            state = None
        # Logged when the answer changes: the Live View wait asks every few seconds
        # (2026-11-13: the same "signed out" line every 5s for minutes).
        level = logging.INFO if state != self._last_account_state else logging.DEBUG
        self._last_account_state = state
        logger.log(level, "Textron account check: %s%s -> %s", resp.status,
                   f" to {location}" if location else "",
                   {True: "signed in", False: "signed out", None: "can't tell"}[state])
        return state

    def _account_proof(self) -> None:
        self.signed_in_proof = f"account page {ACCOUNT_URL}"
        self.login_claimed = True
        self._login_ok = True

    async def is_authenticated(self, page) -> bool:
        state = await self._account_state(page)
        if state is not None:
            return state
        return await super().is_authenticated(page)

    async def _open_login_page(self, page, cred) -> bool:
        """Already signed in (the saved browser context): no login page."""
        if await self._account_state(page) is True:
            logger.info("Login: Textron dashboard opens; already signed in")
            self._account_proof()
            return False
        try:
            return await super()._open_login_page(page, cred)
        except LoginFailed:
            await self._stop_if_refused(page, None)
            raise

    async def _stop_if_refused(self, page, response) -> None:
        """LoginBlocked when the site answered with its error page instead of the page
        asked for (see REFUSED_TEXT)."""
        status = response.status if response is not None else 0
        if status not in (0, 403, 404) or not host_allowed(page.url, self.hosts):
            return
        try:
            text = (await page.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            return
        if REFUSED_TEXT in text and not await page.locator("input[id^='ProductPartNumber_']").count():
            logger.warning("Textron: %s answered with its error page (HTTP %s)", page.url,
                           status or "?")
            raise LoginBlocked(REFUSED.format(url=page.url.split("?")[0], status=status or "?"))

    # -- sign in --------------------------------------------------------------------
    async def _login(self, page, cred: VendorCredential) -> None:
        self._code_requested = False      # one "Send Code" per login: each sends an email
        self._code_page_seen = False      # the code step was reached (VerifyCode)
        self._send_code_since: Optional[float] = None
        if not await self._open_login_page(page, cred):
            return
        if not host_allowed(page.url, self.hosts):
            raise LoginBlocked(f"Refusing to enter credentials on {page.url}")
        user = await self._pick(page, "username_input", web.USERNAME_SELECTORS)
        password = await self._password_field(page)
        if not user or not password:
            raise LoginFailed(f"Textron sign-in fields not found at {page.url} (email: {user}, "
                              f"password: {password})", credentials_sent=False)
        if cred.passwordless:
            raise self._passwordless_error(page)
        await self._type_login_field(page, user, cred.username)
        await self._type_login_field(page, password, cred.password)
        state = await self._sign_in_state(page)
        if state == "captcha" and not await self._solver_cleared(page):
            logger.warning("Login: Textron shows the 'I'm not a robot' box; asking a person")
            raise MfaRequired("captcha", "Textron Aviation shows Google's 'I'm not a robot' "
                                         "check: a person must tick it and log in")
        if state in ("waiting", "no_button"):
            # Nothing was sent: the button never became usable.
            raise LoginFailed(f"Textron's Log In button stayed disabled for "
                              f"{SIGN_IN_WAIT_SECONDS}s (reCAPTCHA check didn't answer) at "
                              f"{page.url}", credentials_sent=False)
        submit = KNOWN_SELECTORS["submit_button"]
        logger.info("Login: submitting with %s", submit)
        await self._submit_and_wait(page, lambda: page.click(submit, no_wait_after=True),
                                    "Login submit")
        await _settle(page)
        await self._after_submit(page, cred, totp_tried=False)

    async def _sign_in_state(self, page) -> str:
        """"ready" (Log In enabled), "captcha" (the checkbox shown), "waiting" /
        "no_button" after SIGN_IN_WAIT_SECONDS."""
        deadline = time.time() + SIGN_IN_WAIT_SECONDS
        state = "waiting"
        while True:
            try:
                state = await page.evaluate(SIGN_IN_STATE_JS)
            except Exception as exc:
                logger.debug("Sign-in state not read: %r", exc)
            if state in ("ready", "captcha") or time.time() >= deadline:
                logger.info("Login: Log In button state %s", state)
                return state
            await page.wait_for_timeout(500)

    async def _solver_cleared(self, page) -> bool:
        """REMOVE-AFTER-CAPTCHA-TRIAL (2026-11-05): with DEV_ALLOW_CAPTCHA_SOLVING=1 the
        browser service may tick the box. True once Log In is enabled."""
        if not captcha_solving_allowed():
            return False
        logger.info("Login: checkbox shown; waiting up to %ss for the CAPTCHA solver",
                    SOLVER_WAIT_SECONDS)
        deadline = time.time() + SOLVER_WAIT_SECONDS
        while time.time() < deadline:
            await page.wait_for_timeout(1000)
            try:
                if await page.evaluate("() => { const b = document.querySelector('#btnSignIn');"
                                       " return !!b && !b.disabled; }"):
                    return True
            except Exception:
                return False
        return False

    async def _after_submit(self, page, cred, *, totp_tried: bool,
                            code_given: bool = False) -> None:
        """The generic checks (lockout, CAPTCHA, wrong password, 2FA) decide, except
        that a page without a sign-in or code box and a dashboard that opens is
        signed in (the page after login is one the rules don't know)."""
        text = await self._stop_if_blocked(page)
        if (not await self._password_field(page) and not await self._mfa_input(page)
                and not any(h in text for h in web.MFA_TEXT_HINTS)
                and await self._account_state(page) is True):
            logger.info("Login: signed in (Textron dashboard opens; now at %s)", page.url)
            self._account_proof()
            return
        await super()._after_submit(page, cred, totp_tried=totp_tried, code_given=code_given)

    async def _mfa(self, page, cred, mfa_sel, text, totp_tried, code_given=False):
        """The 2FA page: Email chosen and Send Code clicked by the program (team,
        2026-11-12), then the generic step hands the code box to a person."""
        if not code_given and not totp_tried and not self._code_requested:
            self._code_requested = True
            mfa_sel = await self._request_code(page) or mfa_sel
            text = await self._login_text(page)
        await super()._mfa(page, cred, mfa_sel, text, totp_tried, code_given)

    async def _request_code(self, page) -> Optional[str]:
        """Choose Email and click Send Code; the code box selector once it shows,
        None when it didn't (the person then does it in Live View)."""
        if _dumping():
            await _save_page(page, "2fa-choice")
        if not host_allowed(page.url, self.hosts):
            return None
        box = await _first_visible(page, list(CODE_BOXES))
        send = await web.first_visible_button(page, list(SEND_CODE_BUTTONS))
        if box and not send:
            logger.info("Login 2FA: the code box is already shown (%s)", box)
            return box
        chose = await self._choose_email(page)
        send = await web.first_visible_button(page, list(SEND_CODE_BUTTONS))
        if send:
            logger.info("Login 2FA: email chosen (%s); clicking %s", chose or "default", send)
            await self._submit_and_wait(page, lambda: page.click(send, no_wait_after=True),
                                        "Send code")
            await _settle(page)
            box = await self._wait_code_box(page)
        if not box:
            # The coded way found no Send button / no code box: the LLM picks the
            # clicks, the code box appearing is the check (assist.py).
            async def code_box_shown() -> bool:
                return bool(await _first_visible(page, list(CODE_BOXES)))
            if await self.assist.act(page, key="textron_send_code", check=code_box_shown,
                                     goal="choose to receive the login code by Email, then "
                                          "click the button that sends the code (Send Code)",
                                     max_steps=3):
                box = await _first_visible(page, list(CODE_BOXES))
        if _dumping():
            await _save_page(page, "2fa-code")
        if box:
            self._code_page_seen = True
            logger.info("Login 2FA: code sent by email; code box %s", box)
        else:
            logger.warning("Login 2FA: Send Code not done by the program (no code box shown); "
                           "the person does it in Live View")
        return box

    async def _choose_email(self, page) -> Optional[str]:
        for selector in EMAIL_CHOICES:
            try:
                radio = page.locator(selector).first
                if await radio.count():
                    await radio.check(force=True)
                    return selector
            except Exception as exc:
                logger.debug("Email choice %s: %r", selector, exc)
        try:                              # a drop-down with an Email entry
            for select in await page.locator("select:visible").all():
                labels = [o.strip() for o in await select.locator("option").all_inner_texts()]
                pick = next((o for o in labels if "mail" in o.lower()), None)
                if pick:
                    await select.select_option(label=pick)
                    return f"select option {pick!r}"
        except Exception as exc:
            logger.debug("Email drop-down: %r", exc)
        choice = await _first_visible(page, list(EMAIL_BUTTONS))
        if choice:
            try:
                await page.click(choice)
                await page.wait_for_timeout(500)
                return choice
            except Exception as exc:
                logger.debug("Email choice %s: %r", choice, exc)
        return None

    async def _wait_code_box(self, page) -> Optional[str]:
        deadline = time.time() + CODE_BOX_WAIT_SECONDS
        while time.time() < deadline:
            box = await _first_visible(page, list(CODE_BOXES))
            if box:
                return box
            await page.wait_for_timeout(500)
        return None

    async def person_finished_login(self, page) -> bool:
        """While a person signs in (and types the 2FA code) in Live View: no sign-in
        or code box on their page, and the dashboard opens with the session's
        cookies. The person's page is never navigated."""
        try:
            if page.is_closed() or not host_allowed(page.url, self.hosts):
                return False
            await self._stop_if_blocked(page)
            self._sign_in_ended(page.url, bool(await self._password_field(page)))
            if await self._mfa_input(page) or await self._password_field(page):
                return False
            if "/account/sendcode" in urlsplit(page.url).path.lower():
                return False              # choosing / resending the code: not signed in yet
        except (LoginBlocked, MfaRequired):
            raise
        except Exception as exc:          # mid-navigation, or a CAPTCHA line: look again
            logger.debug("Live View check: %r", exc)
            return False
        if await self._account_state(page) is not True:
            return False
        logger.info("Login: the person finished signing in (now at %s)", page.url)
        self._account_proof()
        return True

    def _sign_in_ended(self, url: str, login_form: bool) -> None:
        """Raises MfaRequired when the 2FA sign-in has ended without signing in, so
        the Live View wait stops (hitl.watch_live_view) and the run closes the browser
        (2026-11-13: the page went back to Send Code and the run waited out its 10
        minutes). Only once the code step was reached: a person ticking "I'm not a
        robot" on the login form is still signing in."""
        path = urlsplit(url).path.lower()
        if "/account/verifycode" in path:
            self._code_page_seen = True
            self._send_code_since = None
            return
        if not self._code_page_seen:
            return
        if login_form and "/account/login" in path:
            raise MfaRequired("email", "Textron went back to its sign-in page after the code step")
        if "/account/sendcode" in path:
            now = time.time()
            if self._send_code_since is None:
                self._send_code_since = now
            elif now - self._send_code_since > SEND_CODE_IDLE_SECONDS:
                raise MfaRequired("email", f"Textron stayed on Send Code for over "
                                           f"{SEND_CODE_IDLE_SECONDS // 60} min after the code step")

    # -- logout ---------------------------------------------------------------------
    async def logout(self, page) -> bool:
        """The header's Log Off, else the page's own logout form; the dashboard
        check confirms it."""
        try:
            if not host_allowed(page.url, self.hosts):
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await _settle(page)
            link = await _first_visible(page, [KNOWN_SELECTORS["logout_link"],
                                               *web.LOGOUT_SELECTORS])
            if link:
                logger.info("Logout: clicking %s", link)
                await self._submit_and_wait(page, lambda: page.click(link, no_wait_after=True),
                                            "Logout")
            else:
                logger.info("Logout: no Log Off link shown; submitting the page's logout form")
                await self._submit_and_wait(page, lambda: page.evaluate(LOGOUT_JS), "Logout")
            await _settle(page)
        except Exception as exc:
            logger.warning("Logout: %r", exc)
        state = await self._account_state(page)
        if state is False:
            logger.info("Logout: signed out (now at %s)", page.url)
            return True
        logger.warning("Logout: not confirmed (account check: %s)", state)
        return False

    async def person_finished_logout(self, page) -> bool:
        return await self._account_state(page) is False

    # -- reading part pages ------------------------------------------------------------
    async def _blocks(self, page) -> list[dict]:
        try:
            return await asyncio.wait_for(page.evaluate(BLOCKS_JS), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            logger.warning("Textron: part blocks not read at %s: %r", page.url, exc)
            return []

    async def _check_signed_in(self, page, blocks: list[dict]) -> None:
        """Part pages open signed out too, without prices: never read as results."""
        if any(SIGNED_OUT_TEXT in (b.get("side") or "").lower() for b in blocks):
            raise SessionExpired(f"Signed out on a Textron part page ({page.url})")
        if await self._password_field(page):
            raise SessionExpired(f"Textron sign-in form shown at {page.url}")
        # A "Part Not Found." block has no sign-in line (seen signed out 2026-11-12):
        # a page of misses only is a NOT_FOUND only once the dashboard proves the
        # session is still signed in.
        if blocks and all(block_facts(b)["not_found"] for b in blocks) \
                and await self._account_state(page) is False:
            raise SessionExpired(f"Signed out (dashboard check) on a Textron page of misses "
                                 f"({page.url})")

    async def _open_detail(self, page, part: str) -> list[dict]:
        """A part's PartsDetail page on `page`: its blocks (one, normally)."""
        url = detail_url(part)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if not host_allowed(page.url, self.hosts):
            raise SessionExpired(f"Textron part page went to {page.url}")
        await self._stop_if_refused(page, response)
        if DETAIL_PATH not in urlsplit(page.url).path.lower():
            # Redirected (the sign-in page, an error page): read as signed out when it
            # is the login page, else nothing for this part.
            if "/account/login" in page.url.lower():
                raise SessionExpired(f"Textron sent {url} to the sign-in page")
            logger.warning("Textron: %s went to %s (HTTP %s)", url, page.url,
                           response.status if response else "?")
            return []
        blocks = await self._blocks(page)
        await self._check_signed_in(page, blocks)
        await self._read_service_center(page, blocks)
        for block in blocks:
            block["link"] = url
            block["how"] = f"opened by its address for {part}"
            block["for"] = part
        return blocks

    async def _detail_in_tab(self, page, part: str) -> list[dict]:
        tab = None
        try:
            tab = await page.context.new_page()

            async def skip(route):
                if route.request.resource_type in _SKIP_RESOURCES:
                    await route.abort()
                else:
                    await route.fallback()
            await tab.route("**/*", skip)
            self.apply_timeouts(tab)
            blocks = await self._open_detail(tab, part)
            for block in blocks:
                block["how"] = f"opened by its address for {part}, in its own tab"
            return blocks
        finally:
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass

    async def _details_in_tabs(self, page, parts: list[str]) -> dict[str, list[dict]]:
        """Each part's PartsDetail address in its own tab, a few at once (team,
        2026-11-12). A page that failed for another reason gets one more try alone."""
        gate = asyncio.Semaphore(self.tabs_parallel)

        async def one(part: str):
            async with gate:
                return await self._detail_in_tab(page, part)

        started = time.time()
        results = await asyncio.gather(*(one(p) for p in parts), return_exceptions=True)
        out: dict[str, list[dict]] = {}
        for part, result in zip(parts, results):
            if isinstance(result, (SessionExpired, LoginBlocked)):
                raise result
            if isinstance(result, BaseException):
                logger.warning("Textron: %s not read in a tab (%r); trying it once more",
                               part, result)
                result = await self._detail_in_tab(page, part)
            out[part] = result
        logger.info("Textron: %d part page(s) in tabs in %.1fs", len(parts), time.time() - started)
        return out

    # -- Multiple Part Search ------------------------------------------------------------
    async def _multiple_search(self, page, rows: list[RequestRow]
                               ) -> tuple[list[dict], str, dict[str, str]]:
        """The Multiple Part Search form: (blocks, results address, {part key: qty
        typed}). Raises BulkSearchUnavailable when the form can't be used."""
        response = await page.goto(MULTIPLE_URL, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        await self._stop_if_refused(page, response)
        if not host_allowed(page.url, self.hosts) or "/account/login" in page.url.lower():
            raise SessionExpired(f"Textron Multiple Part Search went to {page.url}")
        if await self._password_field(page):
            raise SessionExpired("Textron sign-in form shown instead of Multiple Part Search")
        if not await page.locator(PART_BOXES).count():
            raise BulkSearchUnavailable(f"No part boxes on {page.url}")
        parts = [" ".join(r.part_number.split()) for r in rows]
        qtys = [requested_qty(r.qty_needed) or "" for r in rows]
        started = time.time()
        try:
            filled = await asyncio.wait_for(page.evaluate(FILL_FORM_JS, {
                "parts": parts, "qtys": qtys, "qtySel": QTY_IN_ROW,
                "custom": self.config.get("qty_box_selector") or ""}), web.PAGE_SCRIPT_TIMEOUT)
        except Exception as exc:
            raise BulkSearchUnavailable(f"Multiple Part Search form not filled: {exc!r}") from exc
        if filled.get("error"):
            raise BulkSearchUnavailable(f"Multiple Part Search: {filled['error']} "
                                        f"({filled.get('boxes')} box(es))")
        if filled["values"][:len(parts)] != parts:
            raise BulkSearchUnavailable(f"Multiple Part Search boxes hold {filled['values']!r}, "
                                        f"not {parts!r}")
        typed = {_key(parts[int(i)]): q for i, q in (filled.get("typed") or {}).items()}
        if filled.get("noQty"):
            # The signed-in form showed none the rules know (2026-11-12, 19 parts):
            # what its rows hold, to pin the quantity box.
            logger.info("Textron Multiple Part Search: no quantity box recognised; %d part(s) "
                        "searched without their quantity. Row inputs: %s", filled["noQty"],
                        filled.get("inputs"))
        logger.info("Textron Multiple Part Search: %d part(s) in %d box(es), %d quantity box(es) "
                    "filled in %.1fs", len(rows), filled["boxes"], len(typed), time.time() - started)
        button = page.locator(SEARCH_BUTTON).first
        if not await button.count():
            raise BulkSearchUnavailable("No Search button on the Multiple Part Search form")
        await self._submit_and_wait(page, lambda: button.click(no_wait_after=True),
                                    "Multiple Part Search")
        try:
            # The POST answers with a redirect to PartsDetail?PageType=Multiple (2026-11-12).
            await page.wait_for_url(re.compile(r"/parts/partsearch/partsdetail|/account/login",
                                               re.I), timeout=self.page_load_timeout_ms)
        except Exception:
            pass                          # judged by the address below
        await _settle(page)
        if "/account/login" in page.url.lower():
            raise SessionExpired("Textron Multiple Part Search went to the sign-in page")
        if DETAIL_PATH not in urlsplit(page.url).path.lower():
            message = (await web._body_text(page))[:200]
            raise BulkSearchUnavailable(f"Multiple Part Search stayed at {page.url}: {message!r}")
        blocks = await self._blocks(page)
        await self._check_signed_in(page, blocks)
        await self._read_service_center(page, blocks)
        for block in blocks:
            block["link"] = detail_url(block.get("pn") or "")
            block["how"] = "from the Multiple Part Search"
        return blocks, page.url, typed

    async def _read_service_center(self, page, blocks: list[dict]) -> None:
        """Each block's Service Center Stock dialog read into block["service_center"]
        (Internal Comments). Never stops the search: a part whose dialog isn't read
        just has no such note (logged)."""
        if not self.config.get("service_center_stock", True):
            return
        wanted = [b for b in blocks
                  if SERVICE_CENTER_LINE.search((b.get("side") or "") + "\n" + (b.get("main") or ""))
                  and not block_facts(b)["not_found"] and not _discontinued_only(b)]
        if not wanted:
            return
        budget = float(self.config.get("service_center_seconds", SERVICE_CENTER_SECONDS))
        calls: list[str] = []

        def on_request(request) -> None:
            # The dialog's own data address, if it has one: a direct fetch next time.
            if request.resource_type in ("xhr", "fetch") and len(calls) < 5:
                calls.append(f"{request.method} {request.url}")
        started = time.time()
        page.on("request", on_request)
        try:
            found = await asyncio.wait_for(page.evaluate(SERVICE_CENTER_JS, {
                "ns": [b["n"] for b in wanted], "perPart": SERVICE_CENTER_PART_SECONDS * 1000,
                "budget": budget * 1000, "maxRows": SERVICE_CENTER_ROWS_MAX}), budget + 20)
        except Exception as exc:
            logger.warning("Textron Service Center Stock not read at %s: %r", page.url, exc)
            return
        finally:
            page.remove_listener("request", on_request)
        read = 0
        for b in wanted:
            info = found.get(str(b["n"])) or found.get(b["n"]) or {}
            rows = service_center_rows(info.get("rows") or [])
            if rows:
                b["service_center"] = rows
                read += 1
            elif info.get("error") or not info.get("rows"):
                logger.info("Textron %s Service Center Stock: nothing read (%s; link %s; text %r)",
                            b.get("pn"), info.get("error") or "no table rows",
                            info.get("link", "-"), (info.get("text") or "")[:200])
            if info.get("close_failed"):
                logger.warning("Textron %s: Service Center Stock dialog didn't close (hidden by the "
                               "program)", b.get("pn"))
        logger.info("Textron Service Center Stock: %d of %d part(s) with stock listed, %.1fs%s",
                    read, len(wanted), time.time() - started,
                    f"; page calls seen: {calls}" if calls else "")
        if _dumping():
            _dump("service-center", "\n\n".join(
                f"--- {b.get('pn')} ---\n{found.get(str(b['n'])) or found.get(b['n'])}" for b in wanted))

    # -- the text the LLM reads ----------------------------------------------------------
    def _remember(self, block: dict, requested: str, qty_typed: Optional[str]) -> list[str]:
        """Facts of one block kept for _finish; its lines for the LLM."""
        facts = block_facts(block)
        facts["for"] = _key(requested)
        facts["how"] = block.get("how", "")
        self._facts[_clean_url(block["link"])] = facts
        self._remember_alternates(facts, requested)
        return _fact_lines(facts, requested, qty_typed)

    def _text(self, assigned: list[tuple[str, dict, Optional[str]]]) -> str:
        """[(requested part, block, qty typed)] -> the blocks as the LLM reads them."""
        lines: list[str] = []
        for n, (requested, block, qty) in enumerate(assigned, start=1):
            facts_lines = self._remember(block, requested, qty)
            lines += [HEADER.format(n=n, link=_clean_url(block["link"]), how=block["how"]),
                      FACTS_HEADER.format(n=n), *facts_lines,
                      TEXT_HEADER.format(n=n), block_section(block)]
        return "\n".join(lines)

    # -- one part ------------------------------------------------------------------------
    async def search(self, page, row: RequestRow) -> SearchOutcome:
        if self.extractor is None:
            raise RuntimeError("Textron search needs an LLM to read the pages")
        self.apply_timeouts(page)
        self._facts = {}
        part = row.part_number.strip()
        url = detail_url(part)
        blocks = await self._open_detail(page, part)
        products = [b for b in blocks if not block_facts(b)["not_found"]]
        if not products:
            message = next((block_facts(b)["error"] for b in blocks), "") or "no part block"
            logger.info("Textron: %s not found (%s)", part, message)
            usage, self.usage = self.usage, type(self.usage)()
            self._search_ok = True
            return SearchOutcome(found=False, usage=usage, page_url=url, page_kind="no_results")
        selling = [b for b in products if not _discontinued_only(b)]
        if not selling:
            reason = self._not_sold(products[0], part)
            logger.info("Textron: %s %s", part, reason)
            usage, self.usage = self.usage, type(self.usage)()
            self._search_ok = True
            return SearchOutcome(found=False, usage=usage, page_url=url, not_sold=reason,
                                 page_kind="results", alternates=self.listed_alternates(row))
        products = selling
        text = self._text([(part, b, None) for b in products])
        logger.info("Textron (read): %d block(s) for %s, %d chars", len(products), part, len(text))
        if _dumping():
            _dump(_key(part)[:60] or "part", text)
        self._product_text = text
        outcome = await self._read_results(page, row, url)
        outcome.offers = self._finish(outcome.offers or [], part)
        outcome.found = bool(outcome.offers)
        return outcome

    async def _results_text(self, page, label: str) -> str:
        if self._product_text is not None:
            text, self._product_text = self._product_text, None
            return text
        return await super()._results_text(page, label)

    def _not_sold(self, block: dict, requested: str) -> str:
        """The block's "NO LONGER AVAILABLE" with the replacement Textron lists below
        it, as the row's reason (its alternates stay in Internal Comments too)."""
        facts = block_facts(block)
        self._remember_alternates(facts, requested)
        options = [f"{a['PartNumber']}" + (f" ({a['SubDesc']})" if a.get("SubDesc") else "")
                   for a in facts["alternates"]]
        return facts["discontinued"] + (f" OPTIONS LISTED: {'; '.join(options)}" if options else "")

    def _remember_alternates(self, facts: dict, requested: str) -> None:
        if _key(facts["pn"]) == _key(requested):
            alts = [a["PartNumber"] for a in facts["alternates"]]
            if alts:
                self._alternates[_key(requested)] = list(dict.fromkeys(alts))

    def listed_alternates(self, row: RequestRow) -> list[str]:
        """The part block's alternates ("Cessna Alternate", "One-Way Supersedure") as
        plain numbers for the requested part's Internal Comments: no page opened."""
        if self.show_substitutes:
            return []
        own = _key(row.part_number)
        found = [pn for pn in self._alternates.get(own, []) if _key(pn) != own]
        return found[: int(self.config.get("substitutes_max", SUBSTITUTES_MAX))]

    # -- several parts --------------------------------------------------------------------
    async def search_many(self, page, rows: list[RequestRow]):
        if self.bulk_unavailable is not None:
            raise BulkSearchUnavailable(self.bulk_unavailable)
        try:
            return await self._search_many(page, rows)
        except BulkResultsIncomplete:
            raise
        except BulkSearchUnavailable as exc:
            self.bulk_unavailable = str(exc)
            raise

    async def _search_many(self, page, rows: list[RequestRow]):
        if self.extractor is None:
            raise RuntimeError("Textron search needs an LLM to read the pages")
        self.apply_timeouts(page)
        self._facts = {}
        parts = list(dict.fromkeys(" ".join(r.part_number.split()) for r in rows))
        by_key = {_key(p): p for p in parts}
        found_blocks: dict[str, list[dict]] = {}
        typed: dict[str, str] = {}
        url = MULTIPLE_URL
        if self.bulk_mode == "form" and self.form_unavailable is None:
            unique_rows = list({_key(r.part_number): r for r in rows}.values())
            try:
                blocks, url, typed = await self._multiple_search(page, unique_rows)
                for block in blocks:
                    wanted = by_key.get(_key(block.get("pn") or ""))
                    if wanted:
                        block["for"] = wanted
                        found_blocks.setdefault(wanted, []).append(block)
            except BulkSearchUnavailable as exc:
                # The tabs still work: this run uses them instead of the form.
                self.form_unavailable = str(exc)
                logger.warning("Textron: Multiple Part Search not usable (%s); opening each "
                               "part's page in a tab instead", exc)
        missing = [p for p in parts if p not in found_blocks]
        if missing:
            if self.bulk_mode == "form" and self.form_unavailable is None:
                logger.info("Textron: %d part(s) not in the Multiple Part Search results; "
                            "opening their pages: %s", len(missing), ", ".join(missing))
            for part, blocks in (await self._details_in_tabs(page, missing)).items():
                found_blocks[part] = blocks
        assigned: list[tuple[str, dict, Optional[str]]] = []
        absent: set[str] = set()
        not_sold: dict[str, str] = {}
        for part in parts:
            blocks = [b for b in found_blocks.get(part, []) if not block_facts(b)["not_found"]]
            if not blocks:
                absent.add(_key(part))
                continue
            selling = [b for b in blocks if not _discontinued_only(b)]
            if not selling:
                not_sold[_key(part)] = self._not_sold(blocks[0], part)
                continue
            assigned += [(part, b, typed.get(_key(part))) for b in selling]
        outcomes: dict[str, SearchOutcome] = {}
        usage = type(self.usage)()
        found: dict[str, list] = {}
        if assigned:
            text = self._text(assigned)
            if _dumping():
                _dump("multiple", text)
            if len(text) > self.max_page_chars:
                raise BulkResultsIncomplete(f"Textron results are {len(text)} chars "
                                            f"(limit {self.max_page_chars})")
            check_rate_limited(text)
            asked = [r for r in rows if _key(r.part_number) not in absent
                     and _key(r.part_number) not in not_sold]
            request = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                        "condition_wanted": r.condition_wanted} for r in asked]
            kind, found, usage = await self.extractor.extract_offers_bulk(
                page_text=text, page_url=url, parts=request, system=self.extract_system)
            if kind == "login_page":
                raise SessionExpired("Extractor saw a login page")
            if kind == "error":
                raise BulkResultsIncomplete("Could not read the Textron results (LLM answer "
                                            "missing or cut off)")
        usage.add(self.usage)
        self.usage = type(self.usage)()
        for r in rows:
            offers = self._finish(found.get(r.part_number) or [], r.part_number)
            if offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=url,
                                                   alternates=self.listed_alternates(r))
                self._search_ok = self._search_ok or has_real_offer(offers)
            elif _key(r.part_number) in not_sold:
                # "NO LONGER AVAILABLE": not sold any more, its replacement named.
                outcomes[r.row_id] = SearchOutcome(
                    found=False, not_sold=not_sold[_key(r.part_number)],
                    page_url=detail_url(r.part_number), page_kind="results",
                    alternates=self.listed_alternates(r))
            elif _key(r.part_number) in absent:
                # "Part Not Found." on its own page (or the form's), signed in: an
                # outcome here keeps the pipeline from searching it again one by one.
                outcomes[r.row_id] = SearchOutcome(found=False, page_url=detail_url(r.part_number),
                                                   page_kind="no_results")
        logger.info("Textron multi-part search: offers for %d of %d parts (%d not found)",
                    sum(1 for o in outcomes.values() if o.found), len(rows), len(absent))
        return outcomes, usage

    # -- the line: what the page states ---------------------------------------------------
    def _finish(self, offers: list, requested: str) -> list:
        """The page's own values set on each line (team, 2026-11-04: no value the
        page doesn't state): part number and listing from the block, unit, minimum
        order, condition; the price checked against the page's own price fields
        (USD / EUR) for its currency. An offer for no block read is dropped."""
        kept = []
        for offer in offers:
            facts = self._facts_for(offer, requested)
            if facts is None:
                logger.warning("Textron: offer for %s matches no part block read (%s); dropped",
                               offer.part_number, offer.source_url)
                continue
            self._match_page(offer, facts, requested)
            kept.append(offer)
        return kept

    def _facts_for(self, offer, requested: str) -> Optional[dict]:
        facts = self._facts.get(_clean_url(offer.source_url or ""))
        if facts is not None:
            return facts
        mine = [f for f in self._facts.values() if f["for"] == _key(requested)]
        same = [f for f in mine if _key(f["pn"]) == _key(offer.part_number)]
        if len(same) == 1:
            return same[0]
        return mine[0] if len(mine) == 1 else None

    def _match_page(self, offer, facts: dict, requested: str) -> None:
        changed = []
        flags = [f for f in (offer.review or "").split(",") if f]

        def put(field: str, value) -> None:
            if (getattr(offer, field) or None) != (value or None):
                changed.append(f"{field} {getattr(offer, field)!r} -> {value!r}")
                setattr(offer, field, value)

        if facts["pn"]:
            put("part_number", facts["pn"])
        put("listing", "requested" if _key(facts["pn"]) == _key(requested) else "other")
        if facts["uom"]:
            put("uom", facts["uom"])
        put("moq", facts["moq"])
        code = normalize_condition(facts["condition"])
        if code is None and (offer.condition or "").strip().upper() not in CONDITION_CODES:
            code = self.config.get("condition_not_shown", CONDITION_NOT_SHOWN) or None
        if code:
            put("condition", code)
        if facts["stock"] is not None:
            # "0 IN STOCK" is out of stock; the stock rule then keeps the price.
            out = float(facts["stock"]) == 0
            put("qty_available", "Out of stock" if out else facts["stock"])
            # The one warehouse Textron names ("Ships from Wichita" / "Columbus, GA"),
            # in the Warehouses cell's own layout (extract.format_warehouses).
            if facts["ships_from"]:
                put("warehouses", f"Warehouse: {facts['ships_from']}\nQty on hand: {facts['stock']}")
            if not out:
                put("lead_time", "In stock")
            elif facts["estimate"]:
                put("lead_time", f"Estimated availability {facts['estimate']}")
        if facts["notes"]:
            notes = [n for n in (offer.vendor_note or "").split("; ") if n]
            offer.vendor_note = "; ".join(dict.fromkeys(notes + facts["notes"]))
        if not offer.description and facts["description"]:
            offer.description = facts["description"]
        price = _number(offer.price)
        if price is not None and not (facts["price_us"] or facts["price_eur"]):
            # The page's price fields are 0 / empty: no price shown ("PLEASE CONTACT US
            # FOR ASSISTANCE WITH PRICING"), whatever figure the LLM took.
            put("price", NOT_PRESENT)
            price = None
        if price is not None:
            us, eur = _number(facts["price_us"]), _number(facts["price_eur"])
            if us is not None and abs(price - us) < 0.005:
                put("currency", "USD")
            elif eur is not None and abs(price - eur) < 0.005:
                put("currency", "EUR")
            elif us is not None or eur is not None:
                # The line's price is none of the page's own price fields: a person checks.
                logger.warning("Textron %s: price %s is not the page's own (USD %s, EUR %s)",
                               offer.part_number, offer.price, facts["price_us"] or "-",
                               facts["price_eur"] or "-")
                flags.append("PRICE_NOT_IN_PAGE_DATA")
        if changed:
            logger.warning("Textron %s: set to what the page states: %s", offer.part_number,
                           "; ".join(changed))
            fixed = {"UOM", "MOQ", "CONDITION", "CURRENCY", "PART_NUMBER", "QTY", "PRICE",
                     "WAREHOUSES", "LEAD_TIME"}
            flags = [f for f in flags if f.removesuffix("_NOT_ON_PAGE") not in fixed]
        offer.review = ",".join(dict.fromkeys(flags)) or None


def _discontinued_only(block: dict) -> bool:
    """A block saying "NO LONGER AVAILABLE" with no price and no stock: nothing to quote."""
    facts = block_facts(block)
    return bool(facts["discontinued"]) and not (facts["price_us"] or facts["price_eur"]) \
        and not facts["stock"]


async def _save_page(page, label: str) -> None:
    """With DEBUG_DUMP_PAGES=1, a login step's page (HTML) for pinning its elements."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-textron-{label}.html"
        path.write_text(await page.content(), encoding="utf-8")
        logger.info("Saved the %s page to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the %s page: %r", label, exc)


def _dumping() -> bool:
    return os.environ.get("DEBUG_DUMP_PAGES", "").strip() in ("1", "true", "yes")


def _dump(label: str, text: str) -> None:
    """With DEBUG_DUMP_PAGES=1, the whole text the LLM reads."""
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-textron-{label}-read.txt"
        path.write_text(text, encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
