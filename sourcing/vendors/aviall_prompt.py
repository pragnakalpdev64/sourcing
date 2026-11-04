"""Aviall's own extraction prompt (shop.boeing.com, vendors/aviall.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what Aviall's pages need on top is the section below, which wins
where the two differ. Written from the team's screenshots (2026-10-30) of the
search results, the multi-line (bulk) results and a product page, and checked
against the live pages saved on 2026-11-02 (debug_pages/, first live run).
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

AVIALL_SECTION = """

## 5. Aviall Pages (shop.boeing.com)

These rules are for Aviall's results pages (Boeing's store, "Boeing Distribution (formerly Aviall)"). Where they differ from the sections above, follow these.

### 5.1. What the Page Shows

- Each result is a card: part number (a link), sometimes a badge (`Superseded`, `Clearance`, `Overstock`), the description, small icons, sometimes the manufacturer name and `Mfr Part# <number>`, then on the right the supplier (`Boeing Distribution (formerly Aviall)` or `Boeing Distribution Services`), the price, the stock and the Add to Cart button.
- Multi-line (bulk) results come from the "Multi-line Ordering" window (steps `Add Items`, `Resolve Issues (if any)`, `Add to Cart`); they may be shown in that window or on the page. Parts the site matched to several products are listed under `Resolve Issues` with each candidate; each candidate is a card like the others (its own full part number decides which part it is for). Multi-line results list the same cards one per row, and warn about part numbers the site couldn't find: `We were unable to find the following part number: 166-32800=5b`, `We could not find 166-32800=5b`. Such a part has no line: return no offer for it.
- An `=` is part of Aviall's part numbers (`021200-21181=32`, `PKAB244CKIT=2V`): keep it in `part_number`.
- **The card's own part number is shown without its `=` suffix** (`021200-21181` for `021200-21181=32`, `AV2539512` for `AV2539512=F1`). The program wrote the full number from the card's own product link next to it as `[full part number: 021200-21181=32]` (never from a `View Replacement` link). That full number is the card's `part_number`: write it exactly so (any letter case). It decides which part the card is for: two cards both showing `021200-21181` can be `021200-21181=32` (the part asked for) and `021200-21181=27` (another part). A card without such a note: its part number as shown.
- Values only shown in a pop-up the card opens on a click were read by the program and written into the card next to the button that opens it, as `(<name> pop-up: ...)`: `(Price Breaks pop-up: ...)`, `(Stock By Location pop-up: ...)` (the pin beside the stock: quantity per warehouse), `(Shelf Life pop-up: ...)` and others; hover text as `(shown on hover: ...)`, icon labels as `[icon: ...]`. Take them as part of that card only. A pop-up that repeats the card (price, stock) adds nothing new.
- Not results, never an offer: the cart (`Products in Cart`, `Qty: 100`, `Subtotal`, `View Cart & Checkout`: someone's cart item with its price and unit), `You may also be interested in` and the products under it, the filters on the left (`Brands`, `Supplier`, `Shop By`, `Condition`, `Shelf Life`), the site's feature tour, the header (`Shopping As: ...`) and the footer. The program removes most of these; ignore any that are left.

### 5.2. Card Fields

| On the card | Our field |
|---|---|
| `In Stock (15838)` | `qty_available` `15838` (a bare `In Stock` under the description repeats it: not another line) |
| `Out Of Stock`, `Not Available` | `qty_available` `"Out of stock"` |
| `Available To Order (30 days lead time)` | nothing in stock: `qty_available` `"Out of stock"`, and `lead_time` `30 days` (3.8): it can be ordered with that lead time |
| `On Order` `(Available 11/26/2026)` | nothing in stock: `qty_available` `"Out of stock"`, and `lead_time` `Available 11/26/2026` as written (the program turns the date into days) |
| `The part number is currently not available online. Please contact your Boeing Distribution Sales Representative` | no price and no stock shown (2.2, last case); with `Out Of Stock` on the card, `qty_available` `"Out of stock"` |
| `$20.16 each`, `$0.49 MR`, `$4,860.00 EA` | `price` = the number (`20.16`); `uom` = the word after it as written (`each`, `MR`, `EA`) |
| `$156.00 PK 50 per pack`, `$156.23 CN/1GL` | `price` `156.00` / `156.23`; `uom` = the unit code only (`PK`, `CN/1GL`). The pack size (`50 per pack`) is not a quantity: never `moq` or `qty_available` |
| `Price Breaks` pop-up (`Quantity` / `Price Break`: `1 $0.49/MR`, `500 $0.49/MR`) | `price_breaks` (3.5), one entry per row: the row's quantity is where that price starts, so it is `min_qty`, and `max_qty` is `"Not Present on website"` (never worked out from the next row; the 3.5 rule "same `min_qty` and `max_qty`" doesn't apply here); `price` number only (`0.49`) |
| `(Stock By Location pop-up: ...)`, or stock hover text, with locations and quantities | `warehouses` (3.7), one entry per location as shown (the pop-up's own wording, e.g. `Dallas, TX 15838`) |
| description line under the part number | `description` |
| `[icon: ...]` naming a certificate (e.g. `8130`) | `tagged_by` (3.12) as written |

- The price is the card's own price. A struck-through `List Price: $...` above it is not the price.
- A `Superseded` card (`Superseded by part number 021200-21181=32`, `View Replacement`, no price): the card is for its own full part number (e.g. `021200-21181=27`), which has no stock or price shown. It is `listing` `requested` only when that full number is the part asked for; otherwise `other`. The replacement it names is not a line by itself; when the replacement has its own card with price and stock and is not the part asked for, that card is `listing` `substitute`. Seen 2026-11-02: asked `021200-21181=32`, cards `021200-21181=32` (`$140.59 each`, `In Stock (19)`, `listing` `requested`) and `021200-21181=27` (`Superseded`, `listing` `other`).
- `--- Replacement product page <n> of <m>: <address> (opened by the program from 'View Replacement' on the Superseded card <full part number>) ---`: when a Superseded card's replacement has no card of its own in the results, the program read the replacement's product page and added it after the results. It is one line for the part number that page shows (its full number, with the `=` suffix when shown), with its own price, stock and lead time (`Lead Time (Days)`) from that block only; its address is its `source_url`. It is `listing` `substitute` when the Superseded card is the part asked for (its full number is the requested one, or the requested number has no `=` suffix and the card's number without its suffix is the requested one); otherwise `other`. The Superseded card itself stays its own line as above (`requested` when it is the part asked for, with no price or stock). Seen 2026-11-02: card `161A2114-1=IH` `Superseded by part number 161A2114-1=AG`, `View Replacement` -> `https://shop.boeing.com/cpd/bdi_161a2114-1=ag`.
- `You are not approved to purchase this product`, `Request Price`, `View Replacement`: no stock and no price shown (2.2, last case).
- A card for a different part than the one requested is `listing` `other`. Compare full part numbers: the description (`3M™ Fastbond™ 30NF Contact Adhesive ... 1 gal can`) and the `Mfr Part#` (`62427475306`) don't make a card another part.
- `Mfr Part# <number>` is the manufacturer's number for the same product: not `part_number`, not a substitute.

### 5.3. Not Taken

- `AOG` (the toggle, and `Item cannot be ordered AOG`, `only available as a Standard (Non-AOG) order`): ignored (2.4).
- Shelf life (`Shelf Life 50% - 75% (318 - 476 days)`, `No less than 351 days remaining`, `>99%`, the `(Shelf Life pop-up: ...)`): not a lead time or any other field.
- The order quantity box (`1`, or the quantity entered for a multi-line search, e.g. `50`): never `moq` (3.3) or stock.
- `Manufacturer: Select`, `Revision: Select`, `Quality Docs: Select`, `Certs Required`: choices for an order, not values. `Certifications: <part number>` is a certified part number, not a trace. The manufacturer name under the description is not `trace`.
- `WARNING: California Proposition 65`, `Hazmat`: not a field.
- Badges `Forecastable`, `8130-3 Eligible` (a certificate can be asked for, none is stated): not `tagged_by` or any other field.
- `Add to Cart`, `Add to List`, `Request Support`, `Share`, `Sort By`, `1 result` / `2 results`, `Stock By Location` (the button itself), `expand_more` (an icon's name): page controls, not values.

### 5.4. Condition

Aviall's cards show no condition (the site only has a Condition filter on the left, which is not a card's condition). Unless a card's own text or hover text names one of the condition wordings (3.6), `condition` is `"Not Present on website"`. (The program then applies Aviall's setting for a condition the site doesn't show; don't fill one in yourself.)

### 5.5. Lead Time

A card shows a lead time only when nothing is in stock: `Available To Order (30 days lead time)` -> `lead_time` `30 days`. Otherwise the lead time is only on the product page (`Lead Time (Days) 80` under Technical Information): the program reads it there itself for a priced line that is out of stock, so leave `lead_time` empty when the card shows none. Fill `lead_time` (3.8) only from wording on the card or its hover text that is clearly a lead time or delivery time; never work it out from stock, shelf life or a shipping message.
"""

AVIALL_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + AVIALL_SECTION
