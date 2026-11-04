"""Wencor's own extraction prompt (fly.wencor.com, vendors/wencor.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what Wencor's pages need on top is the section below, which wins
where the two differ. The page text is built by the adapter, per product page:
its entry in the search list, the product page, and what its stock link opened.
Written from the team's description (2026-11-03) and the signed-in product
pages saved by the 10-part run of 2026-11-03 (debug_pages/*-wencor-*: price
`$0.87 / Each`, `In Stock: 3,494`, `Availability: No Stock Available`,
`Sold in multiples of 100.`, `Ships from Georgia (USA) Distribution on ...`,
Alternative Items repeating the product, SPECIFICATIONS table) and the
"Availability By Warehouse" pop-up of the 13:36 run (AS3209-120, MS9385-09,
NAS1611-121), and the PowerSearch (bulk) page of the same run: its row for a
part states the minimum order quantity ("Quantity entered is below minimum
allowed quantity: 10"), which two of the four parts' product pages don't show
at all. The product page's quantity box holds it for some parts only
(AA55549-13: 5), so the program writes that out too.
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

WENCOR_SECTION = """

## 5. Wencor Pages (fly.wencor.com)

These rules are for Wencor's pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program searched the part in Wencor's search bar, which lists the parts found (the requested part and often its interchanges). For every listed part whose part number is the requested one, it put together one block:

- `--- Wencor product page <n> of <m>: <address> (listed as <part number>) ---` starts the block. Everything up to the next such line is about that one product. Its address is the `source_url` of every line taken from it.
- `--- Its row in PowerSearch (the bulk search; read by the program) ---`: the part's row in Wencor's bulk search: a warning when the quantity asked is below the minimum (`Quantity entered is below minimum allowed quantity: 10`), then the part number, `Interchanges: ...`, `In Stock: 4,319` and `$1.05 / Each`. Not present when the part was searched on its own.
- `--- Its entry in the search list (the list the search showed) ---`: the product's entry in the search list (part number, description, and what the list shows for it, e.g. price, stock or a minimum order quantity).
- `--- The product page ---`: the product's own page.
- `--- The product's order quantity rules (...) ---`: `Minimum order quantity: <n>` and/or `Ordered in multiples of: <n>`, read by the program from the product's quantity box (the page shows them only there, or as `Sold in multiples of <n>.`). Only present when the product has such a rule.
- `--- Opened by the program from the product page's stock link '<link words>', with <qty> in the quantity box ---`: the `Availability By Warehouse` pop-up (5.4). Not always present.

The three parts describe the same product: take each value from whichever part shows it, and combine them into the product's line (e.g. `moq` from the search-list entry, price and stock from the product page, `warehouses` from the stock link). When two parts show different values for the same field, use the product page's.

After the requested part's blocks come its substitutes, one block each:

- `--- Substitute product page <n> of <m>: <address> (listed as <part number> under 'Interchanges' on the product page of <requested part>; opened by the program) ---`, then `--- The product page ---` with that part's own product page, and its `--- The product's order quantity rules ---` when it has any (5.5).

Without a `Wencor product page` line, the program found no listed part with the requested part number, and the text is the search list itself. Then return an offer only for an entry whose part number is the requested one; an entry for another part number is `listing` `other`, or `substitute` when the page lists it as an interchange or substitute of the requested part. A list that says nothing was found is `no_results`.

### 5.2. One Line per Product

- A block is one result line, and so is a product shown twice: blocks with the same address item number (`.../ecom-item/10017622`) are the same product, one line, unless its product page shows a table of several lines for the same part (each condition, lot or warehouse with its own stock or price): then each row is its own line, and the search-list values apply to every row only when they clearly cover the whole product (e.g. one `MOQ` for the part).
- Two blocks are two lines, even for the same part number: never merge them, never copy a value from one block to another.
- `part_number` is the part number the product page shows for itself (above the description), as written.
- `Item number: 10047883` is Wencor's own item number, not a part number: never `part_number`.

### 5.3. Product Page Fields

The product page shows, in this order: the part number (twice: breadcrumb and title), the description (`O RING`), the price (`$0.87 / Each`), `Interchanges: <part numbers>`, sometimes `Sold in multiples of 100.`, the stock (`In Stock: 3,494`, none when out of stock), `Availability: In Stock` / `Availability: No Stock Available`, sometimes `Ships from Georgia (USA) Distribution on November 3, 2026`, the `Availability for All Warehouses` link, `ADD TO CART`, then `Interchanges / Substitutes will appear below.` with `Alternative Items (<n> Products)`, and the `SPECIFICATIONS` table.

| On the page | Our field |
|---|---|
| Description under the part number (`CABIN AIR FILTER`) | `description` |
| `In Stock: 3,494` | `qty_available` `3494` (3.2) |
| `Availability: No Stock Available` (and no `In Stock:` number) | `qty_available` `"Out of stock"` |
| `$0.87 / Each` | `price` `0.87` |
| `Quantity entered is below minimum allowed quantity: 10` (PowerSearch row) | `moq` `10`. The PowerSearch row is where Wencor states the minimum: use it first |
| `Minimum order quantity: 5` (order quantity rules block) | `moq` `5` |
| `Ordered in multiples of: 100` / `Sold in multiples of 100.`, with no minimum given | `moq` `100`: the part is only sold in lots of that size, so it is the least that can be ordered. Not a pack size and not stock |
| `MOQ`, `Min Qty`, `Minimum Order Quantity`, `Min Order Qty` (product page or search-list entry) | `moq` (3.3), number only |
| None of these (a PowerSearch row without the warning, no order quantity rules block, no such wording) | `moq` `"Not Present on website"` (the quantity box's `1` is a default, never a minimum) |
| Quantity price table | `price_breaks` (3.5) |
| `Availability By Warehouse` pop-up (5.4) | `warehouses` (3.7) |
| `Ships from Georgia (USA) Distribution on <date>` | nothing: not `lead_time`, not a `warehouses` entry (the program adds it to the line's comments itself) |
| SPECIFICATIONS `Unit Code` (`EA`) | `uom` (the price's `/ Each` is the same unit: write `EA`) |
| SPECIFICATIONS `Standard Lead Time` (`185 Days`) | the lead time of 3.8: in stock `In stock (185 days if out of stock)`, out of stock `185 days` |
| SPECIFICATIONS `Typical Certifications` (`8130 W/ CRITICALITY`) | `tagged_by` (3.12) as written |
| Condition (`NE`, `New`, `OH`, `SV`, ...) when the page shows one | `condition` (3.6) |

- `Login for price and availability`: the page was read signed out; price and stock are not shown (2.2, last case).
- `Call for price`, `Request a quote`, `RFQ`: the price is not shown (`"Not Present on website"`).
- A struck-through list price is not the price.

### 5.4. Availability By Warehouse

The pop-up lists, per warehouse, `Warehouse Name`, `Warehouse City` and `Availability`: how many of the quantity in the box (`with <qty> in the quantity box` in its header) that warehouse can supply on which date. Seen 2026-11-03 with 100 in the box:

```
Georgia (USA) Distribution *
Peachtree City
100 available on November 3, 2026
Netherlands Distribution
Hoofddorp
5 available on November 3, 2026
95 available on February 15, 2027
```

- One `warehouses` entry per warehouse: `name` = `<Warehouse Name>, <Warehouse City>` without the `*` (`Georgia (USA) Distribution, Peachtree City`).
- The earliest date in the pop-up is the day it can ship from stock: the quantity on that date is `qty_on_hand` (`100`; Netherlands `5`). A warehouse whose first date is later has `qty_on_hand` `"0"`.
- Each later date with its quantity is an `incoming` entry, date as written (`{"date": "February 15, 2027", "qty": "95"}`).
- These quantities only go up to the quantity in the box: they say where and when that quantity can come from, not the warehouse's whole stock. Never add them up and never use them for `qty_available` (that is `In Stock: <n>` on the page).
- `min_qty` / `max_qty`: `"Not Present on website"`. `CLOSE`, the column titles and the `*` are not values.

### 5.5. Substitutes (Interchanges)

Wencor lists the parts that can replace this one as `Interchanges: A / B / ...` under the price (the same list as SPECIFICATIONS `Interchanges`) and says `Interchanges / Substitutes will appear below.`: Wencor itself calls them substitutes. The program opened each interchange's own product page (up to five) as a `Substitute product page` block.

- Each `Substitute product page` block is one line with `listing` `substitute`: `part_number` is the part number that page shows for itself (e.g. `M83248-1-120`), and every value comes from that block only (price, `In Stock`, lead time, `Typical Certifications`, the order quantity rules for `moq`), by the rules of 5.3. Never a value of the requested part's block.
- A substitute's own page with `Login for price and availability`, no price and no stock: still its line, with what it shows (2.2).
- Interchanges named on a page but not opened (no block of their own) are not lines. The interchanges listed on a substitute's own page are not lines either.
- `Alternative Items (<n> Products)` on a product page lists that same product again (it repeats the price and stock): not another line.
- A substitute is never the requested part's line: the requested part's own block stays `listing` `requested`, whatever its stock.

### 5.6. Not Taken

- SPECIFICATIONS `Hazardous Item`, `Business Area`, `ECCN Code`, `Cage Code`, `Life Limited Part`, `Critical Part`, `Wencor 8130` (`Not Required`): not fields.
- The site's header, menus, cart and mini cart (`Interchanges: ...` under a cart item), `Recently viewed`, `Customers also bought`, filters, `Sort by`, `8 Results`, `Previous page`.
- The quantity box beside `Add to Cart` (`1`): never `moq` or stock.
- Page controls: `Add to Cart`, `Add to Quote`, `Add to List`, `Share`, `Print`, `List View`.
- `Availability for All Warehouses` (the link's words), `Quantity`, `Added to cart`, `DOCUMENTATION`, `Customer Resources` and the footer.
- Condition: the pages seen show none. Unless the text shows one for the line, `condition` is `"Not Present on website"` (the program applies Wencor's setting for a condition the site doesn't show).
"""

WENCOR_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + WENCOR_SECTION
