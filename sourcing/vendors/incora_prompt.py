"""Incora's own extraction prompt (shop.incora.com, vendors/incora.py).

The client's field rules are the shared prompt's, so a rule change reaches every
vendor; what Incora's pages need on top is the section below, which wins where
the two differ. Written from the team's screenshots (2026-11-05): the Bulk
Product Search table (Part, Description, Search Qty, Order Qty, Selected,
Result ✓ / ×), a found part's stock lines (Order, site, Manufacturer,
Certification Description, Revision, Shelf Life, UOM, Qty Available, Price = a $
button, Order Qty), the $ pop-up (Qty / Price: 1 25.0000 USD, 25 1.0000 USD, ...)
and the product page (the same price table, Add To Cart, Request A Quote, a stock
table by Manufacturer / Stock Location). No condition, lead time or minimum order
seen. Checked against the first live run's records (2026-11-05 16:39, 17 parts, 14
stock lines: certification wordings "Mfg's C of C ...", "Mfr's C of C ...",
"Manufacturers C of C"; 6 lines with an empty price list; sites Northlake and
Wichita); --dump-pages saves debug_pages/*-incora-*-read.txt and the raw data.
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

INCORA_SECTION = """

## 5. Incora Pages (shop.incora.com)

These rules are for Incora's bulk search results. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program searched every requested part in Incora's Bulk Product Search and read the page's own data: every part and every stock line, from all pages of the results table, with each stock line's price breaks from its `$` pop-up.

- `Part block: <part> | Description: ... | Search Qty: ... | Result: Stock Found` (or `Stock Not Found`): one part of the search, as Incora lists it.
- `<part> line <n>: Link: ... | Site: ... | Manufacturer: ... | Certification Description: ... | Revision: ... | Shelf Life (days): ... | UOM: ... | Qty Available: ... | Price breaks (Qty: Price): ...`: one stock line of that part. Each is one result line: return exactly one offer per stock line, with its `Link` as `source_url`.
- `<part>: no stock lines` and `Result: Stock Not Found`: no offer for that part.
- `--- Incora bulk search results table, page <n> ... ---`: read from the page itself (its stored data couldn't be read): the table's text, then for each part with a ✓ `--- Stock lines of <part row> ---` (its opened stock lines: site, Manufacturer, Certification Description, Revision, Shelf Life, UOM, Qty Available, with thousands commas: `207,169` is `207169`) and `Stock line <k> price breaks (Qty: Price): ...` for the k-th stock line in that order. Each stock line is one offer, read by the same rules; its `source_url` is `https://shop.incora.com/shop/products/bulk-search`. A part with a × (hover text `Stock Not Found`) has no offer.

### 5.2. Which Lines

- `listing` `requested` when the block's part is the requested part (spelling differences in spaces, dashes or slashes aside). Incora's bulk search lists only the codes searched: never `substitute`.
- Two stock lines of one part (another Site, Manufacturer, Revision or certification) are two lines, even when everything but one value is the same. Never combine them or add their quantities.

### 5.3. Fields

| In the record | Our field |
|---|---|
| `Qty Available: 207169` | `qty_available` `207169` |
| `Qty Available: 0` | `qty_available` `"Out of stock"` |
| `Price breaks (Qty: Price): 1: 25.0000 USD; 25: 1.0000 USD; 50: 0.5000 USD` | `price` `25.0000` (the row for quantity 1); `currency` `USD` |
| the same price breaks | `price_breaks`: one row per entry, `min_qty` the quantity shown, `max_qty` `"Not Present on website"` (the program writes the ranges) |
| `Price breaks: none shown (no price)` (Incora shows no price: quote only) or `Price breaks: not read` | `price` `"Not Present on website"`, `price_breaks` `[]`, `currency` `"Not Present on website"`; the stock and the line itself stay |
| `UOM: EA` | `uom` `EA` |
| `Pricing UOM: C` (only when the prices are per another unit) | `uom` stays the `UOM` of the stock; never convert the prices |
| `Site: Northlake` | one warehouse: `name` `Northlake`, `qty_on_hand` the line's `Qty Available` |
| `Description: SOLID RIVET,UNIV HD,206 2117AL` (the part block) | `description`, as written |

- `Certification Description` is the line's certification notes (3.11, 3.12). Seen wordings: `Mfg's C of C, Test Report, Process Certs & Separate Raw Material`, `Mfr's C of C, Test Report, Raw Material Certs & Process Certs, DFARS`, `Mfr's C of C, Raw Material Certs & Process Certs, DFARS`, `Manufacturers C of C`.
  - `Mfg's C of C`, `Mfr's C of C` and `Manufacturers C of C` are the manufacturer's certificate of conformance: `tagged_by` `<Manufacturer> C of C` with the line's own `Manufacturer` (company suffixes such as `Inc`, `Inc.`, `Company` dropped, 3.12), e.g. `Allfast Fastening Systems C of C`, `HELLERMANNTYTON C of C`.
  - `trace`: the line's `Certification Description` as written. The program shortens it for the Trace column itself (run 2026-11-05: shortened wording such as `Test Rpt` isn't on the page and was removed by the page check).
  - Never write `8130` or `Form 1`: no Incora line says so.
- `Manufacturer` is who made the line's parts, never the vendor and never a tag on its own.
- `Revision` and `Shelf Life (days)` (shown only when above 0) are the line's own details: never a price, quantity, MOQ or lead time.
- Incora shows no condition, lead time or minimum order quantity: `condition` `"Not Present on website"` (the program sets the default), `lead_time` `In stock` when `Qty Available` is above 0, else `"Not Present on website"`, and `moq` `"Not Present on website"`. The first price-break quantity is never an MOQ.
- Never taken: `Search Qty` (the quantity searched, 1 when none was given), `Order Qty`, `Selected`, the `Order` checkbox, `Add To Cart`, `Request A Quote`, the customer / ship-to name (`Skylink Inc, 2800 S Financial Ct`), `0 items` (the cart), `Items per page`, the range (`1 – 50 of 63`), the Flash Sale and featured products, and the footer.
"""

INCORA_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + INCORA_SECTION
