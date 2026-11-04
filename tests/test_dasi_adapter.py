"""DasiAdapter against a mock of DASI's results page (as seen 2026-10-29: part
blocks closed until their + is clicked, a hidden "Fulfilled by" select under
DASI's own list, a View link per row). The opened rows and the View dialog are
invented: the live ones haven't been saved yet. Skipped if no local Chromium."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.extract import EXTRACT_SYSTEM
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.dasi import FILTER_NOTE, DasiAdapter
from sourcing.vendors.dasi_prompt import DASI_EXTRACT_SYSTEM
from sourcing.vendors.generic import GenericAdapter

RESULTS = """<html><body><button aria-label="User Profile">CARRIE</button>
<label>Fulfilled by</label>
<select name="stock-source" tabindex="-1" style="display:none" onchange="window.src = this.value; redraw();">
<option value="all">All</option><option value="third-party-fulfillment">Third Party Fulfillment</option>
<option value="dasi-only">DASI Only</option></select>
<fieldset><legend>Location</legend><div role="location-picklist">
<div role="button" class="cmpl-input" onclick="document.getElementById('locs').style.display = 'block'"></div>
<ul id="locs" style="display:none">{locations}</ul></div></fieldset>
<div>{n} of {n} ITEMS</div><div id="list"></div>
<script>
// The blocks sit in a shadow root: like DASI's Salesforce components (run 12), a
// query from `document` doesn't see them; Playwright and a query that starts at
// a block do.
const root = document.getElementById('list').attachShadow({{mode: 'open'}});
root.innerHTML = {blocks};
// Cond, Ship From, Lead, Trace/Tag, trace source, tag details, Qty, MOQ, Price, fulfilled by
const LOTS = [["NS", "DASI Miami HQ (FL)", "0", "FAA 129", "CHINA SOUTHERN AIRLINES", "OEM C OF C ONLY",
               "138", "134", "$ 0.15", "DASI"],
              ["NS", "Plant City (FL)", "1", "FAA 121", "REPUBLIC AIRWAYS", "", "30", "30", "$ 0.67", "DASI"],
              ["NE", "DASI Miami HQ (FL)", "0", "OEM", "", "", "50", "1", "$ 4.00", "Other Co"]];
function rows() {{
  return LOTS.filter(l => window.src !== 'dasi-only' || l[9] === 'DASI')
    .filter(l => !window.loc || l[1] === window.loc)
    .map(l => '<tr><td><lightning-icon><div role="tooltip">Documents Available</div></lightning-icon></td>' +
      '<td><button>View</button></td><td>' + l[0] + '</td><td>' + l[1] + '</td><td>' + l[2] +
      '</td><td><span>' + l[3] + '</span><div role="tooltip"><p>Trace: ' + l[4] + '</p>' +
      (l[5] ? '<p>Tag Details: ' + l[5] + '</p>' : '') + '</div></td><td>EA</td><td>' + l[6] +
      '</td><td>' + l[7] + '</td><td>' + l[8] + '</td><td><input type="number"></td>' +
      '<td><button>Add to Cart</button></td></tr>').join('');
}}
const HEAD = '<thead><tr><th></th><th>Action</th><th>Cond</th><th>Ship From</th><th>Lead (Days)</th>' +
  '<th>Trace/Tag</th><th>UOM</th><th>Qty Avail</th><th>MOQ</th><th>Price</th><th>Cart Qty</th>' +
  '<th>Select Item</th></tr></thead>';
function toggle(el) {{
  const item = el.closest('.result-item');
  const open = item.querySelector('.details');
  if (open) {{ open.remove(); return; }}
  const d = document.createElement('div'); d.className = 'details';
  d.innerHTML = '<table>' + HEAD + '<tbody>' + rows() + '</tbody></table>';
  item.appendChild(d);
}}
function redraw() {{ root.querySelectorAll('.result-item .details').forEach(d => d.remove()); }}
function pick(where) {{ window.loc = where; document.getElementById('locs').style.display = 'none'; redraw(); }}
</script></body></html>"""
# The + sits under a cover, so a normal click on it times out (as on DASI, run 12);
# a click on the part number opens the block.
BLOCK = """<div class="result-item"><div class="header" style="position:relative">
<span class="item-info" onclick="toggle(this)">{pn} (Quantity: 218) | RIVET</span>
<button title="Expand/Collapse" onclick="toggle(this)">+</button>
<div style="position:absolute; right:0; top:0; width:60px; height:40px"></div></div></div>"""
LOCATIONS = """<li onclick="pick('DASI Miami HQ (FL)')">DASI Miami HQ (FL)</li>
<li onclick="pick('Plant City (FL)')">Plant City (FL)</li>"""


class MockDasi:
    def __init__(self, locations=LOCATIONS):
        self.gets: list[str] = []
        self.locations = locations

    async def handle(self, route):
        url = urlparse(route.request.url)
        self.gets.append(f"{url.path}?{url.query}")
        q = parse_qs(url.query)
        parts = q.get("product", []) if q.get("type") == ["multiple"] else q.get("searchterms", [])
        body = RESULTS.format(n=len(parts),
                              blocks=json.dumps("".join(BLOCK.format(pn=p) for p in parts)),
                              locations=self.locations)
        await route.fulfill(status=200, content_type="text/html", body=body)


class Reader:
    """Stands in for Extractor: keeps what it was given, finds one offer per part."""

    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []

    async def extract_offers(self, *, page_text, page_url, part_number, system="", **_):
        self.texts.append(page_text)
        self.systems.append(system)
        return True, "results", [Offer(part_number, "5.00", "USD", "100", "NE", None,
                                       page_url)], LlmUsage(calls=1)

    async def extract_offers_bulk(self, *, page_text, page_url, parts, system=""):
        self.texts.append(page_text)
        self.systems.append(system)
        return "results", {p["part_number"]: [Offer(p["part_number"], "5.00", "USD", "100",
                                                    "NE", None, page_url)]
                           for p in parts}, LlmUsage(calls=1)


def row(pn, n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="DASI", part_number=pn)


@pytest_asyncio.fixture
async def dasi_page():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockDasi()
    page = await browser.new_page()
    await page.route("https://store.dasi.com/**", portal.handle)
    yield page, portal
    await browser.close()
    await pw.stop()


MIAMI_LOT = ("MS20470AD4-3 lot 1: Cond: NS | Ship From: DASI Miami HQ (FL) | Lead (Days): 0 | "
             "Trace/Tag: FAA 129 | UOM: EA | Qty Avail: 138 | MOQ: 134 | Price: $ 0.15 | "
             "Trace: CHINA SOUTHERN AIRLINES | Tag Details: OEM C OF C ONLY")


@pytest.mark.asyncio
async def test_one_part_read_as_records_of_dasi_miami_lots(dasi_page):
    page, portal = dasi_page
    reader = Reader()
    outcome = await DasiAdapter(extractor=reader).search(page, row("MS20470AD4-3"))
    assert outcome.found
    assert portal.gets[0] == "/product-search?type=simple&searchterms=MS20470AD4-3&exact=true"
    text = reader.texts[0]
    assert reader.systems == [DASI_EXTRACT_SYSTEM]
    assert text.startswith(FILTER_NOTE) and MIAMI_LOT in text
    assert "lot 2" not in text                    # Plant City and third-party lots left out
    assert "View" not in text and "Add to Cart" not in text and "Documents Available" not in text


@pytest.mark.asyncio
async def test_lots_from_other_warehouses_are_left_out_without_the_location_filter(dasi_page):
    page, portal = dasi_page
    portal.locations = ""                         # the filter has no options: code filter only
    reader = Reader()
    assert (await DasiAdapter(extractor=reader).search(page, row("MS20470AD4-3"))).found
    assert MIAMI_LOT in reader.texts[0] and "Plant City" not in reader.texts[0]


@pytest.mark.asyncio
async def test_bulk_search_uses_the_multiple_address(dasi_page):
    page, portal = dasi_page
    reader = Reader()
    rows = [row("MS20470AD5-4", 2), row("NAS1149C0863R", 3)]
    outcomes, _ = await DasiAdapter(extractor=reader).search_many(page, rows)
    assert portal.gets[0] == ("/product-search?type=multiple&exact=true"
                              "&product=MS20470AD5-4&product=NAS1149C0863R")
    assert set(outcomes) == {r.row_id for r in rows}
    assert reader.systems == [DASI_EXTRACT_SYSTEM]
    text = reader.texts[0]
    assert "MS20470AD5-4 lot 1: Cond: NS" in text and "NAS1149C0863R lot 1: Cond: NS" in text


def test_dasi_prompt_is_the_shared_rules_plus_its_own_section():
    assert DASI_EXTRACT_SYSTEM.startswith(EXTRACT_SYSTEM.rstrip())
    assert "## 5. DASI Pages" in DASI_EXTRACT_SYSTEM and FILTER_NOTE in DASI_EXTRACT_SYSTEM


def test_dasi_gets_its_adapter_even_with_an_auto_made_generic_record():
    auto = {"adapter": "generic", "auto_created_from": "sourcing-vendor-dasi"}
    assert adapter_class("dasi", auto) is DasiAdapter
    assert adapter_class("dasi") is DasiAdapter
    # A person who set the record to generic on purpose keeps it.
    assert adapter_class("dasi", {"adapter": "generic"}) is GenericAdapter


def test_bulk_records_are_read_in_pieces_of_whole_part_blocks():
    from sourcing.vendors.dasi import _record_chunks
    head = "--- Filter: Fulfilled by = DASI Only (set by the program) ---\n\n--- DASI results ---\n"
    block = lambda pn, n: (f"\nPart block: {pn} (Quantity: 9) | RIVET\n"
                           + "\n".join(f"{pn} lot {i}: Cond: NS" for i in range(1, n + 1)))
    text = head + block("A-1", 12) + block("B-2", 6) + block("C-3", 9)
    chunks = _record_chunks(text, 20)
    assert len(chunks) == 2
    assert all(c.startswith(head) for c in chunks)
    assert "A-1 lot 12" in chunks[0] and "B-2 lot 6" in chunks[0] and "C-3" not in chunks[0]
    assert "C-3 lot 9" in chunks[1] and "A-1" not in chunks[1]
    assert _record_chunks("No Results Found", 20) == ["No Results Found"]
