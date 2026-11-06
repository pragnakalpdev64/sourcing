"""Monroe Aerospace's own extraction prompt (monroeaerospace.com, vendors/monroe.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what Monroe's pages need on top is the section below, which wins
where the two differ. Written from the pages (2026-11-04, BigCommerce):
the search card shows part number, name, price (or Request Quote) and "In
Stock"; the product page adds the stock number ("In Stock - 44,350 units
available"), the variants table drawn by its script (Available, Unit of
Measure, Unit Price, certificate / manufacturer choices), the Bulk Discounts
pop-up (price breaks), the minimum order and notes. No condition, lead time or
warehouses. The first signed-in run (16:28) lost every price: "In stock"
without a number was removed by the value check (fixed in extract.py), and the
stock number and price breaks weren't read (now are). --dump-pages saves
debug_pages/*-monroe-*-read.txt.
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

MONROE_SECTION = """

## 5. Monroe Aerospace Pages (monroeaerospace.com)

These rules are for Monroe's search cards and product pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program searched Monroe for the requested part number(s), kept only the search cards whose part number is a requested one (spaces, dashes, slashes or dots aside, 3.9), and opened each of those products. Per product, in this order:

- `--- Card <n>: <product link> ---`: the search card, written by the program as `Part number:`, `Name:`, `Price:`, `Stock:`, `Action:` lines.
- `--- Product page of card <n>: <product link> ---`: the product page's text (price, stock line, quantity box, Specifications, Notes, Description).
- `--- Stock table of card <n> ... ---`: one line, `Available: <n>; Unit of Measure: <u>; Unit Price: <p>`: the product's whole stock, its unit and unit price, from the page's variants table.
- `--- Bulk discount rates of card <n> ... ---`: the "Bulk Discounts" pop-up, one rate per line.
- `--- Monroe search for <query>: no card is for <part numbers> ---`: nothing Monroe lists is that part (no offer).

Card, product page, stock table and bulk rates of one card are ONE line: take every value from them together, and return exactly one line per card. Where they differ, the stock table wins for stock, unit and price, then the product page, then the card. The product link is the `source_url`.

### 5.2. Which Cards Are Lines

- Every card shown is for a requested part number: its line is `listing` `requested`. Two cards with the same part number written differently (`MS21250-05022`, `MS21250-05-022`) are two lines, each with its own product link.
- `part_number` is the card's `Part number:` as written. The unpunctuated number (`MS24693C274`), the `UPC` and the `NSN` / `NIIN` are never `part_number`.
- No substitute lines: `Alternate Part Number: AN507C1032R12` (or `Possible`) in Specifications is not an offer; the program does not read "View Similar Products".

### 5.3. Fields

| On the line | Our field |
|---|---|
| Stock table `Available: 44,350` | `qty_available` `44350` (2.2: in stock) |
| `In Stock - 44,350 units available` on the product page | `qty_available` `44350` |
| `In Stock` and no number anywhere for the line | `qty_available` `"In stock"` |
| `Out of Stock`, `Sold Out`, `Available: 0` | `qty_available` `"Out of stock"` |
| Stock table `Unit Price: $0.27`, `$0.27 / Unit`, card `Price: $0.27` | `price` `0.27` |
| `Currency code of the page: USD` (the first line of the product page text: the page's own currency setting), `US$` before the price, or `USD` | `currency` `USD` |
| `Price: none shown`, `Action: Request Quote`, only `$0.00` | `price` `"Not Present on website"` (quoted on request); stock and the other fields as shown |
| `Buy 500 - 999 and pay only $0.26 each` (bulk rates) | one `price_breaks` row each: `min_qty` `500`, `max_qty` `999`, `price` `0.26`; `Buy 10000+ ...` / `or more`: `max_qty` `"Not Present on website"`. Only the rates shown: never add a row for the base price |
| `Minimum Order Note: Minimum order is 100. Must order in increments of 100.`, `Minimum Purchase: 100 units` | `moq` `100` (3.3). ONLY from these. Never from the bulk discount rates (their first `Buy 500 - 999` is a discount, not the minimum), `Sold in increments of`, the `Qty:` box or the amount beside `Add to Cart` |
| Stock table `Unit of Measure: EA`, else `/ Unit` after the price | `uom` as written (`EA`) |
| Name under the part number (`FLAT HEAD SCREW (MS24693-C274)`) | `description` |

- The amount beside `Add to Cart` (`$27.00`, the price of the quantity in the box) is never `price`. `Sold in increments of 100`, `Quantity Increment`, `Units Per Package` and the `Qty:` box are never `moq` or stock.
- `tagged_by`: `"Not Present on website"` unless the line names who tags or certifies the part (`Tagged By:`, `Tag Source:`, `Cert Source:`). `Industry Standards/Certification: MS25036` is the part's specification standard, not a tag: never `tagged_by` or `trace`. Never from `Country of Origin`, `Alternate Part Number`, `NSN`, `Material` or any other specification, and never from the description's words (`Material certifications, Certificates of Conformance (CoC), and traceability are available where applicable` states nothing for this line). `trace`: `"Not Present on website"` unless the line names a trace source.
- Return only values written for this line. Nothing else from a product's specification list (size, colour, style, material) goes in any field.
- Monroe shows no condition, lead time or warehouses: leave those empty (`warehouses` `[]`); never invent them. `Our inventory is extensive...`, `Need additional stock? Call ...` and `available for immediate shipment` are not stock, lead time or a quantity.
- Never taken: `Free Shipping`, the phone number, notices (flood, holidays), `Prop 65 Warning`, `Request Info` / `Request Information`, `Email`, `Print`, `Download PDF`, the cart, the breadcrumb (`Home / Screws / ...`) and the footer.
"""

MONROE_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + MONROE_SECTION
