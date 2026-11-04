"""DASI's own extraction prompt (store.dasi.com, vendors/dasi.py).

The client's field rules (stock, price, condition, trace, tags...) are the
shared prompt's, so a rule change reaches every vendor; what DASI's pages need
on top is the section below, which wins where the two differ. The LLM reads
lot records the adapter builds from DASI's grid (dasi.py RECORDS_JS), e.g.
"MS24665-132 lot 1: Cond: NS | Ship From: DASI Miami HQ (FL) | Lead (Days): 0 |
Trace/Tag: FAA 129 | UOM: EA | Qty Avail: 138 | MOQ: 134 | Price: $ 0.15 |
Trace: CHINA SOUTHERN AIRLINES | Tag Details: OEM C OF C ONLY" (run 12, 2026-10-29).
"""
from __future__ import annotations

from ..extract import EXTRACT_SYSTEM

DASI_SECTION = """

## 5. DASI Pages (store.dasi.com)

These rules are for DASI's results pages. Where they differ from the sections above, follow these.

### 5.1. What the Page Text Is

The program opened every part block and read its grid. The page text is a list of records:

- `Part block: <part number> (Quantity: <n>) | <description>`: one block per part found. The header's `Quantity` is DASI's total for the part, not a line's quantity: never use it.
- `<part number> lot <n>: <column>: <value> | ...`: one record per line (one offer). Take each value only from its own record.
- `<part number> lot <n> (<k> lots combined): ...`: the program combined lots with the same condition, stock location, lead time, MOQ and price into one line (client rule). It is one offer: `Qty Avail: 273 (138 + 135)` means `qty_available` `273` (the program added them up; 3.2 doesn't apply), and every other field lists each lot's value. Its `Trace/Tag` already pairs each lot's trace with that lot's own source (`Trace/Tag: FAA 129 (CHINA SOUTHERN AIRLINES), FAA 121 (REPUBLIC AIRWAYS)`), so it has no separate `Trace:`.
- `<part number>: no lots shipped from ...`: the part has no line to quote. Return no offer for it.
- Without records (no part block at all), the text is the page itself, e.g. `No Results Found`: no offers.

### 5.2. Record Fields

| Record field | Our field |
|---|---|
| `Cond` | `condition` (3.6) |
| `Qty Avail` | `qty_available` |
| `MOQ` | `moq` |
| `Price` | `price` (number only: `$ 0.15` is `0.15`) |
| `Lead (Days)` | `lead_time` (3.8): in stock `In stock (<n> days)`, else `<n> days` |
| `Ship From` | one `warehouses` entry: `name` = Ship From, `qty_on_hand` = Qty Avail |
| `Trace/Tag`, `Trace:` | `trace` (3.11): the `Trace/Tag` value, then the `Trace:` source (hover text) in brackets when the record has one: `FAA 129 (CHINA SOUTHERN AIRLINES)`; without it, the `Trace/Tag` value alone. A combined line's `Trace/Tag` is already paired, each trace with every source of its own lots: copy it as written, `DISTRIBUTOR (Aero Inventory), FAA 129 (CHINA EASTERN AIRLINES, CHINA SOUTHERN AIRLINES)`. Always the whole wording (client, 2026-10-30): no 50-character limit here (3.11 and 4.8 don't apply to DASI's `trace`), never shorten, abbreviate or drop a source or a word (`CHINA SOUTHERN AIRLINES`, not `CHINA SOUTHERN` or `CHINA E/S`), and never move a source into another trace's brackets |
| `Tag Details:` | `tagged_by` (3.12) as written, with its `(1 of 2 lots)` when a combined line shows one: e.g. `OEM C OF C ONLY (1 of 2 lots)`. A date in it is the tag date: it goes in `tag_date` as written, not in `tagged_by` (`Tag Details: 15 OCT 1996` -> `tag_date` `15 OCT 1996`), with its `(1 of 2 lots)` on a combined line. Only some records have it (the Trace/Tag hover text shows `Trace:` alone, or `Trace:` and `Tag Details:`); without it `tagged_by` is not present |
| `Trace/Tag note:` | hover text without a label: part of `trace` |
| block header description | `description` |

- `UOM` is `uom` as written (`EA`, `LB`): the record's own, never converted. Every record is the requested part itself (`listing` `requested`) unless its part number differs.

### 5.3. DASI's Own Stock, One Warehouse

- Only lines with DASI Miami HQ as the stock location are quoted (client rule). The program kept only DASI's own lots (`Fulfilled by` = DASI Only, when `--- Filter: Fulfilled by = DASI Only (set by the program) ---` is shown) shipped from the warehouse named in the records note. Return every record as a line; never add a line from anywhere else.
"""

DASI_EXTRACT_SYSTEM = EXTRACT_SYSTEM.rstrip() + DASI_SECTION
