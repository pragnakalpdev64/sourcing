# Client RFQ sample: column review

File shared by the client: `RFQ_Spreadsheet_Sample` (24 columns).
Reviewed 2026-10-19 against SourcingAI's current **Requests** and **Results** tabs.

> **Built 2026-10-19:** the worker writes these 24 columns to an `RFQ` tab (always since 2026-10-29;
> the Results tab only with `--results`), using the
> answers marked "for now" below (see docs/PIPELINE.md §8). Alternates are copied, not searched yet.
>
> **Answered 2026-10-19 by the client's own quote-reading rules** (`inputai-skill.md`, in
> `test_runs/11_…`): Type Requested / Type Quoted, Qty Quoted, Delivery, Unit Price, Serial Number,
> Tagged By, Tag Date, Trace and the condition codes. Marked **Answered** below; the code follows them.

> **Changed 2026-10-23 by the client** (quoting rules for price breaks, delivery and substitutes):
> Unit Price = the price-break price for the quantity quoted; Delivery = `0` in stock, else the
> lead time; out-of-stock lines keep the price and condition shown; a substitute is its own line
> (B / B) and the requested part gets a `QUOTING PN B` line (qty 0, price 0.01) when it has none;
> Internal Comments in the client's wording (`MOQ 22 FOR THIS PRICE BREAK`, `STK BREA, CA: 7915`,
> `PRICE BREAKS:`). Details in docs/PIPELINE.md §8. Avsight-only fields (Part Class, Warranty,
> Min PO $, Times, Cycles, …) wait for go-live.

> **Changed 2026-10-26 by the client** (for the AvSight import), always: Serialized = `N`,
> UOM = `EA`, Type Requested = `Outright`, Condition Code = Condition Quoted; a `No Quote` line
> never has a Delivery (their CA02020A: "Part cannot be quoted or purchased at this time" next to
> a 146-day lead time). Built in `ResultRow.as_rfq_record`. Not built: recognising that sentence,
> so such a line is No Quote even with a price shown (asked the senior first).
> Also 2026-10-26: an out-of-stock line has Qty Quoted (and Quantity Requested) = the total of
> every "Qty due in", No Quote lines included (their 418-09104: 7 due in, 460 days → 7, not 0).
> Unit Price unchanged by this (senior, 2026-10-26).

> **Changed 2026-10-27 by the client** (answers to our questions): a line can be quoted when it
> has a price and a lead time; no other message makes it No Quote. Out of stock with stock due in:
> Quantity Requested = Qty Quoted = what comes in on the **soonest** date (same date: added up;
> their 418-09104 → 7, 4140073-1 → 75 of 75 + 200) and Delivery = days until that date (4140073-1
> quoted Oct 26 → 42). Out of stock with nothing due in: both quantities = the customer's (406Q80-1:
> 1 / 1, 387.04, Delivery 336, Internal Comments `336 DAY LEAD TIME, FOB: NETHERLANDS`). Built in
> `models.rfq_quote`, `rfq_delivery`, `soonest_due_in` and `pipeline.stock_note`.
> Also 2026-10-27 (Victoria): a substitute without stock is noted as `APPROVED SUBSTITUTE ALSO
> OFFERED WITH LEAD TIME: PN B` (426EN10-8 / 426EN9-8), and an out-of-stock line with a price and
> a lead time is never No Quote, also when the page shows no condition (426EN9-8: Condition Code
> left empty, Flag `NO_CONDITION`).

## Summary

SourcingAI uses two tabs today. Team members add parts to **Requests**, and SourcingAI writes one row per
vendor offer to **Results**. The client's sample is a single sheet: each row holds the customer's
request on the left (*Vendor Reference* to *Type Requested*) and the quote on the right
(*Quoted Date* to *External Comments*).

- **11 of the 24 columns already exist** in our sheets, some under another name.
- **13 are new:** 6 are request details the team member fills in Requests, and 7 are quote details for Results.

### What the sheet is probably for

An RFQ (Request For Quotation) sheet is where Skylink answers its own customer: what the customer
asked for on the left, what Skylink quotes back on the right. Validity, External Comments and the
tag and trace fields only make sense from a seller's side. It may also be the format Skylink uploads
to its ERP or to a parts marketplace such as ILS. That part is a guess.

**Proposal:** keep Requests and Results as working tabs, and later add a separate **export tab** in
the client's exact column order, filled from the Results rows a team member marks `Approved`. If the file
is imported somewhere, column order and exact headings matter, and our review columns (Flag,
Source URL, Warehouses) shouldn't be mixed in.

### All 24 columns at a glance

| # | Client column | Status | Tab | Our column |
|---|---|---|---|---|
| 1 | Vendor Reference | Have | Requests + Results | `Vendor` |
| 2 | Part Number | Have | Requests + Results | `Part Number` / `Requested Part` |
| 3 | Alternate Part Number | **New** | Requests | – |
| 4 | Keyword | **New** | Requests | – |
| 5 | Part Description | **New** | Requests (+ Results) | – |
| 6 | Serialized | **New** | Requests | – |
| 7 | UOM | **New** | Requests | – |
| 8 | Condition Code | Have | Requests | `Condition Wanted` |
| 9 | Quantity Requested | Have | Requests + Results | `Qty Needed` |
| 10 | Type Requested | **New** | Requests | – |
| 11 | Quoted Date | Have | Results | `Fetched At` |
| 12 | PN Quoted | Have | Results | `Part Number` |
| 13 | Condition Quoted | Have | Results | `Condition` |
| 14 | Qty Quoted | Have | Results | `Qty Available` |
| 15 | Type Quoted | **New** | Results | – |
| 16 | Unit Price | Have | Results | `Unit Price` |
| 17 | Delivery | Have | Results | `Lead Time` |
| 18 | Validity | **New** | Results | – |
| 19 | Serial Number | **New** | Results | – |
| 20 | Tagged By | **New** | Results | – |
| 21 | Tag Date | **New** | Results | – |
| 22 | Trace | **New** | Results | – |
| 23 | Internal Comments | Have | Results | `Internal Comments` |
| 24 | External Comments | **New** | Results | – |

---

## Columns we already have

### Vendor Reference → `Vendor` (Requests + Results)
- **Why there:** the team member picks the vendor in Requests; it's copied to every Results row.
- **Change:** for now, treated as the vendor's name. Accept "Vendor Reference" as another heading for Vendor.
- **Question:** is this the vendor's name, or the vendor's own quote or reference number? "Reference" usually means a number.

### Part Number → `Part Number` / `Requested Part` (Requests + Results)
- **Why there:** the part the customer asked for. In Results it stays next to the vendor's part number, so substitutes can be traced back.
- **Change:** none.

### Condition Code → `Condition Wanted` (Requests)
- **Why there:** what the customer wants, entered with the request. It drives the `CONDITION_DIFFERS` flag.
- **Change:** accept "Condition Code" as another heading.

### Quantity Requested → `Qty Needed` (Requests + Results)
- **Why there:** entered with the request. It's used for Price at Qty Needed and the `QTY_SHORT` and `BELOW_MOQ` flags.
- **Change:** accept "Quantity Requested" as another heading.

### Quoted Date → `Fetched At` (Results)
- **Why there:** when the vendor page was read, which is when the price was valid.
- **Change:** in an export, write the date only (ours is date and time, UTC).
- **Question:** date only, or date and time? Which time zone?

### PN Quoted → `Part Number` (Results)
- The vendor's line: either the part itself or an approved substitute.
- **Changed 2026-10-23 (client):** never a different number from Part Number. A substitute is its own line with its own number in both; look-alike numbers aren't written.

### Condition Quoted → `Condition` (Results)
- The code the vendor shows; blank when out of stock. **Change:** none.

### Qty Quoted → quantity offered (Results)
- **Answered (client's rules):** the quantity on the vendor's quote line, never a stock figure; when the vendor states none, the quantity requested.
- **Built:** the quantity requested, at most the stock (AS15001-1P: 50, not 147,282). Qty Available stays its own column in Results. A No Quote line has `0`.
- **Changed 2026-10-27 (client):** out of stock, what's due in on the soonest date (same date added up); nothing due in, the customer's quantity.

### Unit Price → `Unit Price` (Results)
- **Changed 2026-10-23 (client):** in the RFQ tab, the price-break price for the quantity quoted (50 of AS3491-01: 2.15, not the 1-piece 15.64). Results keeps one piece's price.
- **Answered (client's rules):** the vendor's price from its quote, with `$` and commas removed, so Skylink's cost; SourcingAI fills it. A No Quote line has `0.00` in their rules; we write `0` (2026-10-28).
- **Our answer:** the price of a single piece, as we do now. Keep `Price at Qty Needed` too; at qty 300 that's the price that matters.
- **Note:** some parts have no one-piece price. AN901-4C has a minimum order of 50, so its lowest price-break row starts at 50; the MOQ column shows this.
- **Question:** on this sheet, is Unit Price the vendor's price (Skylink's cost) or Skylink's selling price to its customer? If it's the selling price, SourcingAI shouldn't fill it.

### Delivery → `Lead Time` (Results)
- **Changed 2026-10-23 (client):** `0` in stock, the lead time in days otherwise. Replaces the two points below.
- **Changed 2026-10-27 (client):** out of stock with stock due in, the days from the quote date to the soonest due date (4140073-1: 42).
- **Answered (client's rules):** a number of days: in stock = `0`, weeks x 7, months x 30, a range's larger end (`3-5 weeks` = `35`).
- **Built (changed 2026-10-19, our request):** the line's lead time in days for in-stock lines too (Proponent's "if out of stock, approximate lead time is 59 days" gives `59`), instead of the client's `0`; `0` only for an in-stock line with no lead time shown. Lead Time in Results is unchanged (blank in stock).

### Internal Comments → `Internal Comments` (Results)
- Already holds substitute notes and the full price-break table. **Change:** none.

---

## New columns: request side (Requests tab)

These describe what the customer asked for, so the team member fills them in **Requests**, and SourcingAI
copies them to each Results row (as it already does for Qty Needed).

### Alternate Part Number
- **Why there:** the customer names alternates they would accept, so it belongs with the request.
- **Change:** search each alternate along with the main part number (one more part in the same bulk search, so little extra cost). Results rows for it keep the main part as Requested Part and get a flag such as `REQUESTED_ALT`. Substitutes the portal lists itself are still shown, flagged `SUBSTITUTE` as now.
- **Status:** agreed. This one can be built before the other answers come back.
- **Question:** can a row have several alternates? If so, how are they separated (comma, new line)?

### Keyword
- **Why there:** request-side detail entered by the team member.
- **Change:** copy to Results only; not used for searching.
- **Question:** what is Keyword used for? A search term when there's no part number, or a label for the team's own sorting?

### Part Description
- **Why there:** it sits on the request side, so it's probably the customer's description.
- **Change:** copy the customer's text across. Optionally, also read the vendor's own description (Proponent shows it, e.g. "RECEPTACLE"), checked against the page like other values.
- **Question:** is the description provided with the request, or should SourcingAI fill it from the vendor's page?

### Serialized
- **Why there:** whether the part is serial-number tracked; a property of the request, not something Proponent shows.
- **Change:** copy across.
- **Question:** what values does it take (Y/N)? Does a serialized part change anything, e.g. must the quote include a serial number?

### UOM
- **Why there:** unit of measure for the quantity requested (EA, FT, KT, …), on the request side.
- **Change:** copy across. Proponent shows the vendor's unit (`Um: EA`), so we could flag a mismatch (e.g. asked in FT, sold in EA), since prices and quantities would then not compare.
- **Question:** should the vendor's unit be shown or flagged when it differs? There's no "UOM Quoted" column in the sample.
- **Built (2026-10-30, LB request for MS20470AD3-4 on DASI):** each line's unit is read (`uom`). Only lines in the request's unit are written (`PC`/`EACH` count as `EA`), never converted; none in that unit: `NOT_FOUND`. UOM = the request's unit, else the line's, else `EA` (was always `EA`).

### Type Requested
- **Answered (client's rules):** Type is the kind of deal, not the condition: `Outright` or `Exchange` (the rules define it on the quote side).
- **Built:** read from a `Type Requested` column in Requests when the team member fills it; blank otherwise. No longer a copy of Condition Wanted.

---

## New columns: quote side (Results tab)

These describe the vendor's offer, so they go in **Results**. Proponent's pages don't show most of
them, so most would be blank or a fixed value per vendor.

### Type Quoted
- **Answered (client's rules):** `Outright`, `Exchange` or `No Quote`. No Quote when the line has no unit price, condition or quantity; then Condition Quoted `-` (its condition code when known, e.g. DASI SV lots without a price, since 2026-10-30), Qty Quoted `0`, Unit Price `0.00` (written as `0` since 2026-10-28).
- **Built:** `Outright` for a line with a price and condition code (Proponent only sells outright); `No Quote` otherwise (out of stock, no price shown).

### Validity
- **Why there:** how long the quoted price holds; part of the quote.
- **Change:** Proponent's portal shows no validity, so blank unless a rule is given (e.g. a fixed number of days per vendor).
- **Question:** is this the vendor's validity or Skylink's own quote validity? If it's Skylink's, who sets it?

### Serial Number
- **Why there:** serial number of the specific unit quoted.
- **Answered (client's rules):** only when explicitly stated; never inferred. Proponent doesn't show one, so blank.

### Tagged By
- **Answered (client's rules):** the release certificate's issuer and type, as written (e.g. `Boeing 8130`, `Bombardier Form 1`), max 50 characters; a company name alone also counts. Blank when no certificate is written for the line; never invented.
- **Built:** read by the model from the line's certification notes (prompt 3.12), every word checked against the part's page data. Proponent's `Civil Authorized Release Certificate true copy e.g. FAA Form 8130, EASA Form One, etc.` (85 characters) gives `Civil Auth Release Cert FAA Form 8130, EASA F1`; `No 8130 Available` and notes without a release certificate give blank (run 11 replay, 2026-10-19).

### Tag Date
- **Why there:** date of that certificate.
- **Answered (client's rules):** `YYYY-MM-DD`, only when a tag or certificate date is stated. Proponent shows none, so blank.
- **Built (2026-10-30):** DASI gives one in the Tag Details hover text (`15 OCT 1996`). The model copies it as written (`tag_date`, checked against the page) and `models.rfq_tag_date` writes `1996-10-15`; a date it can't read is written as shown.

### Trace
- **Why there:** traceability: where the part came from (e.g. OEM, airline). Part of the quote.
- **Answered (client's rules):** trace sources such as `OEM`, `MFR`, `121`/`129`, `FULL AIRLINE TRACE`, COC references (`MFR COC`, `OEM COC`) and manufacturer supply references; several joined with ", "; availability words left out.
- **Built:** read by the model from the line's certification notes (prompt 3.11), every word checked against the part's page data. Proponent: `Proponent CofC, Manufacturer CofC`, plus `Approved Mfr` when the note says so; `ATA-106 upon request` and test-report availability are left out.

### External Comments
- **Why there:** comments the customer will see, next to Internal Comments.
- **Change:** SourcingAI leaves it blank for the team member to fill. It never copies internal notes (substitute notes, price breaks, vendor cost) into it.
- **Question:** should SourcingAI ever write here, e.g. "Approved alternate offered"?

---

## Our columns not in the sample

These stay in our working tabs. They're needed to review an offer before it's quoted.

- `RFQ ID`, `Requested By`: who asked, and their reference
- `Price at Qty Needed`, `Currency`, `MOQ`: pricing context
- `Qty Available`, `Warehouses`: stock detail
- `Source URL`, `Flag`: what to check and where
- `Approved`: the team's choice of offer
- Requests only: `Status`, `Run ID`, `Notes`

---

## Questions to settle

The first two change the most and should be answered first.

1. Who is this sheet for (the customer, Skylink's ERP, a marketplace such as ILS)? Is it imported somewhere, so that column order and headings must match exactly?
2. ~~Unit Price~~: answered, the vendor's price (Skylink's cost).
3. ~~Type Requested / Type Quoted~~: answered, Outright / Exchange / No Quote.
4. ~~Qty Quoted~~: answered, the quantity offered, not the stock.
5. ~~Delivery~~: answered, days as a number; `0` in stock (client, 2026-10-23).
6. Vendor Reference: vendor name, or the vendor's quote/reference number?
7. Alternate Part Number: can there be several per row, and how are they separated?
8. Keyword: what is it used for?
9. Part Description: provided with the request, or filled from the vendor's page?
10. Serialized: what values, and does it change what we must quote?
11. UOM: show or flag the vendor's unit when it differs?
12. Validity: the vendor's validity or Skylink's own? Who sets it?
13. ~~Serial Number, Tagged By, Tag Date~~: answered, only when stated for the line.
14. ~~Trace~~: answered, trace sources and COC references from the certification notes.
15. External Comments: should SourcingAI ever write here?
16. Quoted Date: date only or date and time, and which time zone?
