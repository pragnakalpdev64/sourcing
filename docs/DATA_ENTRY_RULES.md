# SourcingAI data entry rules

The client's checklist for what we extract and write to the sheet (received 2026-10-28). Every
result we give must follow these rules. Each rule is checked by tests in
`tests/test_data_entry_rules.py`, named `test_rule_<n>_...`, so a failing test names the rule
that a code change broke.

- A test fails after a change: fix the code, not the test.
- The client changes a rule: change this file and its tests together.
- Rules marked **Gap** aren't met yet. Their tests are marked `GAP` (expected to fail). Once the
  code is fixed the test starts passing, and pytest reports it as failed until the `@GAP` marker is
  removed. After that, the rule is protected like the others.

```bash
.venv/bin/python -m pytest -q tests/test_data_entry_rules.py -rxX   # lists the open gaps too
```

## The rules (client's text)

1. **Quantity:** Quantity Requested must always equal Quantity Quoted. Use the full available stock
   quantity.
2. **Incoming Stock:** If incoming quantities have the same Date Next In, combine them. If dates
   differ, use only the quantity arriving soonest.
3. **MOQ and Pricing:** Calculate the unit price based on the customer's requested quantity,
   available stock, and MOQ. Include the requested quantity, MOQ, and all price breaks in Internal
   Comments.
4. **Delivery:**
   - In stock: Delivery = 0.
   - Out of stock with incoming stock: use the earliest incoming quantity and its lead time.
   - Out of stock without incoming stock: use the customer's requested quantity for both quantity
     fields and enter the lead time in Delivery.
   - Include `X DAY LEAD TIME, FOB: [WAREHOUSE LOCATION]` in Internal Comments when applicable.
5. **No Quote:** If a valid price and lead time are available, quote the part even if it is out of
   stock. Mark it as No Quote only when the supplier indicates it cannot be quoted or purchased.
   Leave Delivery Date blank for No Quote items.
6. **Approved Substitutes:** Never put a substitute part number in the requested part's PN Quoted
   field. Create two separate quote lines: one for the requested part and another for the
   substitute. If the substitute is out of stock, mention its lead time in Internal Comments.
7. **AvSight Fields:** Always use Serialized = N, UOM = EA, Type Requested = Outright,
   Condition Code = Condition Code Quoted.

## DASI rules (client, 2026-10-29)

- **DASI 1, combining lines:** if pricing, stock location and MOQ are the same, lines can be
  combined. If there are several trace sources, e.g. 121 and 129, Trace = `121, 129`.
- **DASI 2, stock location:** only enter lines with DASI Miami HQ as the stock location.

Both are applied while DASI's grid is read (`vendors/dasi.py`, `_records_text` / `_combine`), so
the LLM gets one record per line. Combined lines also need the same condition, lead time and UOM
(our addition: one RFQ line has one condition code and one delivery, and EA and LB quantities
are never added up); their quantities are added up
("273 (138 + 135)"), each lot's trace source stays with its own trace (`FAA 129 (CHINA
SOUTHERN AIRLINES), FAA 121 (REPUBLIC AIRWAYS)`, client 2026-10-30), and a tag only some of the lots have is marked (`OEM C OF C ONLY (1 of 2
lots)`). Tests: `test_rule_dasi_1_...`, `test_rule_dasi_2_...`.

## How the code follows them

| Rule | Where | Status |
|---|---|---|
| 1 Quantity | `models.ResultRow.as_rfq_record`, `models.rfq_quote` | Met on quoted lines. **Gap:** a No Quote line with Qty Quoted 0 keeps the customer's quantity in Quantity Requested |
| 2 Incoming stock | `models.soonest_due_in` | Met (same date added up across warehouses; soonest date only) |
| 3 MOQ and pricing | `pipeline.quote_price`, `moq_note`, `price_break_note` | Price and price breaks met. **Gap:** the quantity asked is only in the comments when it's below the MOQ (`(ASKED 10)`) |
| 4 Delivery | `models.rfq_delivery`, `pipeline.stock_note` | Met |
| 5 No Quote | `models.rfq_quote` | Out of stock with price + lead time quoted, no Delivery on No Quote: met. **Gap:** an in-stock priced line whose page shows no condition is No Quote; we never read the supplier's own "can't buy" signal |
| 6 Approved substitutes | `pipeline.alternate_notes` | Met (lead time in the requested part's comments since 2026-10-28) |
| 7 AvSight fields | `models.ResultRow.as_rfq_record`, `pipeline.same_uom` | Met. **Changed 2026-10-30 (LB requests):** UOM = the request's unit, else the line's, else `EA` (`PC`/`EACH` count as EA); only lines in the request's unit are written (a line showing no unit counts as EA), never converted; none in that unit: `NOT_FOUND` |

Not covered by the rules: the line added when the portal lists only a substitute (requested
part, Qty Quoted 0, Unit Price 0.01, `QUOTING PN B`, client 2026-10-23) keeps the customer's quantity
in Quantity Requested.
