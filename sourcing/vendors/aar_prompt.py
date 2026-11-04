"""AAR's own extraction prompt (shop.aarcorp.com, vendors/aar.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what AAR's pages need on top is the section below, which wins
where the two differ. Written from the team's description (2026-11-03): the
results show in-stock availability, alternate parts, condition, out-of-stock
status, lead time, unit price and description; some parts show only an RFQ
button instead of a price. The pages haven't been seen yet: the wordings in
quotes below are the usual Magento ones, to be checked on the first run
(--dump-pages saves debug_pages/*-aar-read.txt).
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

AAR_SECTION = """

## 5. AAR Pages (shop.aarcorp.com)

These rules are for AAR's results pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program opened AAR's search for the requested part number(s) (several parts in one search are separated by commas) and read every product card on the results pages:

- `--- AAR search results for <query>: one block per product card ... ---` starts the text.
- `--- Card <n>: <product link> ---` starts one card. Everything up to the next such line is that one product. Its product link is the `source_url` of every line taken from it.
- `(shown on hover or as icon labels: ...)` after a card is that card's hover text and icon labels: part of that card only.
- `--- AAR page at <address> (no product cards; the page text) ---`: no cards were found. The text is the page itself: a "no results" page (no offers), or the product page the search opened directly (read it as one card; its address is the `source_url`).

AAR's search matches words, so cards for other part numbers are often listed too (similar numbers, the same words in the description). The program put the cards that mention a requested part number first.

### 5.2. Which Cards Are Lines

- A card whose part number is a requested part number (spaces, dashes, slashes or dots aside, 3.9) is that part's line: `listing` `requested`.
- A card the site shows as an alternate of a requested part (`Alternate`, `Alternate Part`, `Alternates: <part numbers>`, `Interchangeable with`, `Replaces`, `Superseded by`) is `listing` `substitute`, for that requested part, only when the card itself shows its own price, stock or RFQ. A list of alternate part numbers inside the requested part's card is not a line by itself, and never that card's `part_number`.
- Any other card (another part number that only looks similar or shares a word) is `listing` `other`. Never return an offer for it in a bulk search, and never take a value from it.
- A card shown twice (same product link) is one line.
- The card's part number is the one the card shows as the product's part number or name (as written). A manufacturer's or AAR's own item/SKU number shown as such is not `part_number`.

### 5.3. Card Fields

| On the card | Our field |
|---|---|
| A stock quantity (`In Stock: 25`, `25 in stock`, `Qty Available 25`, `Available: 25`) | `qty_available` the number |
| `In Stock` without a quantity | `qty_available` `"In stock"` |
| `Out of Stock`, `Not Available`, `Unavailable`, a quantity of 0 | `qty_available` `"Out of stock"` |
| Price (`$123.45`, `Unit Price $123.45`, `$123.45 / EA`) | `price` the number (`123.45`); `currency` `USD` only when the card writes `USD` or `US$` (3.4) |
| `RFQ`, `Request for Quote`, `Request a Quote`, `Request Quote`, `Call for Price`, `Login for price` and no price | `price` `"Not Present on website"` (the part is quoted on request only). The stock and other fields are still taken as shown |
| Condition (`NE`, `New`, `NS`, `New Surplus`, `OH`, `Overhauled`, `SV`, `Serviceable`, `AR`, `As Removed`, ...) | `condition` (3.6) |
| Lead time (`Lead Time: 30 days`, `Lead time 4 weeks`, `Ships in 5-7 days`) | `lead_time` (3.8), as written for weeks or ranges (never converted) |
| Minimum order (`Min Qty`, `MOQ`, `Minimum Order Quantity`) | `moq` (3.3) |
| Unit (`EA`, `/ EA`, `UOM: EA`) | `uom` as written |
| Description under the part number | `description` |
| Quantity price table | `price_breaks` (3.5) |
| Stock per location (location with its own quantity) | `warehouses` (3.7) |
| Certificate (`8130-3`, `EASA Form 1`, `C of C`, `FAA`) named on the card | `tagged_by` (3.12) as written |
| Trace (`Trace: ...`) named on the card | `trace` (3.11) |

- An RFQ-only card with a stock status is still a line (stock and condition as shown, price not present). An RFQ-only card with no stock status falls under 2.2's last case.
- A struck-through or `Was`/`List` price above another price is not the price.
- The quantity box beside `Add to Cart` (`1`) is never `moq` or stock.

### 5.4. Not Taken

- The site's header, menus, mini cart, `My Account`, search box, filters on the left (`Shop By`, `Condition`, `Manufacturer`, `Price`), `Sort By`, `Items 1-12 of 30`, `Show 12 per page`, page numbers, `Compare Products`, `My Wish List`, `Recently Viewed` and the footer.
- Buttons: `Add to Cart`, `Add to Quote`, `RFQ` (the button itself; see 5.3 for what it means), `Add to Wish List`, `Add to Compare`.
- Ratings, reviews and images.
"""

AAR_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + AAR_SECTION
