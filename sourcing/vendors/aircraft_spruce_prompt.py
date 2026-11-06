"""Aircraft Spruce's own extraction prompt (www.aircraftspruce.com, vendors/aircraft_spruce.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what Aircraft Spruce's pages need on top is the section below,
which wins where the two differ. Written from the team's screenshots and the
public product pages (2026-11-05): title, "$77.65/Kit", Quantity box, Add to
Cart, "Part# 01-08800 / MFR Model# 25405", a stock line worked out for the
quantity in the box ("In Stock", "Partial Stock", "No Stock") and a delivery
line ("Expected to Deliver: Tuesday, Nov. 10", "Expected to Ship: Friday, Dec.
4"), then Overview, Specifications, Documents, other products, Reviews, Q&A. No
stock number, condition, warehouses or price breaks seen. What a signed-in page
adds is checked on the first run (--dump-pages saves
debug_pages/*-aircraft_spruce-*-read.txt).
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

AIRCRAFT_SPRUCE_SECTION = """

## 5. Aircraft Spruce Pages (aircraftspruce.com)

These rules are for Aircraft Spruce's product pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program opened the product page(s) for the requested part: by the page's address, from the site's search for the part number, or (only when the part number found nothing) from the site's search for the request's description. On each page it typed the requested quantity into the `Quantity` box, because the page shows no stock number: its stock line says whether that quantity is in stock. Per product, in this order:

- `--- Product <n>: <product link> (<how it was found>) ---`: one product. Everything up to the next such line is that one product, and its product link is the `source_url` of its line.
- `--- What the program read on product <n> ---`: the program's own reading of the page, one value per line: `Title:`, `Part#:` (Aircraft Spruce's catalog number), `MFR Model#:` (the manufacturer's number), `Is the requested part <part>: yes` or `no (another part number)`, `Price shown:`, `Quantity typed into the Quantity box:`, `Price shown for that quantity:`, `Stock line for quantity <q>:`, `Delivery line:`, `Page footer: Invoice currency USD`.
- `--- Page text of product <n> ---`: the product's own section of the page (title, price, Part#, Overview, Specifications, Documents).
- `--- Aircraft Spruce: no product page for <part> ... ---`: nothing found (no offer).

Return exactly one line per product. Where the program's reading and the page text differ, the program's reading wins.

### 5.2. Which Products Are Lines

- `Is the requested part <part>: yes`: `listing` `requested`. `part_number` is the `Part#` when it is the requested number, else the `MFR Model#` as written.
- `Is the requested part <part>: no (another part number)`: the site's search led to another part (e.g. the search for `MS20470AD4-4` opened `AN470AD-4-4`), or it was found by the description. `listing` `other`, `part_number` the `Part#`. Never `substitute`: the page doesn't call it one.
- The size / option buttons (`Select Size: Quart Kit | 1/2 Gallon Kit | 2 Gallon Kit`) are the same product in other sizes, each with its own Part#: never lines of their own and never values for this line. Only the selected one (the page's `Part#`) is this line.
- A Specifications table listing several part numbers (`Part No.` column): only the row with this line's `Part#` belongs to it.
- No substitutes: `View ... Series Page`, `Frequently Purchased With` and `Customers Also Viewed` are never offers.

### 5.3. Fields

| On the page | Our field |
|---|---|
| `Stock line for quantity 5: In Stock` (also `In Stock (Lower 48)`) | `qty_available` `"In stock"` (the program sets the quantity) |
| `Stock line ...: Partial Stock`, `Delivery line: 6 Expected to ship: Thursday, Nov. 5 / 44 Backordered Expected to ship: TBD` | `qty_available` `6` (the quantity expected to ship, never the backordered one) |
| `No Stock`, `Special Order`, `Non-Stocking Product`, `Made to Order`, `Pre-Order`, `Dropship from Supplier` | `qty_available` `"Out of stock"` |
| `Price shown: $77.65/Kit` | `price` `77.65`; `uom` `Kit` (the word after `/`: `Each`, `Kit`, `Sheet`, `Pound`, `Foot`) |
| `Price shown for that quantity: $70.00/Kit` | that quantity's price; `price` stays the `Price shown:` one |
| `Page footer: Invoice currency USD` | `currency` `USD` |
| `Price shown: none shown`, `Call for Price`, `Request Quote` | `price` `"Not Present on website"` |
| In stock: `Delivery line: Expected to Ship: Thursday, Nov. 5` (or `Expected to Deliver: ...`) | `lead_time` `In stock (expected to ship Thursday, Nov. 5)` with the date as written |
| Out of stock: `Delivery line: Expected to Ship: Friday, Dec. 4` | `lead_time` `Expected to ship Friday, Dec. 4`, as written. `Available to Order` alone is no lead time |
| Partial stock | `lead_time` `In stock (<the backordered part of the delivery line, as written>)` |
| The title (`Liquid X-30 Foam Quart Kit X-40`) | `description` |
| A minimum stated for the product (`Minimum order: 4`, `Minimum Purchase`) | `moq` (3.3). Never the `Quantity` box, the quantity typed by the program, a kit or pack size, or a size button |

- `Other price shown beside it:` is a list or regular price shown next to the price: never `price`.
- `tagged_by` and `trace`: `"Not Present on website"` unless the product's own text names who tags or certifies this part (`8130-3`, `Certificate of Conformance`, `C of C` stated as supplied with the part). `Mfg test reports are available for an additional charge` is an extra, not a certificate of this line. Never from `FAA-approved under STC ...`, `TSO` or `PMA` wording in the Overview.
- Aircraft Spruce shows no condition and no warehouses: leave condition empty and `warehouses` `[]`.
- Never taken: `FREE Shipping`, `Eligible for Flat Rate Shipping`, `30 Day Risk Free Purchase`, `FREE Returns`, `SAME DAY SHIPPING ON ORDERS PLACED BY 4 PM`, the phone number, `Add to Cart`, `Add to Wishlist`, `SHARE`, `Photo may represent series and not specific product`, ratings and reviews counts, `Answered Questions`, `JUMP TO`, `View in Catalog`, `Documents` link names, the breadcrumb (`Home / Composite Materials / Foam`) and prices in the Overview (`custom lengths for $1.60 per foot`: a charge for a custom option, not this line's price).
"""

AIRCRAFT_SPRUCE_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + AIRCRAFT_SPRUCE_SECTION
