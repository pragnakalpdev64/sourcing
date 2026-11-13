"""Textron Aviation's own extraction prompt (ww2.txtav.com, vendors/textron.py).

The client's field rules are the shared prompt's, so a rule change reaches
every vendor; what Textron's pages need on top is the section below, which wins
where the two differ. Written from the public part pages (2026-11-12, signed
out): one block per part with the part number, description, "Unit of Measure:
EA", weight and dimensions, the applicable aircraft, a stock line ("In-stock
and ready to ship!"), "Log in or Register ... to view pricing and availability"
and the listed alternates ("FOR CESSNA APPLICATION ONLY 34-0070371-01",
"Beech-One-Way Supersedure"). The page also carries its values in hidden fields
(price USD / EUR, unit, minimum order, condition NEW, an availability note),
which the program reads itself. The signed-in block (onboarding 2026-11-12) adds
"Your Price: $42.01 / 37,43 €", "LIST PRICE: $70.01 / 62,38 € DISCOUNT: 40%",
"160 IN STOCK" / "0 IN STOCK" with "Order today with estimated availability
12/27/2026.", "Ships from Wichita", NOTES, Quantity, Add to Cart, PRICE REVIEW.
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

TEXTRON_SECTION = """

## 5. Textron Aviation Pages (ww2.txtav.com)

These rules are for Textron Aviation's part pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program searched the requested part(s) on Textron Aviation's site, signed in, and gives one block per part found. Per part, in this order:

- `--- Part <n>: <part link> (<how it was found>) ---`: one part. Everything up to the next such line is that one part, and its part link is the `source_url` of its line.
- `--- What the program read for part <n> (the page's own data fields) ---`: values the page carries in its own data, one per line: `Part number:`, `Is the requested part <part>: yes` or `no (another part number)`, `Description:`, `Unit of measure:`, `Minimum order:`, `Condition:`, `Your price in the page's data: USD <x> / EUR <y>`, `Availability note in the page's data:`, `Message on the part:`, `Quantity typed into the Multiple Part Search:`, `Alternate listed: <part number> <wording> (<kind>)`.
- `--- Page text of part <n> ---`: the part's own section of the page (title, description, unit, weight, dimensions, stock line, price line, alternates).

Return exactly one line per part block. Where the program's reading and the page text differ, the program's reading wins.

### 5.2. Which Parts Are Lines

- `Is the requested part <part>: yes`: `listing` `requested`, `part_number` the `Part number:` as written.
- `Is the requested part <part>: no (another part number)`: the site answered with another part number. `listing` `other`, `part_number` the `Part number:`.
- `Alternate listed:` lines (`FOR CESSNA APPLICATION ONLY`, `SEE TECHNICAL PUBLICATIONS FOR APPLICATION`, `Cessna Alternate`, `Beech-One-Way Supersedure`, `ACCEPTABLE SPARE FOR BEECH MODELS`, `ALTERNATE PART NUMBER FOR ...`) are never lines of their own and never values of this line: the program lists them in Internal Comments.

### 5.3. Fields

| On the page | Our field |
|---|---|
| `Your Price: $42.01 / 37,43 €` | `price` `42.01`, `currency` `USD` (the dollar figure; the euro one after `/` is the same price in EUR, written with a decimal comma). It is the USD figure of `Your price in the page's data:` |
| `LIST PRICE: $70.01 / 62,38 € DISCOUNT: 40%` | never `price`: the list price before this account's discount |
| `Your Price: $38.40` (a dollar price alone, no euro figure) | `price` `38.40`, `currency` `USD` |
| `Your price in the page's data: none`, no `Your Price:` line, `PLEASE CONTACT US FOR ASSISTANCE WITH PRICING.`, `Call for Price`, `Request Quote` | `price` `"Not Present on website"` |
| `Stock shown: 160 IN STOCK` (`160 IN STOCK` in the text) | `qty_available` `160` |
| `Stock shown: 0 IN STOCK` | `qty_available` `"Out of stock"` |
| `Ships from: Wichita` (also `Ships from Columbus, GA`) | `warehouses`: one entry, that place with the stock quantity (also when it is 0) |
| Out of stock and `Order today with estimated availability 12/27/2026.` (also `Availability note in the page's data:`) | `lead_time` `Estimated availability 12/27/2026` |
| In stock | `lead_time` `In stock` |
| `Availability note in the page's data: Contact customer support for availability` | no lead time |
| `Unit of measure: EA` (also `Unit of Measure: LB` in the text) | `uom` |
| `Minimum order: 10` (the page: `Must be a Multiple of: 10`) | `moq` `10` (3.3). Never the quantity typed by the program |
| `MANUFACTURER PART NUMBER PT6A-100HRBEECH KT` under the Textron number | the manufacturer's number of the same part: never `part_number`, never a line of its own |
| `Condition: NEW` | `condition` `NE` |
| `Description:` | `description` |

- `tagged_by` and `trace`: `"Not Present on website"` unless the block names who tags or certifies this part (`8130-3`, `Certificate of Conformance` stated as supplied with the part). `Each delivered OEM part will meet the original manufacturer's specification` is not a certificate.
- `Note:` lines (`SOLD IN MULTIPLES OF 10`, `MAX SHELF LIFE 12 MONTHS`, `HAZARDOUS MATERIAL`, `EXTENDED INACTIVITY: QUOTE MAY TAKE LONGER`) are written into Internal Comments by the program: not values of any field.
- `Message on the part: NO LONGER AVAILABLE. PLEASE SEE OPTIONS BELOW.`: Textron doesn't sell the part any more (the program reports it as not sold, with the options listed). `NEW REPAIR SERVICES` under the title is a link to repair services, not a condition.
- Never taken: `Service Center Stock`, `No Service Center Stock Available`, `FREE FedEx Ground SHIPPING`, `Part Type: Proprietary`, `Quantity` and its `Location` / `On Hand Quantity` table (stock at service centers for grounded aircraft only, AOG), the Fairbanks / Northland Aviation contact, `Add to List`, `Add to Cart`, `Remove Item`, `Create a New List`, the breadcrumb (`Categories / Hardware / Fasteners / Rivets`), weight and dimensions, the applicable aircraft list, `YOUR REGIONAL SUPPORT TEAM`, `WHAT'S NEW?`, the cart count, the header and footer.
- Never a price: a core deposit, `Core Charge`, a hazardous-material or shipping charge, `LIST PRICE`, `DISCOUNT`.
- `NOTES` lines (`THIS ITEM IS SOLD BY THE POUND`, `PRICE VALID UNTIL STOCK DEPLETED`, `ORDER -4A PER SPEC FOR ANNODIZED RIVET`): the program copies what affects the purchase into Internal Comments. They are not values of any field; ordering instructions for other part numbers never are.
- Also never taken: `Quantity`, `Add to Cart`, `CREATE LIST`, `PRICE REVIEW`, the `ProAdvantage` savings sentence, and the regional support team's phone and email.
"""

TEXTRON_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + TEXTRON_SECTION
