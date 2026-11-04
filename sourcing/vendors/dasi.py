"""DasiAdapter: DASI's store (store.dasi.com, a Salesforce Experience site).

Login, logout and the sign-in check are the generic adapter's (Log out sits in
the profile menu; seen 2026-10-29). Searching is DASI's own:

  one part   product-search?type=simple&searchterms=<part>&exact=true
  bulk       product-search?type=multiple&exact=true&product=<A>&product=<B>...
             (addresses from the client, 2026-10-29; exact=true so only the
             parts asked for are listed, not similar numbers)

Before the results page is read, the program
  1. sets "Fulfilled by" to DASI Only and tries the Location filter for DASI
     Miami HQ (FL): only DASI's own stock from that warehouse is quoted
     (client, 2026-10-29)
  2. opens every part block (one script click on all part numbers, then one
     by one for any still closed): the lots are only drawn once it is open
  3. reads each block's grid as records, one per lot, by column name, with the
     Trace/Tag cell's hover text (trace source, tag details), and keeps only
     lots whose Ship From is DASI Miami HQ (FL) (vendor config `ship_from`)
and the records are read with DASI's own prompt (dasi_prompt.py).

No View clicks (vendor config `view_details_max` turns them back on): run 12
(2026-10-29) spent ~13s per View and a 66-lot bulk page ran past the 600s bulk
limit twice, and the View dialog adds nothing we use that the grid row and its
hover text don't have (only serial no., tag date, documents, shelf life).
A run with --dump-pages saves what was read as debug_pages/*-dasi-read.txt.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import quote_plus, urlsplit

from ..models import RequestRow, SearchOutcome
from . import web
from .base import (
    BulkResultsIncomplete, BulkSearchUnavailable, SessionExpired, check_rate_limited,
    has_real_offer, host_allowed,
)
from .dasi_prompt import DASI_EXTRACT_SYSTEM
from .generic import GenericAdapter
from .web import _settle, accept_cookies

logger = logging.getLogger("sourcing.vendors.dasi")

SEARCH_URL = "https://store.dasi.com/product-search?type=simple&searchterms={part}&exact=true"
BULK_URL = "https://store.dasi.com/product-search?type=multiple&exact=true"
RESULTS_PATH = "/product-search"

RESULT_ITEM = ".result-item"
HEADER = ".header"
PART_IN_HEADER = ".header .item-info"
EXPAND_BUTTON = ".header button[title='Expand/Collapse']"
# The visible "Fulfilled by" list is drawn over this hidden select.
FILTER_SELECT = "select[name='stock-source']"
DASI_ONLY = "dasi-only"
FILTER_NOTE = "--- Filter: Fulfilled by = DASI Only (set by the program) ---"
# The only warehouse quoted from (client, 2026-10-29).
SHIP_FROM = "DASI Miami HQ (FL)"
LOCATION_PICKLIST = "[role='location-picklist']"
RECORDS_NOTE = ("--- DASI results, one record per lot (read from the page by the program; "
                "only lots shipped from {ship_from}) ---")
# Columns that are controls, not values.
_SKIP_COLUMNS = ("", "action", "cart qty", "select item")
# Each block's header and its grid rows by column name, with the hover text of
# a cell (Trace/Tag: "Trace: CHINA SOUTHERN AIRLINES", "Tag Details: OEM C OF C
# ONLY"); the document icon's tooltip is left out. lots = null: block closed.
# Run on the blocks Playwright finds (evaluate_all), never from `document`:
# DASI's Salesforce components hide their insides from document.querySelectorAll
# (run 12, 2026-10-29: it found 0 blocks while 10 were open), not from a query
# that starts at the block itself.
RECORDS_JS = """items => items.map(item => {
  const t = n => (n ? n.textContent || '' : '').replace(/\\s+/g, ' ').trim();
  const header = t(item.querySelector('.item-info') || item.querySelector('.header'));
  const table = item.querySelector('table');
  if (!table) return {header, lots: null};
  const heads = Array.from(table.querySelectorAll('thead th')).map(t);
  const lots = Array.from(table.querySelectorAll('tbody tr')).map(tr => {
    const cells = [], notes = [];
    Array.from(tr.children).forEach((td, i) => {
      if (!td.querySelector('lightning-icon'))
        td.querySelectorAll('[role=tooltip]').forEach(tip => {
          const ps = tip.querySelectorAll('p');
          (ps.length ? Array.from(ps) : [tip])
            .forEach(p => { const v = t(p); if (v && !notes.includes(v)) notes.push(v); });
        });
      const cell = td.cloneNode(true);
      cell.querySelectorAll('[role=tooltip], .slds-assistive-text').forEach(e => e.remove());
      cells.push([heads[i] || '', t(cell)]);
    });
    return {cells, notes};
  });
  return {header, lots};
})"""
VIEW_LINKS = ", ".join(f".result-item {tag}:text-is('View')"
                       for tag in ("a", "button", "[role='button']"))
DIALOG = "[role='dialog'], section.slds-modal, .slds-modal"
CLOSE_BUTTON = ("button[title*='close' i], button[aria-label*='close' i], "
                "button:text-is('Close'), button:text-is('×')")

# Most part blocks opened per page. View links opened per one-part / bulk page:
# none unless vendor config `view_details_max` / `view_details_max_bulk` says.
MAX_BLOCKS = 60
VIEWS_PER_PART = 0
VIEWS_PER_BULK = 0
VIEW_MAX_CHARS = 3000
# How long a block's rows / a View's dialog may take to appear after the click.
OPEN_WAIT_MS = 4000
# Lot records per LLM call on a bulk page: each line is ~250 output tokens, and
# 63 lines in one call ran past the 16000-token limit (run 12). The calls run
# at the same time.
LOTS_PER_CALL = 20


class DasiAdapter(GenericAdapter):
    vendor_id = "dasi"
    display_name = "DASI"
    allowed_hosts = ("dasi.com",)
    default_login_url = "https://store.dasi.com/login"
    default_part_url_template = SEARCH_URL
    extract_system = DASI_EXTRACT_SYSTEM
    # Not DASI's confirmed limit: the batch size used for Procart.
    max_parts_per_search = 25
    default_bulk_batch_size = 25
    default_bulk_min_parts = 2

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bulk_unavailable: Optional[str] = None

    @property
    def part_url(self) -> str:
        """Vendor record `part_url_template`, else DASI's exact-match address
        (never one learned from a search box: that one has exact=false)."""
        if "part_url_template" in self.config:
            return self.config.get("part_url_template") or ""
        return SEARCH_URL

    # -- reading a results page -------------------------------------------
    @property
    def ship_from(self) -> str:
        """The warehouse quoted from: vendor config `ship_from` ("" = any)."""
        return self.config.get("ship_from", SHIP_FROM) or ""

    async def _results_text(self, page, label: str, views: Optional[int] = None) -> str:
        """Filter notes and the lot records; the page text when there are no part
        blocks at all ("No Results Found", a portal message). Raises
        BulkResultsIncomplete when a block couldn't be opened: its lots would be
        missing (a wrong NOT_FOUND), so the pipeline tries that batch again."""
        if views is None:
            views = int(self.config.get("view_details_max", VIEWS_PER_PART))
        await self._wait_for_results(page)
        filtered = await self._dasi_only(page)
        await self._ship_from_filter(page)
        closed = await self._open_blocks(page)
        if closed:
            raise BulkResultsIncomplete(f"DASI: {len(closed)} part block(s) didn't open: "
                                        + "; ".join(closed[:5]))
        blocks = await page.locator(RESULT_ITEM).evaluate_all(RECORDS_JS)
        notes = [FILTER_NOTE] if filtered else []
        if not blocks:
            if await page.locator(RESULT_ITEM).count():
                logger.warning("DASI: part blocks shown but none read; using the page text")
            text = "\n\n".join([*notes, await web.page_text(page, label)])
        else:
            text = "\n\n".join([*notes, self._records_text(blocks)])
            if views:
                text += "\n\n" + "\n\n".join(await self._view_details(page, views))
        _dump(label, text)
        return text

    def _records_text(self, blocks: list[dict]) -> str:
        """One line per lot: '<part> lot <n>: Cond: NS | Ship From: ... | Trace:
        ...'. Lots from another warehouse than `ship_from` are left out."""
        ship_from = " ".join(self.ship_from.split()).lower()
        lines = [RECORDS_NOTE.format(ship_from=self.ship_from or "any warehouse")]
        kept = dropped = 0
        for block in blocks:
            header = block.get("header") or ""
            part = header.split(" (")[0].strip()
            lines.append(f"\nPart block: {header}")
            lots = []
            for lot in block.get("lots") or []:
                cells = [(k, v) for k, v in lot["cells"] if k.lower() not in _SKIP_COLUMNS and v]
                where = next((v for k, v in cells if k.lower() == "ship from"), None)
                if ship_from and where is not None and " ".join(where.split()).lower() != ship_from:
                    dropped += 1
                    continue
                lots.append((cells, lot.get("notes", [])))
            n = 0
            for cells, notes, combined in _combine(lots):
                n += 1
                lines.append(f"{part} lot {n}" + (f" ({combined} lots combined)" if combined > 1
                                                  else "") + ": " + " | ".join(
                    [f"{k}: {v}" for k, v in cells] + [_note(x) for x in notes]))
            if not n:
                lines.append(f"{part}: no lots" + (f" shipped from {self.ship_from}"
                                                   if ship_from else ""))
            kept += n
        logger.info("DASI: read %d lot(s) from %d part block(s)%s", kept, len(blocks),
                    f"; left out {dropped} from other warehouses" if dropped else "")
        return "\n".join(lines)

    async def _wait_for_results(self, page) -> None:
        """The blocks are drawn by script after the page loads."""
        try:
            await page.wait_for_function(
                "() => document.querySelector('.result-item') || "
                "/\\d+\\s+of\\s+\\d+\\s+items|no (results|products)/i.test(document.body.innerText)",
                timeout=self.page_load_timeout_ms)
        except Exception:
            logger.warning("DASI: no result blocks or item count at %s", page.url)
        shown = await page.evaluate(
            "() => (document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+items/i) || [])"
            ".slice(1).map(Number)")
        if len(shown) == 2:
            # "7 of 19": lots left after the page's filters, of all lots found.
            logger.info("DASI: page shows %d of %d items", shown[0], shown[1])

    async def _dasi_only(self, page) -> bool:
        """Set "Fulfilled by" to DASI Only. False (logged) when it can't be set:
        the prompt then leaves out lines marked third party."""
        select = page.locator(FILTER_SELECT).first
        try:
            if not await select.count():
                logger.warning("DASI: no Fulfilled by filter at %s", page.url)
                return False
            if await select.input_value() == DASI_ONLY:
                return True
            before = await web.content_text(page)
            try:
                await select.select_option(DASI_ONLY, force=True, timeout=5000)
            except Exception:
                # Hidden behind DASI's own list: set it and tell the page.
                await select.evaluate(
                    "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', "
                    "{bubbles: true, composed: true})); }", DASI_ONLY)
            await _settle(page, 5000)
            for _ in range(10):           # the list is drawn again by script
                if await web.content_text(page) != before:
                    break
                await page.wait_for_timeout(500)
            ok = await select.input_value() == DASI_ONLY
        except Exception as exc:
            logger.warning("DASI: Fulfilled by filter not set: %r", exc)
            return False
        logger.info("DASI: Fulfilled by = DASI Only %s", "set" if ok else "NOT set")
        return ok

    async def _ship_from_filter(self, page) -> bool:
        """Pick `ship_from` in the Location filter. Its options are only drawn once
        it is opened and weren't in any saved page, so this is best effort; the
        records are filtered by Ship From in any case (_records_text)."""
        if not self.ship_from:
            return False
        box = page.locator(f"{LOCATION_PICKLIST} [role='button']").first
        try:
            if not await box.count():
                logger.info("DASI: no Location filter at %s", page.url)
                return False
            await box.click(timeout=3000)
            option = page.locator(LOCATION_PICKLIST).get_by_text(self.ship_from, exact=True).first
            await option.wait_for(state="visible", timeout=3000)
            before = await web.content_text(page)
            await option.click(timeout=3000)
            await page.keyboard.press("Escape")
            for _ in range(OPEN_WAIT_MS // 250):          # the list is drawn again
                if await web.content_text(page) != before:
                    break
                await page.wait_for_timeout(250)
        except Exception as exc:
            logger.info("DASI: Location filter not set (%s); keeping only Ship From %s lots",
                        str(exc).splitlines()[0], self.ship_from)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        logger.info("DASI: Location = %s set", self.ship_from)
        return True

    async def _open_blocks(self, page) -> list[str]:
        """Open every part block; returns the headers of blocks still closed.
        First one script click on every closed block's part number at once (run
        12: one at a time took ~6s each), then, for any still closed: a click on
        the part number, a script click on the + (Playwright's normal click on it
        timed out on DASI), a script click on the header."""
        items = page.locator(RESULT_ITEM)
        count = min(await items.count(), MAX_BLOCKS)
        clicked = await items.evaluate_all(
            """(items, max) => items.slice(0, max)
                 .filter(it => !it.querySelector('table'))
                 .map(it => (it.querySelector('.item-info') || it.querySelector('.header')).click())
                 .length""", count)
        for _ in range(2 * OPEN_WAIT_MS // 250):
            if await items.evaluate_all(
                    "(items, max) => items.slice(0, max).every(it => it.querySelector('table'))",
                    count):
                break
            await page.wait_for_timeout(250)
        ways = {"all at once": 0}
        closed = []
        for i in range(count):
            item = items.nth(i)
            if await _block_open(item):
                ways["all at once"] += 1
                continue
            tries = (
                ("part number", lambda: item.locator(PART_IN_HEADER).first.click(timeout=3000)),
                ("+ (script click)",
                 lambda: item.locator(EXPAND_BUTTON).first.evaluate("b => b.click()")),
                ("header (script click)",
                 lambda: item.locator(HEADER).first.evaluate("h => h.click()")),
            )
            for how, click in tries:
                try:
                    await click()
                except Exception as exc:
                    logger.debug("DASI: block %d, %s: %s", i + 1, how, str(exc).splitlines()[0])
                    continue
                if await _wait_open(page, item):
                    ways[how] = ways.get(how, 0) + 1
                    break
            else:
                header = " ".join((await item.locator(HEADER).first.inner_text()).split())
                logger.warning("DASI: part block %d (%s) didn't open", i + 1, header[:80])
                closed.append(header[:80])
        logger.info("DASI: %d of %d part block(s) open (%s; %d clicked at once)",
                    count - len(closed), count,
                    ", ".join(f"{n} {how}" for how, n in ways.items() if n) or "none", clicked)
        return closed

    async def _view_details(self, page, limit: int) -> list[str]:
        """Open each row's View link and return what it shows, labelled with the
        row. A View may open a dialog, a new tab, a new page or a panel in the
        row; each is read and closed again."""
        out: list[str] = []
        new_tabs: list = []

        def on_tab(tab) -> None:       # Playwright wants a plain function here
            new_tabs.append(tab)

        page.context.on("page", on_tab)
        try:
            links = page.locator(VIEW_LINKS)
            count = await links.count()
            if count > limit:
                logger.warning("DASI: %d View links; opening the first %d", count, limit)
            for i in range(min(count, limit)):
                link = links.nth(i)
                try:
                    detail, row_text = await self._open_view(page, link, new_tabs)
                except Exception as exc:
                    logger.warning("DASI: View %d not read: %s", i + 1, str(exc).splitlines()[0])
                    continue
                if detail:
                    out.append(f"--- View details: {row_text} ---\n{detail[:VIEW_MAX_CHARS]}")
        finally:
            page.context.remove_listener("page", on_tab)
        logger.info("DASI: read %d View detail(s)", len(out))
        return out

    async def _open_view(self, page, link, new_tabs: list) -> tuple[str, str]:
        if not await link.is_visible():
            return "", ""
        row_text = await link.evaluate(
            "el => { const row = el.closest('tr, [role=row]') || el.parentElement;"
            " const item = el.closest('.result-item');"
            " const head = item && (item.querySelector('.item-info') || item.querySelector('.header'));"
            " const t = n => (n?.innerText || '').replace(/\\s+/g, ' ').trim();"
            " return (t(head) + ' | ' + t(row)).slice(0, 300); }")
        href = await link.get_attribute("href") or ""
        if href.startswith("http") and not host_allowed(href, self.hosts):
            logger.warning("DASI: View link %s leaves the site; not opened", href)
            return "", row_text
        item_before = await link.evaluate(
            "el => (el.closest('.result-item') || document.body).innerText")
        start_url = page.url.split("#")[0]
        try:
            await link.click(timeout=3000)
        except Exception:
            await link.evaluate("el => el.click()")
        # Wait for what the click opens, not for the network (DASI's never settles).
        for _ in range(OPEN_WAIT_MS // 250):
            if new_tabs or page.url.split("#")[0] != start_url or await _visible_dialog(page):
                break
            await page.wait_for_timeout(250)

        if new_tabs:                                    # opened in a new tab
            tab = new_tabs.pop()
            try:
                await tab.wait_for_load_state("domcontentloaded", timeout=self.page_load_timeout_ms)
                await _settle(tab, 5000)
                return (await tab.inner_text("body")).strip(), row_text
            finally:
                await tab.close()
        if page.url.split("#")[0] != start_url:        # opened as a page
            text = (await page.inner_text("body")).strip()
            await page.go_back(wait_until="domcontentloaded")
            await _settle(page)
            await self._wait_for_results(page)
            await self._dasi_only(page)
            await self._open_blocks(page)
            return text, row_text
        box = await _visible_dialog(page)
        if box is not None:                             # opened as a dialog
            text = (await box.inner_text()).strip()
            close = box.locator(CLOSE_BUTTON).first
            try:
                if await close.count() and await close.is_visible():
                    await close.click(timeout=3000)
                else:
                    await page.keyboard.press("Escape")
                await box.wait_for(state="hidden", timeout=OPEN_WAIT_MS)
            except Exception:
                await page.keyboard.press("Escape")
            return text, row_text
        # Opened in place: what the click added to the part block.
        item_after = await link.evaluate(
            "el => (el.closest('.result-item') || document.body).innerText")
        seen = set(item_before.splitlines())
        return "\n".join(l for l in item_after.splitlines() if l not in seen).strip(), row_text

    # -- bulk search --------------------------------------------------------
    async def search_many(self, page, rows: list[RequestRow]):
        if self.bulk_unavailable is not None:
            raise BulkSearchUnavailable(self.bulk_unavailable)
        try:
            return await self._search_many(page, rows)
        except BulkResultsIncomplete:
            raise                       # this batch only; the bulk address itself works
        except BulkSearchUnavailable as exc:
            self.bulk_unavailable = str(exc)
            raise

    async def _search_many(self, page, rows: list[RequestRow]):
        self.apply_timeouts(page)
        url = (self.config.get("bulk_search_url") or BULK_URL) + "".join(
            f"&product={quote_plus(r.part_number)}" for r in rows)
        logger.info("Bulk search: %d parts in one search (%s)", len(rows), url)
        response = await page.goto(url, wait_until="domcontentloaded")
        await _settle(page)
        await accept_cookies(page)
        if await self._signed_out_here(page):
            raise SessionExpired("Signed out when opening the bulk results address")
        status = response.status if response is not None else 0
        if status >= 400 or urlsplit(page.url).path.lower() != RESULTS_PATH:
            raise BulkSearchUnavailable(f"Bulk address gave HTTP {status} at {page.url}")

        text = await self._results_text(
            page, "dasi-bulk", int(self.config.get("view_details_max_bulk", VIEWS_PER_BULK)))
        if any(h in text.lower() for h in web.EXPIRED_HINTS):
            raise SessionExpired("Portal says the session expired")
        check_rate_limited(text)
        if len(text) > self.max_page_chars:
            # The pipeline tries half the batch next.
            raise BulkResultsIncomplete(f"Bulk results are {len(text)} chars "
                                        f"(limit {self.max_page_chars})")
        parts = [{"part_number": r.part_number, "qty_needed": r.qty_needed,
                  "condition_wanted": r.condition_wanted} for r in rows]
        chunks = _record_chunks(text, LOTS_PER_CALL)
        if len(chunks) > 1:
            logger.info("DASI: reading %d lot record(s) in %d LLM calls at once",
                        _lot_count(text), len(chunks))
        answers = await asyncio.gather(*(self.extractor.extract_offers_bulk(
            page_text=chunk, page_url=page.url, parts=parts, system=self.extract_system)
            for chunk in chunks))
        found: dict[str, list] = {}
        usage = type(self.usage)()
        for kind, part_offers, used in answers:
            usage.add(used)
            if kind == "login_page":
                raise SessionExpired("Extractor saw a login page")
            if kind == "error":
                # Most often an answer too long for the LLM's output limit (run 12,
                # 2026-10-29: 63 lots in one call): the pipeline tries half the batch.
                raise BulkResultsIncomplete("Could not read the bulk results (LLM answer "
                                            "missing or cut off)")
            for pn, offers in part_offers.items():
                found.setdefault(pn, []).extend(offers)
        usage.add(self.usage)
        self.usage = type(self.usage)()
        outcomes = {}
        for r in rows:
            offers = found.get(r.part_number) or []
            if offers:
                outcomes[r.row_id] = SearchOutcome(found=True, offers=offers, page_url=page.url)
                self._search_ok = self._search_ok or has_real_offer(offers)
        logger.info("Bulk search: offers for %d of %d parts", len(outcomes), len(rows))
        return outcomes, usage


# Lots alike in all of these are one line (client, 2026-10-29: "if pricing,
# stock location and MOQ is the same, lines can be combined"; condition and lead
# time added so one line never mixes two condition codes or deliveries; UOM so
# EA and LB quantities are never added up, 2026-10-30).
COMBINE_BY = ("cond", "ship from", "lead (days)", "uom", "moq", "price")
QTY_COLUMN = "qty avail"
TRACE_COLUMN = "trace/tag"
# Hover-text labels (_note) that name the source of the lot's own Trace/Tag.
TRACE_SOURCES = ("Trace", "Trace/Tag note")


def _combine(lots: list[tuple[list, list]]) -> list[tuple[list, list, int]]:
    """Lots of one part -> lines: (cells, notes, how many lots). Combined lots
    add up their Qty Avail ("273 (138 + 135)") and list every other value that
    differs, in page order. Trace/Tag keeps each lot's source with its own trace
    ("FAA 129 (CHINA SOUTHERN AIRLINES), FAA 121 (REPUBLIC AIRWAYS)", _paired_traces).
    A lot whose quantity isn't a plain number stays on its own."""
    groups: dict[tuple, list] = {}
    for i, (cells, notes) in enumerate(lots):
        values = {k.lower(): " ".join(v.split()).lower() for k, v in cells}
        key = tuple(values.get(k, "") for k in COMBINE_BY)
        if _qty(values.get(QTY_COLUMN)) is None:
            key += (i,)
        groups.setdefault(key, []).append((cells, notes))
    out = []
    for group in groups.values():
        if len(group) == 1:
            out.append((group[0][0], group[0][1], 1))
            continue
        cells = []
        paired = False
        for column, _ in group[0][0]:
            values = [v for c, _ in group for k, v in c if k == column]
            if column.lower() == QTY_COLUMN:
                total = sum(_qty(v) for v in values)
                cells.append((column, f"{total} ({' + '.join(values)})"))
            elif column.lower() == TRACE_COLUMN:
                cells.append((column, _paired_traces(group)))
                paired = True
            else:
                cells.append((column, _join(values)))
        labels: dict[str, list] = {}
        for _, notes in group:
            for note in notes:
                label, _, value = _note(note).partition(":")
                if paired and label.strip() in TRACE_SOURCES:
                    continue                  # already in Trace/Tag, with its own trace
                labels.setdefault(label.strip(), []).append(value.strip())
        # A note only some lots have says so: the tag of 138 pieces isn't the
        # tag of all 273.
        out.append((cells, [f"{label}: {_join(values)}"
                            + (f" ({len(values)} of {len(group)} lots)"
                               if len(values) < len(group) else "")
                            for label, values in labels.items()], len(group)))
    return out


def _paired_traces(group: list[tuple[list, list]]) -> str:
    """Combined lots' Trace/Tag values, each with the sources from its own lots'
    hover text, in page order: "FAA 129 (CHINA SOUTHERN AIRLINES), FAA 121
    (REPUBLIC AIRWAYS)". A source belongs to its lot's trace, not to the whole
    line (client, 2026-10-30); lots with the same trace share its brackets."""
    by_trace: dict[str, list] = {}
    for cells, notes in group:
        trace = next((v for k, v in cells if k.lower() == TRACE_COLUMN), "")
        sources = by_trace.setdefault(" ".join(trace.split()), [])
        for note in notes:
            label, _, value = _note(note).partition(":")
            if label.strip() in TRACE_SOURCES and value.strip():
                sources.append(value.strip())
    return ", ".join(f"{trace} ({_join(sources)})" if trace and sources else trace or _join(sources)
                     for trace, sources in by_trace.items() if trace or sources)


_LOT_LINE = re.compile(r"^\S+ lot \d+", re.M)


def _lot_count(text: str) -> int:
    return len(_LOT_LINE.findall(text))


def _record_chunks(text: str, max_lots: int) -> list[str]:
    """Records text -> pieces of whole part blocks with at most `max_lots` lot
    lines each (a bigger block stays whole), each with the notes at the top.
    Page text without records is one piece."""
    head, sep, body = text.partition("\nPart block: ")
    if not sep:
        return [text]
    chunks, current, lots = [], [], 0
    for block in body.split("\nPart block: "):
        n = _lot_count(block)
        if current and lots + n > max_lots:
            chunks.append(current)
            current, lots = [], 0
        current.append("Part block: " + block)
        lots += n
    chunks.append(current)
    return [head + "\n" + "\n".join(c) for c in chunks]


def _note(text: str) -> str:
    """Hover text as 'Label: value'. The Trace/Tag hover shows "Trace: X", and
    sometimes "Tag Details: Y" under it (2026-10-29); a line with no label of its
    own is named so the prompt can place it."""
    label, sep, value = text.partition(":")
    if sep and label.strip() and len(label) <= 30:
        return f"{label.strip()}: {value.strip()}"
    return f"Trace/Tag note: {text.strip()}"


def _qty(value: Optional[str]) -> Optional[int]:
    text = (value or "").replace(",", "").strip()
    return int(text) if text.isdigit() else None


def _join(values: list[str]) -> str:
    return ", ".join(dict.fromkeys(v for v in values if v))


async def _block_open(item) -> bool:
    """The block's grid is drawn (DASI adds its table only when it is opened)."""
    return await item.evaluate("it => !!it.querySelector('table')")


async def _wait_open(page, item) -> bool:
    for _ in range(OPEN_WAIT_MS // 250):
        if await _block_open(item):
            return True
        await page.wait_for_timeout(250)
    return False


async def _visible_dialog(page):
    dialogs = page.locator(DIALOG)
    for j in range(await dialogs.count()):
        if await dialogs.nth(j).is_visible():
            return dialogs.nth(j)
    return None


def _dump(label: str, text: str) -> None:
    """With DEBUG_DUMP_PAGES=1, save the whole text the LLM reads (page, filter
    note and View details) next to page_text's own dump of the page."""
    if os.environ.get("DEBUG_DUMP_PAGES", "").strip() not in ("1", "true", "yes"):
        return
    try:
        web.DUMP_DIR.mkdir(exist_ok=True)
        path = web.DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-dasi-read.txt"
        path.write_text(text, encoding="utf-8")
        logger.info("Saved the text read for %s to %s", label, path)
    except Exception as exc:
        logger.warning("Could not save the text read: %r", exc)
