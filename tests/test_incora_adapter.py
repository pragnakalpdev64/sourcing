"""IncoraAdapter against a mock of shop.incora.com, built from the team's
screenshots and the site's script bundle (2026-11-05): the app sends a browser
without a token to B2C's sign-in (login.incora.com), keeps its OAuth tokens in
localStorage, the Bulk Product Search page reads its parts from localStorage,
POSTs them to app.hw2.../api/Customer/stocks/bulk and saves the whole result in
localStorage; each stock line's $ pop-up loads .../Customer/<customer>/stocks/<spec>.
Skipped if no local Chromium."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from sourcing.credentials.base import VendorCredential
from sourcing.models import LlmUsage, Offer, RequestRow
from sourcing.vendors import adapter_class
from sourcing.vendors.base import BulkResultsIncomplete, InvalidCredentials
from sourcing.vendors.incora import (
    RESULTS_KEY, SEARCH_CODES_KEY, IncoraAdapter, _record_chunks, break_rows, search_codes,
)
from sourcing.vendors.incora_prompt import INCORA_EXTRACT_SYSTEM

SHOP = "https://shop.incora.com"
API = "https://app.hw2.shop.incora.com/api/Customer"
B2C = "https://login.incora.com"
CORS = {"access-control-allow-origin": SHOP, "access-control-allow-headers": "*",
        "access-control-allow-methods": "GET, POST, OPTIONS",
        "access-control-allow-credentials": "true"}

CERTS = "Mfg's C of C, Test Report, Process Certs & Separate Raw Material"
LINE = dict(site="Northlake", manufacturerName="Allfast Fastening Systems Inc",
            certificateDescription=CERTS, unitOfMeasure="EA", shelfLifeDays=None)
# As in the team's screenshot: two Northlake lines of MS20470AD4-4 (revision 2 / 3).
DATA = {
    "MS20470AD4-4": dict(itemDescription="SOLID RIVET,UNIV HD,206 2117AL", locations=[
        dict(LINE, revision="2", quantityAvailable=207169, itemKey="I1", itemSpecKey="S1"),
        dict(LINE, revision="3", quantityAvailable=326148, itemKey="I1", itemSpecKey="S2")]),
    "NAS1097AD4-4": dict(itemDescription="SOLID RIVET,100 SHR,206 2117AL", locations=[
        dict(LINE, revision="", quantityAvailable=0, itemKey="I2", itemSpecKey="S3",
             shelfLifeDays=720)]),
}
BREAKS = [dict(quantity=q, price=p, currencyCode="USD")
          for q, p in ((1, 25), (25, 1), (50, 0.5), (10000, 0.05))]
PRICES = {"S1": BREAKS, "S2": BREAKS, "S3": []}

HOME = """<html><body><script>
if (!localStorage.getItem('access_token'))
  location.replace('https://login.incora.com/tfp/x/authorize?prompt=login');
</script><div>Skylink Inc, 2800 S Financial Ct</div><h1>Home</h1>
<div>Shop</div><div>Flash Sale</div><div>Auctions</div><div>Bulk Search</div>
<div>Order History</div><div>View Invoices</div><div>0 items</div></body></html>"""
AUTH_REDIRECT = """<html><body><script>
localStorage.setItem('access_token', 'AT-1');
localStorage.setItem('id_token', 'ID-1');
localStorage.setItem('expires_at', String(Date.now() + 1800000));
setTimeout(() => location.replace('/home'), 300);
</script></body></html>"""
# The results page: parts from localStorage, the bulk POST, the result saved, the
# table's first page and its paginator (page size 10).
BULK = """<html><head><meta charset="utf-8"></head><body><div>0 items</div><button>Add To Cart</button>
<div id="t" class="mf-table"></div><div class="mat-mdc-paginator-range-label" id="range"></div>
<button class="mat-mdc-paginator-navigation-next" id="next">Next page</button>
<style>svg{display:inline-block;width:14px;height:14px} .mf-table-row{padding:4px}</style><script>
const API = '%(api)s', TOKEN = () => localStorage.getItem('access_token');
const ICON = n => `<svg data-icon="${n}" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>`;
const codes = JSON.parse(localStorage.getItem('%(codes)s') || '[]');
let list = [], page = 0;
function priceOverlay(btn, loc) {
  const old = document.querySelector('.ecom-price-overlay');
  if (old) { old.remove(); return; }
  fetch(API + '/CUST1/stocks/' + loc.itemSpecKey, {headers: {Authorization: 'Bearer ' + TOKEN()}})
   .then(r => r.json()).then(d => {
    const o = document.createElement('div'); o.className = 'ecom-price-overlay';
    o.innerHTML = '<table><tr><th>Qty</th><th>Price</th></tr>' + d.priceBreaks.map(b =>
      `<tr><td>${b.quantity}</td><td>${(b.price * %(shift)s).toFixed(4)} ${b.currencyCode}</td></tr>`).join('') + '</table>';
    document.body.appendChild(o); });
}
function openPart(item, row) {
  const old = document.getElementById('nested'); if (old) old.remove();
  const t = document.createElement('mf-table'); t.id = 'nested';
  t.innerHTML = '<div>Order site Manufacturer Certification Description Revision Shelf Life UOM Qty Available Price</div>';
  item.locations.forEach(l => {
    const d = document.createElement('div');
    d.innerHTML = `${l.site} ${l.manufacturerName} ${l.certificateDescription} ${l.revision} ${l.unitOfMeasure} ${l.quantityAvailable.toLocaleString('en-US')} `;
    const b = document.createElement('button'); b.innerHTML = ICON('dollar-sign');
    b.onclick = e => { e.stopPropagation(); priceOverlay(b, l); };
    d.appendChild(b); t.appendChild(d); });
  row.after(t);
}
function render() {
  const t = document.getElementById('t'); t.innerHTML = '';
  list.slice(page * 10, page * 10 + 10).forEach(i => {
    const r = document.createElement('div'); r.className = 'mf-table-row';
    r.innerHTML = `<span>${i.itemCode}</span> <span>${i.itemDescription || ''}</span> `
      + `<span mattooltip="${i.stockFound ? 'Stock Found' : 'Stock Not Found'}">${ICON(i.stockFound ? 'check' : 'xmark')}</span>`;
    if (i.stockFound) r.onclick = () => openPart(i, r);
    t.appendChild(r); });
  const shown = list.length + %(extra)s, last = Math.min(page * 10 + 10, shown);
  document.getElementById('range').innerText = (page * 10 + 1) + ' – ' + last + ' of ' + shown;
  document.getElementById('next').disabled = last >= shown;
}
document.getElementById('next').onclick = () => { page += 1; render(); };
fetch(API + '/stocks/bulk', {method: 'POST', headers: {'Content-Type': 'application/json',
  Authorization: 'Bearer ' + TOKEN()}, body: JSON.stringify({key: 'CUST1', searchValues: codes})})
 .then(r => r.json()).then(l => {
  list = l;
  codes.forEach(c => { if (!list.find(i => i.itemCode.toUpperCase() === c.code.toUpperCase()))
    list.push({itemCode: c.code, stockFound: false, hideExpandedAction: true}); });
  if (%(store)s) localStorage.setItem('%(results)s', JSON.stringify(list));
  render();
 });
</script></body></html>"""
LOGIN = """<html><body><h2>Returning Customer Sign In</h2><div class="error pageLevel" id="err"></div>
<label>Email Address</label><input type="email" id="email">
<label>Password</label><input type="password" id="password">
<button id="next" type="submit">SIGN IN</button><a href="#">Forgot your password?</a>
<script>document.getElementById('next').onclick = () => {
  if (document.getElementById('password').value === 'good')
    location.replace('https://shop.incora.com/auth-redirect?code=c1');
  else document.getElementById('err').innerText = 'Your password is incorrect.';
};</script></body></html>"""


class MockIncora:
    def __init__(self):
        self.bulk_posts: list[dict] = []
        self.price_gets: list[tuple[str, str]] = []
        self.failing_specs: set[str] = set()
        self.extra_rows = 0             # parts the table counts beyond the data
        self.popup_shift = 1            # 2: the $ pop-up shows other prices than the data
        self.store_result = True        # False: the page keeps no stored result
        self.logouts = 0
        self.cart_calls = 0

    async def shop(self, route):
        path = urlparse(route.request.url).path
        html = lambda body: route.fulfill(status=200, content_type="text/html", body=body)  # noqa: E731
        if path in ("/", "/home"):
            await html(HOME)
        elif path == "/auth-redirect":
            await html(AUTH_REDIRECT)
        elif path == "/shop/products/bulk-search":
            await html(BULK % {"codes": SEARCH_CODES_KEY, "results": RESULTS_KEY, "api": API,
                               "extra": self.extra_rows, "shift": self.popup_shift,
                               "store": "true" if self.store_result else "false"})
        elif path == "/logged-out":
            await html("<html><body><p>You have been logged out</p></body></html>")
        else:
            await html("<html><body>other</body></html>")

    async def api(self, route):
        req = route.request
        path = urlparse(req.url).path
        if req.method == "OPTIONS":
            await route.fulfill(status=204, headers=CORS)
            return
        if "cart" in path.lower():
            self.cart_calls += 1
        auth = (await req.all_headers()).get("authorization", "")
        if auth != "Bearer AT-1":
            await route.fulfill(status=401, headers=CORS, body="")
            return
        if path.endswith("/stocks/bulk") and req.method == "POST":
            body = json.loads(req.post_data or "{}")
            self.bulk_posts.append(body)
            items = []
            for c in body["searchValues"]:
                d = DATA.get(c["code"].upper())
                if d:
                    items.append(dict(d, itemCode=c["code"].upper(), stockFound=True,
                                      searchQuantity=c.get("qty")))
                elif c["code"].upper().startswith("FILL-"):
                    items.append(dict(itemCode=c["code"], stockFound=True, itemDescription="F",
                                      searchQuantity=c.get("qty"), locations=[]))
            await route.fulfill(status=200, headers=CORS, content_type="application/json",
                                body=json.dumps(items))
            return
        m = re.search(r"/Customer/([^/]+)/stocks/([^/]+)$", path)
        if m and req.method == "GET":
            customer, spec = unquote(m.group(1)), unquote(m.group(2))
            self.price_gets.append((customer, spec))
            if spec in self.failing_specs:
                await route.fulfill(status=500, headers=CORS, body="")
                return
            await route.fulfill(status=200, headers=CORS, content_type="application/json",
                                body=json.dumps({"itemSpecKey": spec,
                                                 "priceBreaks": PRICES.get(spec, [])}))
            return
        await route.fulfill(status=404, headers=CORS, body="")

    async def b2c(self, route):
        url = urlparse(route.request.url)
        if url.path.endswith("/logout"):
            self.logouts += 1
            back = parse_qs(url.query)["post_logout_redirect_uri"][0]
            await route.fulfill(status=200, content_type="text/html",
                                body=f"<script>location.replace({back!r})</script>")
            return
        await route.fulfill(status=200, content_type="text/html", body=LOGIN)


class Reader:
    """Stands in for the LLM: one offer per stock line, with a made-up price and
    stock the program must replace with what the page states."""

    def __init__(self):
        self.texts: list[str] = []
        self.systems: list[str] = []

    async def extract_offers_bulk(self, *, page_text, page_url, parts, system=""):
        self.texts.append(page_text)
        self.systems.append(system)
        wanted = {p["part_number"].upper(): p["part_number"] for p in parts}
        found: dict[str, list] = {}
        for part, link in re.findall(r"^(\S+) line \d+: Link: (\S+)", page_text, re.M):
            offer = Offer(part, "9.99", "EUR", "5", None, "3 days", link)
            found.setdefault(wanted.get(part.upper(), part), []).append(offer)
        return "results", found, LlmUsage(calls=1)


def row(pn, qty="", n=2):
    return RequestRow(sheet_row=n, rfq_id="R1", vendor_raw="Incora", part_number=pn,
                      qty_needed=qty)


def cred(password="good"):
    return VendorCredential("incora", "", "sourcing-vendor-incora", "buyer@skylink.example",
                            password, None, url=f"{SHOP}/")


@pytest_asyncio.fixture
async def incora():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"No local Chromium: {exc}")
    portal = MockIncora()
    context = await browser.new_context()
    await context.route(f"{SHOP}/**", portal.shop)
    await context.route(f"{API}/**", portal.api)
    await context.route(f"{B2C}/**", portal.b2c)
    page = await context.new_page()
    yield page, portal
    await browser.close()
    await pw.stop()


async def signed_in(page):
    await page.goto(f"{SHOP}/auth-redirect?code=c1")
    await page.wait_for_url(f"{SHOP}/home")


def test_registered_for_the_sheet_and_records():
    for name in ("incora", "wesco", "wesco_aircraft"):
        assert adapter_class(name) is IncoraAdapter
    assert adapter_class("x", {"adapter": "incora"}) is IncoraAdapter
    assert IncoraAdapter().default_login_url == f"{SHOP}/"


def test_search_codes_are_the_dialogs_lines():
    rows = [row("MS20470AD4-4", "10"), row(" AN960-10 ", ""), row("NAS6604-6", "2.5")]
    assert search_codes(rows) == [{"code": "MS20470AD4-4", "qty": 10}, {"code": "AN960-10"},
                                  {"code": "NAS6604-6"}]      # the dialog takes whole numbers


def test_price_breaks_become_ranges():
    assert break_rows(BREAKS) == [("1", "24", "25.0000"), ("25", "49", "1.0000"),
                                  ("50", "9999", "0.5000"), ("10000", "", "0.0500")]
    assert break_rows([]) == []
    assert break_rows([{"quantity": 5, "price": 2}])[0] == ("5", "", "2.0000")


def test_record_chunks_keep_part_blocks_whole():
    text = "--- note ---\nPart block: A\nA line 1: x\nA line 2: x\nPart block: B\nB: no stock lines" \
           "\nPart block: C\nC line 1: x"
    chunks = _record_chunks(text, 2)
    assert len(chunks) == 2 and all(c.startswith("--- note ---") for c in chunks)
    assert "A line 2" in chunks[0] and "B: no stock lines" in chunks[0] and "C line 1" in chunks[1]


@pytest.mark.asyncio
async def test_bulk_search_reads_every_stock_line_with_its_price_breaks(incora):
    page, portal = incora
    await signed_in(page)
    reader = Reader()
    adapter = IncoraAdapter(extractor=reader)
    rows = [row("MS20470AD4-4", "30", 2), row("NAS1097AD4-4", "", 3), row("MS20426AD4-6", "5", 4)]
    outcomes, usage = await adapter.search_many(page, rows)

    # The page's own search: the parts with their quantities, Skylink's customer.
    assert portal.bulk_posts == [{"key": "CUST1", "searchValues": [
        {"code": "MS20470AD4-4", "qty": 30}, {"code": "NAS1097AD4-4"},
        {"code": "MS20426AD4-6", "qty": 5}]}]
    # Each line's prices once, plus the $ pop-up the page check opens (S1 again).
    assert sorted(set(portal.price_gets)) == [("CUST1", "S1"), ("CUST1", "S2"), ("CUST1", "S3")]
    text = reader.texts[0]
    assert reader.systems[0] == INCORA_EXTRACT_SYSTEM
    assert "Part block: MS20470AD4-4 | Description: SOLID RIVET,UNIV HD,206 2117AL" in text
    assert "MS20470AD4-4 line 2: Link: https://shop.incora.com/shop/products/detail/I1/S2" in text
    assert f"Certification Description: {CERTS}" in text and "Revision: 3" in text
    assert "Price breaks (Qty: Price): 1: 25.0000 USD; 25: 1.0000 USD" in text
    # The app's own placeholder for a code the search didn't return (no Search Qty).
    assert "Part block: MS20426AD4-6 | Result: Stock Not Found" in text
    assert "Skylink Inc" not in text and "Add To Cart" not in text

    rivet = outcomes[rows[0].row_id].offers
    assert len(rivet) == 2
    first = rivet[0]
    # What the page states, not the LLM's made-up values.
    assert (first.price, first.currency, first.qty_available, first.uom) == (
        "25.0000", "USD", "207169", "EA")
    assert first.price_breaks[:2] == [("1", "24", "25.0000"), ("25", "49", "1.0000")]
    assert first.lead_time == "In stock" and first.condition == "NE"
    assert "Warehouse: Northlake" in first.warehouses and "Qty on hand: 207169" in first.warehouses
    assert "MANUFACTURER: ALLFAST FASTENING SYSTEMS INC" in first.vendor_note
    assert "REVISION: 2" in first.vendor_note and rivet[1].qty_available == "326148"

    # No stock and no price: out of stock, no price, shelf life noted.
    nas = outcomes[rows[1].row_id].offers[0]
    assert nas.qty_available == "Out of stock" and nas.price is None and nas.price_breaks == []
    assert nas.lead_time is None and "SHELF LIFE: 720 DAYS" in nas.vendor_note
    # Incora's own "Stock Not Found", signed in: not searched again one by one.
    missing = outcomes[rows[2].row_id]
    assert not missing.found and missing.page_kind == "no_results"
    assert usage.calls == 1 and portal.cart_calls == 0


@pytest.mark.asyncio
async def test_every_part_is_read_not_only_the_first_table_page(incora):
    page, portal = incora
    await signed_in(page)
    reader = Reader()
    rows = [row("MS20470AD4-4", "", 2)] + [row(f"FILL-{i}", "", i + 3) for i in range(14)]
    outcomes, _ = await IncoraAdapter(extractor=reader).search_many(page, rows)
    text = reader.texts[0]
    # The table shows 10 per page; the page's data has all 15.
    assert await page.evaluate("() => document.getElementById('range').innerText") == "1 – 10 of 15"
    assert all(f"Part block: FILL-{i}" in text for i in range(14))
    assert outcomes[rows[0].row_id].found


@pytest.mark.asyncio
async def test_table_counting_more_parts_than_the_data_is_incomplete(incora):
    page, portal = incora
    await signed_in(page)
    portal.extra_rows = 3
    with pytest.raises(BulkResultsIncomplete):
        await IncoraAdapter(extractor=Reader()).search_many(page, [row("MS20470AD4-4"),
                                                                  row("NAS1097AD4-4", n=3)])


@pytest.mark.asyncio
async def test_price_breaks_not_read_are_flagged_never_no_price(incora):
    page, portal = incora
    await signed_in(page)
    portal.failing_specs = {"S2"}
    outcome = await IncoraAdapter(extractor=Reader()).search(page, row("MS20470AD4-4"))
    ok, failed = outcome.offers
    assert ok.price == "25.0000" and not (ok.review or "")
    assert failed.price is None and "PRICE_BREAKS_NOT_READ" in failed.review


@pytest.mark.asyncio
async def test_sign_in_through_b2c_then_logout(incora):
    page, portal = incora
    adapter = IncoraAdapter(extractor=Reader())
    assert await adapter.is_authenticated(page) is False        # /home -> B2C
    await adapter.login(page, cred())
    assert adapter.signed_in_proof and page.url.startswith(SHOP)
    assert await adapter.is_authenticated(page) is True
    assert await adapter.logout(page) is True
    assert portal.logouts == 1
    assert await page.evaluate("() => localStorage.getItem('access_token')") is None
    assert await adapter.is_authenticated(page) is False


@pytest.mark.asyncio
async def test_wrong_password_is_a_credentials_error(incora):
    page, _ = incora
    with pytest.raises(InvalidCredentials):
        await IncoraAdapter(extractor=Reader()).login(page, cred("bad"))


def test_login_starts_on_the_shop_never_a_b2c_address():
    # Run 2026-11-05: the secret's url was the bare B2C host, and the B2C address
    # reached at 16:39 was saved and opened again at 16:46 (one sign-in each).
    saved = ("https://login.incora.com/a42f/b2c_1a_signup_signin_oauth_30m/oauth2/v2.0/"
             "authorize?state=old&code_challenge=old")
    adapter = IncoraAdapter(selectors={"login_page_url": saved})
    secret = VendorCredential("incora", "", "sourcing-vendor-incora", "a@b.c", "pw", None,
                              url="https://login.incora.com/")
    assert adapter.login_url_candidates(secret) == [("adapter default", f"{SHOP}/")]
    assert adapter.login_url_candidates(cred())[0][1] == f"{SHOP}/"
    adapter._remember_login_page(None, f"{SHOP}/")                # never saved
    assert "login_page_url" not in adapter.learned


def test_records_from_the_first_live_run():
    """Shapes seen 2026-11-05 16:39: Shelf Life 0 on every line, empty price lists
    (AN310-4), pricingUnitOfMeasure empty (set here to a hundred for the flag)."""
    adapter = IncoraAdapter()
    base = dict(site="Northlake", manufacturerName="SPS Technologies (Jenk)",
                certificateDescription="Mfr's C of C, Test Report", revision="2",
                shelfLifeDays=0, unitOfMeasure="EA", itemKey="K", pricingUnitOfMeasure="")
    items = [dict(itemCode="AN310-4", stockFound=True, itemDescription="PLAIN CASTLE HEX NUT",
                  locations=[dict(base, quantityAvailable=7566, itemSpecKey="A", _breaks=[],
                                  _breaks_error=None),
                             dict(base, quantityAvailable=50, itemSpecKey="B",
                                  pricingUnitOfMeasure="C", _breaks_error=None,
                                  _breaks=[dict(quantity=1, price=4, currencyCode="USD")])])]
    text = adapter._records_text(items, [row("AN310-4")])
    assert "Shelf Life" not in text and "Pricing UOM: C" in text
    assert "AN310-4 line 1:" in text and "Price breaks: none shown (no price)" in text
    no_price, per_hundred = adapter._facts.values()
    offer = Offer("AN310-4", "1.00", "USD", "7566", None, None, f"{SHOP}/shop/products/detail/K/A")
    adapter._match_page(offer, no_price)
    assert offer.price is None and offer.currency is None and offer.price_breaks == []
    assert "NO PRICE SHOWN ON INCORA (REQUEST A QUOTE)" in offer.vendor_note
    assert "SHELF LIFE" not in offer.vendor_note
    other = Offer("AN310-4", "4.0000", "USD", "50", None, None, f"{SHOP}/shop/products/detail/K/B")
    adapter._match_page(other, per_hundred)
    assert "PRICING_UOM_DIFFERS" in other.review and "PRICED PER C, STOCK IN EA" in other.vendor_note


@pytest.mark.asyncio
async def test_page_check_opens_a_part_clicks_its_dollar_and_hovers_the_cross(incora, caplog):
    page, _ = incora
    await signed_in(page)
    caplog.set_level("INFO", logger="sourcing.vendors.incora")
    outcomes, _ = await IncoraAdapter(extractor=Reader()).search_many(
        page, [row("MS20470AD4-4", "", 2), row("MS20426AD4-6", "", 3)])
    check = next(r.message for r in caplog.records if r.message.startswith("Incora page check:"))
    assert "MS20470AD4-4 opened, qty = data, $ pop-up 4 row(s) = data" in check
    assert "× says 'Stock Not Found'" in check
    assert not any("PAGE_CHECK_MISMATCH" in (o.review or "")
                   for o in outcomes[row("MS20470AD4-4", "", 2).row_id].offers)


@pytest.mark.asyncio
async def test_page_showing_other_prices_than_the_data_is_flagged(incora):
    page, portal = incora
    await signed_in(page)
    portal.popup_shift = 2
    outcome = await IncoraAdapter(extractor=Reader()).search(page, row("MS20470AD4-4"))
    assert all("PAGE_CHECK_MISMATCH" in o.review for o in outcome.offers)


@pytest.mark.asyncio
async def test_without_stored_data_every_table_page_is_clicked_through(incora, monkeypatch):
    from sourcing.vendors import incora as incora_module
    page, portal = incora
    await signed_in(page)
    portal.store_result = False
    monkeypatch.setattr(incora_module, "RESULTS_WAIT_SECONDS", 2)
    reader = Reader()
    rows = [row(f"FILL-{i}", "", i + 2) for i in range(10)] + [row("MS20470AD4-4", "", 20)]
    await IncoraAdapter(extractor=reader).search_many(page, rows)
    text = reader.texts[0]
    # 11 parts: the rivet is on the table's second page.
    assert "page 2 of the table" in text and "Stock lines of MS20470AD4-4" in text
    assert "207,169" in text and "326,148" in text
    assert "Stock line 2 price breaks (Qty: Price): 1: 25.0000 USD; 25: 1.0000 USD" in text


def test_trace_is_the_certification_wording_shortened_by_the_program():
    from sourcing.vendors.incora import cert_trace
    # The wordings of run 2026-11-05; at most 50 characters, Process Certs dropped first.
    assert cert_trace("Mfg's C of C, Test Report, Process Certs & Separate Raw Material") == \
        "MFR C of C, Test Report, Raw Material Certs"
    assert cert_trace("Mfr's C of C, Test Report, Raw Material Certs & Process Certs, DFARS") == \
        "MFR C of C, Test Report, Raw Material Certs, DFARS"
    assert cert_trace("Manufacturers C of C") == "MFR C of C"
    assert cert_trace("") is None
    adapter = IncoraAdapter()
    item = dict(itemCode="X-1", stockFound=True, locations=[dict(
        site="Northlake", certificateDescription="Mfg's C of C, Test Report", unitOfMeasure="EA",
        quantityAvailable=5, itemKey="K", itemSpecKey="S", _breaks=[], _breaks_error=None)])
    adapter._records_text([item], [row("X-1")])
    offer = Offer("X-1", None, None, "5", None, None, f"{SHOP}/shop/products/detail/K/S")
    offer.review = "TRACE_NOT_ON_PAGE"           # the LLM's shortened trace, removed
    adapter._match_page(offer, adapter._facts[offer.source_url])
    assert offer.trace == "MFR C of C, Test Report" and not offer.review


def test_note_when_a_larger_break_costs_less_in_total():
    from sourcing.vendors.incora import cheaper_break_note
    breaks = break_rows([dict(quantity=q, price=p) for q, p in
                         ((1, 25), (25, 1), (50, 0.5), (100, 0.25), (10000, 0.05))])
    # MS20470AD4-4, 10 asked (run 2026-11-05): $25.0000 each stays the Unit Price.
    assert cheaper_break_note(breaks, "10") == \
        "25+ AT $1.0000 EACH: $25.00 FOR 25, LESS THAN $250.00 FOR 10"
    assert cheaper_break_note(breaks, "100") is None          # MS24665-132: already cheapest
    assert cheaper_break_note(breaks, "") is None        # 1 at $25 = 25 at $1: not less
    assert cheaper_break_note([("1", "", "0.0200")], "1") is None
