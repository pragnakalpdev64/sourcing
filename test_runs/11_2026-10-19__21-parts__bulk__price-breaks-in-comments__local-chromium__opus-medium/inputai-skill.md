# Skill Knowledge — Aerospace/Aviation Email Processing

> **Type**: Static SOP  
> **Updated by**: Human (manual review only)  
> **Consumed by**: `analyze`, `classify`, `extract` nodes  
> **Last reviewed**: 2026-08-03

---

## 1. Classification Rules

### Email Types
| Type     | Description                                                                 |
|----------|-----------------------------------------------------------------------------|
| RFQ      | Customer requesting pricing/availability for specific part numbers          |
| Quote    | Vendor/supplier responding with pricing offers                              |
| Inquiry  | General question — no structured line items expected                        |
| Other    | Auto-reply, OOO, newsletter, internal thread, non-actionable content        |

### Skip Conditions (route to `skip` node)
- Subject contains "Re:" with no new human-written content
- Body is an auto-reply or out-of-office message
- No attachments AND body is fewer than 20 meaningful words
- Email is a delivery/shipping notification only
- Email is a newsletter or marketing blast

---

## 2. Source Prioritisation Rules

### Line Item Extraction
- **CRITICAL**: Extract **ONE record per line item** (per part number row) quoted — every row in a quotation table, purchase order, PDF/attachment item list, or numbered sequence (Item 1, Item 1.1, Item 2, etc.) with its own part number MUST produce its own object in the `line_items` array, even if it is a sub-item, alternate, or core component.
- **Never merge** two rows with different part numbers into one line item, even if they appear related (e.g., a main part and its core return part).
- If the same part number is quoted with **different** condition, price, quantity, delivery, or type → keep ALL as **separate** objects in `line_items`.

### Attachment Handling
- **Include**: Quotations, purchase orders, RFQ sheets, BOMs, Excel files, item lists
- **Exclude**: Certificates, compliance documents, datasheets, non-transactional supporting docs
- Always extract from the **Email Body**, regardless of attachments
- **ABSOLUTE PRIORITY RULE**: If the same part number appears in both the Email Body and a qualifying attachment, the Email Body is authoritative — never override its condition, price, quantity, delivery, or comments values with a conflicting attachment value.
- **Attachment-fillable fields** (exhaustive, not illustrative): use the attachment only to populate these fields when completely missing from the Email Body: `serial_number`, `tag_date`, `tagged_by`, `trace`, `warranty`, `core_due`, `core_due_date`, `contact_person`.
  - For `contact_person` specifically: only take the attachment value if the Email Body / header-derived value is a placeholder or invalid per the `contact_person` rules in Section 5 — never override a validly extracted header-derived contact name with an attachment value.
- Combine all qualifying sources into a **single deduplicated list**
- Remove duplicates only when ALL fields are exactly identical

---

## 3. Field Extraction Rules

### Delivery Days (`delivery_days`)
- **DELIVERY PRIORITY**: Extract `delivery_days` ONLY from information describing delivery, lead time, shipment, availability, or estimated delivery. Ignore quote validity, warranty, payment terms, net terms, core due, certification dates, tag dates, expiration dates, and other non-delivery dates. If multiple timelines are present, use only the one explicitly referring to delivery or lead time.
- Unit conversion (DAYS vs WEEKS vs MONTHS) and range handling are fully specified per-pattern in the table below — follow it exactly, including which rows do **not** get multiplied.

| Input Pattern                          | Output Value                                    |
|----------------------------------------|-------------------------------------------------|
| "in stock" / "stock" / "immediate"     | `0`                                             |
| Exact or business days (e.g. "90 days", "7 business days") | the stated number (do NOT multiply) |
| Range or range business days (e.g. "4-5 DAYS", "7-10 business days") | maximum number (do NOT multiply) |
| Weeks (e.g. "2 weeks")                 | `14` (multiply by 7)                            |
| Range weeks (e.g. "3-5 weeks")         | `35` (5 × 7)                                    |
| Months (e.g. "3 months")              | `90` (multiply by 30)                           |
| Range months (e.g. "2-4 months")       | `120` (4 × 30)                                  |
| Specific future date                   | Calendar days from email sent date (inclusive)  |
| Month only (e.g. "MAY")               | Days until last day of that month               |
| Month range (e.g. "MAY/JUNE")         | Days until last day of final month              |
| "ARO" with a day-based timeline (e.g. "7-10 days ARO") | Extract the stated timeline (e.g., `10`)  |
| "ARO" only (no specific timeline)      | Omit field + add to `internal_comments`          |
| "Call for delivery" / "Contact us"     | Omit field + add to `internal_comments`          |

**Reference-date clarifications**:
- "Inclusive" counting applies to explicit dates and to the Month/Month-range rows: both the sent date and target date count as full days (e.g., sent Jan 1, stated delivery Jan 5 → `delivery_days = 5`, not 4); Month/Month-range rows count through the last day of the stated (or final) month.
- If an ARO timeframe uses a non-day unit (e.g., "3-4 weeks ARO", "2 months ARO"), apply the Weeks/Months conversion rules above rather than the "ARO with timeline" row — that row applies only to bare day-based ARO figures.

### Condition Codes
- Only accept exact codes: `OH`, `SV`, `NE`, `AR`, `NS`, `FN`, `RP`
- Do **not** infer from generic or unrelated words not listed in the Variations column below (e.g. "Service" alone ≠ `SV`, "Repair" alone ≠ `RP`) — match only the exact code or a listed variation, as a **complete phrase** (not a substring), checking the longer/more specific phrase first so "New Surplus" maps to `NS`, not `NE` via the word "New" inside it.
- Matching is case-insensitive (e.g. "new", "NEW", "New" all match `NE`).
- Return the code only; if not found → omit field
- Use the quoted condition as the authoritative value for `condition_quoted` — this is the single field these rules populate.

| Code | Meaning       | Variations to Recognize                                    |
|------|---------------|--------------------------------------------------------------|
| AR   | As Removed    | "As Removed", "AR"                                            |
| FN   | Factory New   | "Factory New", "FN"                                           |
| NE   | New           | "New", "NE"                                                   |
| NS   | New Surplus   | "New Surplus", "NS", "Govt New Surplus"                       |
| OH   | Overhauled    | "Overhauled", "OH"                                            |
| RP   | Repaired      | "Repaired", "RP"                                              |
| SV   | Serviceable   | "Serviceable", "SV" |

### Transaction Type (`type_quoted`)
| Condition                                                                 | Value       |
|---------------------------------------------------------------------------|-------------|
| A line `Selected Price Type: Exchange` is present                         | `Exchange`  |
| A line `Selected Price Type: Outright` is present                         | `Outright`  |
| A line `Checked: No Quote This Item` is present                           | `No Quote`  |
| Supplier text contains: "exchange", "core return", "core charge", "plus cost", "flat rate exchange", and no `Selected Price Type` line is present | `Exchange`  |
| No quote provided for the line item                                       | `No Quote`  |
| All other cases                                                           | `Outright`  |

An unselected form label such as "Exchange" next to a selected "Outright" radio is not a quote type. When `Selected Price Type` is present, use that value and ignore the other price-type words in the form. When `Selected Condition` is present, use that condition code instead of other condition options listed in the form.

#### Outright RFQ Exception
If the email or quote contains:
- "Price Type: Make Offer"
- "What is your price?"
Then set `type_quoted = "Outright"`. This rule overrides the No Quote Conditions, even when unit price is not provided.

#### No Quote Conditions
If a part number is provided but any of the required quote fields are missing:
- condition code
- unit price
- quantity
OR the vendor explicitly states "no quote" or "no bid"
Then set:
- `type_quoted` = "No Quote"
- `condition_quoted` = "-"
- `quantity_quoted` = 0
- `unit_price` = 0.00

These four fields are always output for No Quote line items — see Section 6 → Output Rules.

Do not include "QTY & PRICE TBA" in internal comments if it is not mentioned in the no-quote response.

### Exchange Type (`exchange_type`) — only when `type_quoted = Exchange`
**Output constraint**: when populated, this field must be exactly `Cost Plus` or `Flat Rate w/[COND] Core` (including the `Flat Rate w/Repairable Core` variant per Rule 3) — omit the field for anything else.

Apply top-down; the first matching rule wins:

1. Text contains "Cost Plus", or otherwise describes core repair cost billed on top of the exchange price (e.g., "plus cost to repair core") → `Cost Plus`
2. Text contains "Flat Rate" + any valid condition code → `Flat Rate w/[COND] Core`
3. Text contains "Flat Rate" + "Repairable" → `Flat Rate w/Repairable Core`
4. Pricing context or condition not clearly present → omit field

### Pricing
- Strip `$` and commas before storing: `"$25,237.00"` → `25237.00`
- Extract `outright_price` whenever it is explicitly stated — see Field Population below for how this interacts with `type_quoted`.

### Field Population — Content-Driven, Not Type-Gated
- `exchange_type`, `core_due`, `core_due_date`, and `outright_price` are each extracted independently, based only on whether they're explicitly stated in the content — if stated, extract it; if not, omit it. Never force-null a field purely because of the line's `type_quoted` classification.

---

## 4. Certification & Traceability

### `tagged_by` — Priority Order
1. Tagged by / Tag by
2. Tag Src
3. Tag Type (only if Tagged by is missing)
4. Trace Type
5. Cert Issuer / Issuing Entity
6. Tag Info
7. Tag
8. If none of the above exist, search in Comments, Remarks, or any field containing certification details.
9. If two or more tagged_by-related fields are present for the same line item, return a single combined tagged_by value.
   - Use the organization/issuer from fields such as Tagged By, Tag Src, or Cert Issuer / Issuing Entity.
   - Append all applicable certification details from Tag Details, Tag Type, Cert Source, Trace Type, Tag Info, or Tag.
   - Combine only fields belonging to the same quoted line item.
   - Do not combine unrelated company names, customers, buyers, operators, ship-to parties, or unrelated vendors with certification details.
   - Do not duplicate certification details if they already appear in the organization/issuer field.
   - Remove only non-essential company suffixes (Inc., LLC, Ltd., Corp., Corporation, GmbH, etc.) when needed.
   - If multiple certification descriptors are present, preserve all applicable certification information.

**Combined Field Examples**:
- Tag Src: `The Boeing Company`, Cert Source: `8130-3 OEM` → `tagged_by = "The Boeing Company OEM 8130-3"`
- Tagged By: `JJA Aviation Inc`, Tag Type: `Fresh Tag FAA/EASA` → `tagged_by = "JJA Aviation Inc Fresh Tag FAA/EASA"`
- Tag Src: `Bombardier Inc.`, Tag Details: `Form 1` → `tagged_by = "Bombardier Form 1"`

**CRITICAL RULE FOR COMPANY NAMES WITHOUT CERT TYPES**:
If the label (e.g., "Tag:", "Tagged by:", "Tag Src:") is followed ONLY by a company/organization name (e.g., "GLOBAL FILTRATION, INC.") and no certification type (like 8130 or FORM 1) is explicitly stated, you MUST extract that full company name as `tagged_by`.
- Do NOT hallucinate or infer a certification type (e.g., do not output "8130" unless "8130" is explicitly written).
- Do NOT move the company name to `internal_comments`; it belongs in `tagged_by`.

If a certification type (e.g., "8130", "8130-3", "FORM 1", "DUAL RELEASE", "EASA FORM 1") is mentioned together with, or in a nearby line within the same line-item block as, an organization, company, or certificate issuer, extract BOTH together as `tagged_by` in the format:
`<Organization/Issuer> <Certification Type(s)>`

**Organization + Certification Type Examples**:
- Source: `"COND: OHC 7I26 C&C AEROSYSTEMS"`, `"TRACE: 129 OPERATOR"`, `"8130 DUAL RELEASE, 1 YEAR WARRANTY"` → `tagged_by = "C&C AEROSYSTEMS 8130 DUAL RELEASE"`
- Source: `"FAA / 8130-3 DUAL RELEASE CERTS FULL AIRLINE TRACE AVAILABLE"` → `tagged_by = "FAA 8130-3 DUAL RELEASE"`
- Source: `"Boeing 8130"` → `tagged_by = "Boeing 8130"`

**Extraction Rules**:
- Extract the EXACT text exactly as it appears in the source document.
- Preserve EXACT casing (do not convert to uppercase).
- Do NOT remove any adjectives, words, or descriptors, UNLESS it exceeds the length limit.
- Remove only dates from the extracted value, if present.
- If both "Tagged by" and "Tag Type" are present, you MUST extract the value from "Tagged by" and ignore "Tag Type".
- If no certification is written anywhere on that line, leave `tagged_by` blank. Do not invent one, and do not treat a standalone "145" as `tagged_by`.
- A certification written in that line's Note, Comments, or Remarks is explicit. Put it in `tagged_by`, not only in `internal_comments`. Example: `Note: Fresh Dual Release 8130` → `tagged_by = "DUAL RELEASE 8130 FRESH"` and `internal_comments` empty.
- Do not invent comment text. If that note is only the certification, leave `internal_comments` empty. If the line also has a separate written note, keep that note as written.

### Field Length Limit (`tagged_by` and `trace`)
Both fields must not exceed 50 characters. If a value exceeds the limit, shorten it — abbreviate, never truncate — using the field-specific abbreviations below, removing non-essential descriptors as needed while preserving the core organization/certification (`tagged_by`) or trace source (`trace`) information.

- `tagged_by` abbreviations: "DUAL RELEASE" → "DR", "8130-3" → "8130"
- `trace` abbreviations: "Certificate of Conformance" → "C of C", "Dual Release" → "DR", "Form One" → "F1", "EASA & TC" → "EASA/TC"

### `tag_date`
Extract the date associated with the certification/tagging.
Format: `YYYY-MM-DD`

Rules:
- If month, day, and year are all provided, preserve the exact date.
- **Month/Year vs Month/Day disambiguation** for two-segment dates (e.g. "3/26"): only interpret as month/year when (a) the value appears directly next to an explicit tag/cert-date label (e.g. "Tag Date:", "Cert Date:"), AND (b) the second number is exactly 2 digits with no other day-of-month context nearby. If the same two-segment date appears without a clear tag/cert-date label, or could plausibly be a day (e.g. near a delivery or quote-validity date in M/D format), do NOT guess — omit `tag_date` and note the ambiguous value in `internal_comments` instead.
  - Example: `"TAG DATE: 3/26"` → `2026-03-01`.
  - Example: `"Ships 3/26"` (no tag/cert label) → do not populate `tag_date` from this value.
- If only the month is provided, use the first day of that month, inferring the year from the email's sent date (the sent date is always available on an email).
- If no tag or certification date is present, omit the field.

### `trace` — Search Priority Order
1. "Trace" / "Trace To" field
2. "Cert Source" field
3. "Cert" field (only if it contains a company, org, or valid trace descriptor)
4. Comments, Remarks, Notes, or other text within the same line-item block containing explicit traceability information.

**Extraction Rules**:
- Extract ALL traceability documentation, traceability references, or trace sources found.
- Extract the COMPLETE values (do not truncate).
- Return ALL valid values found as a single string. If multiple trace sources exist, separate them with **comma and space** (e.g., `"LEARJET, COMPANY COC"`).
- Normalize the delimiter to comma and space if sources are already separated differently in the source.
- If the email comments or body contains generic traceability descriptors (such as "MFG CERTS", "MFR CERTS", "OEM CERTS", "MFG COC", "MFR COC", "OEM COC"), extract the corresponding full values.
- If an explicit traceability phrase appears anywhere in the line-item content, including Comments, Remarks, Notes, or other related text, extract the complete traceability phrase into `trace`, excluding only availability or status wording such as "AVAILABLE" or "PROVIDED".
- Return blank if nothing valid is found.

**Valid extractions include**:
- Trace identifiers (e.g., "129", "121 Trace", "145")
- Trace destinations (e.g., "OEM", "MFR", "Manufacturer")
- Explicit trace availability descriptions (e.g., "FULL AIRLINE TRACE", "FULL TRACE", "AIRLINE TRACE")
- COC references (e.g., "COMPANY COC", "MFR COC", "OEM COC")
- Manufacturing cert sources (e.g., "BOEING", "AIRBUS")
- Vendor names or cert issuer names
- Manufacturer supply references: Ex-Works Mfr, Direct from Manufacturer, Supplied from Manufacturer, Manufacturer Supply, Manufacturer Paperwork references

**Do NOT include**:
- Dates
- Document numbers unrelated to the trace source
- Full sentences or context
- Vendor location or contact info

#### SPECIAL EXCEPTION — EASA Part 145
Always extract EASA Part 145 phrases into `trace`, NOT `tagged_by`.
Valid EASA Part 145 phrases include:
- EASA Part 145
- EASA Part 145 Repair Station
- EASA Part-145 Approved Repair Station
- Any similar "EASA Part 145..." phrase

**Example**:
- Source: `"Tagged by EASA Part 145 Repair Station"`
- Correct: `trace = "EASA Part 145 Repair Station"`, `tagged_by = ""` (blank)

---

## 5. Special Field Handling

### `serial_number`
- Extract **only** if explicitly stated in the quote
- Do NOT infer, generate, or use placeholders
- If not explicitly mentioned → return blank

### `warranty`
- Extract duration only in short format (max 30 characters)
- Examples: `"6 Months"`, `"1 Year"`, `"90 Days"`
- Do NOT include full warranty sentences, conditions, flight hours, or additional terms

### `core_due`
- Return number of days as integer only
- Extract only from phrases explicitly tied to core return/exchange (e.g., "core due in 15 days", "return core within 30 days") → return `15` or `30`
- Do NOT extract from general payment terms (e.g., "Net 30", "Net 60") — those refer to invoice payment terms, not core return deadlines, even though the phrasing looks similar
- Typically only present when `type_quoted = "Exchange"` — see Field Population in Section 3

### `core_due_date`
- Format: `YYYY-MM-DD`
- Extract **only** if a literal date is explicitly stated in the quote (e.g., "core due by 2026-05-01", "return core by May 1, 2026") — do **not** calculate or derive it from `core_due` (days) or the email sent date
- If no explicit date is stated, omit the field entirely

### `contact_person_email`
- Always lowercase

### `contact_person`
- Default to the provided `contact_person`/`contact_person_email` values, used as-is. The fallback below only applies when they're missing, blank, a bare local part, or a generic system/auto-generated label (e.g. "ilssales", "Auto-Quote", "AVFLEX Auto-Quote") with no real name mentioned elsewhere.
- In that case: use a real name or department from the vendor's own body signature if tied to that address, otherwise fall back to a readable version of the local part (e.g. "ilssales" → "ILS Sales"). Never output a raw email address or local part as the final value.
- Never override a valid provided `contact_person`/`contact_person_email` with a different name or address found elsewhere in the email thread — including forwarded or internal messages. Never output an `@skylinkintl.com` address as `contact_person_email`, and never use an internal Skylink staff member's name as `contact_person`, even if they appear as the most visually prominent sender (e.g. the person who forwarded the email internally).

### Part Numbers (`part_number_quoted`)
- Use the quoted part number as the authoritative value.
- Extract exactly as written but do not include availability statuses, condition codes, remarks, or annotations. (e.g. `"part-number(STOCK)"` → `"part-number"`)

### `quantity_quoted`
- Use the quantity stated on the vendor's own quote line. That stated quantity overrides any older requested quantity elsewhere in the thread, including a forwarded or quoted original message.
- If the vendor's reply does not state a quantity for that line, use the quantity requested in the original message for that part.
- Do not use a stock or availability phrase as the quoted quantity.

### FOB / Stock Location
- If the email or PDF mentions "STOCK" or "FOB" followed by a city, state, or country (e.g., "FOB Miami", "STOCK UK", "FOB Los Angeles, CA"), extract the full stock location phrase and include it in the `internal_comments` field.

### Internal Comments Rules
- Do not duplicate information already captured in structured fields within `internal_comments`.
- Remove New List Price notes (e.g., "New List $194.88ea", "List Price $XXX", "Current List Price $XXX") when the value is already captured in pricing fields.
- Cert date / expiration date may remain in `internal_comments` even if also captured in Tag Date.
- Keep only additional notes, terms, restrictions, qualifications, or context not represented elsewhere.
- If the originally quoted condition is "NS": Do NOT add any PPW-related internal comment (e.g., "PPW NOT SPECIFIED").

---

## 6. Output Rules

Note: strict JSON-only output and the null/empty-field omission rule are enforced by the extraction prompt template itself — not repeated here to avoid stating the same rule twice.

- **No Quote line items**: output the four placeholder fields (`type_quoted`, `condition_quoted`, `quantity_quoted`, `unit_price`) exactly as defined in Section 3 → No Quote Conditions. This is the only case where placeholder (non-extracted) values are output — it does not authorize placeholder values anywhere else in the document.
- Header fields (defined in the accompanying output schema) appear **once** at the top level — not repeated per line item.

---

## 7. Routing Logic (for `analyze` / `classify` nodes)

```
analyze output:
  → has_content AND not auto-reply → classify
  → is auto-reply OR too short OR no parts → skip

classify output:
  → type is RFQ or Quote → extract
  → type is Inquiry or Other → skip

extract output:
  → has line_items → validate (regardless of confidence — low-confidence extractions with real line items need MORE scrutiny, not less; `confidence` is still passed through for `validate` to use internally)
  → no line_items → output (skip validation)
```