# DASI: test run results (Oct 29, 2026)

DASI searching now works end to end. We searched 10 parts in one bulk search and wrote the offers to the RFQ tab: 9 parts quoted (39 lines) and 1 not found.

## What the run did

- Logged in to store.dasi.com with Skylink's shared login, and logged out at the end.
- Searched all 10 parts at once with DASI's "Multiple" search, exact part numbers only.
- Set "Fulfilled by = DASI Only" and "Location = DASI Miami HQ (FL)". Lots from other warehouses are also removed in our code.
- Read the trace and tag details from the grid's hover text, so the "View" pop-ups don't need to be opened.
- Combined lots with the same condition, stock location, lead time, MOQ and price into one line. Their quantities are added up and every trace is listed (e.g. Trace: FAA 129, FAA 121).

## Numbers

| | |
|---|---|
| Parts searched | 10 (one bulk search) |
| Quoted | 9 parts, 39 RFQ lines |
| Not found | 1: NAS1352-5LE16P (DASI Miami has none: "The part is temporarily unavailable…") |
| Whole run | about 6 minutes (login about 1.5 min, bulk search about 2.5 min; most of it is DASI's slow page loads) |
| AI cost | $0.43 for the run, about $0.04 per part |

We expect more parts per search to add little extra time.

## Questions for you

1. **Lots sold by the pound.** Some DASI lots are priced per LB instead of per piece. For example, MS20470AD3-4 has 200 LB at $0.10, and DASI notes "3,900 EA PER LB". The RFQ always writes UOM = EA, so these lines are wrong as they stand. Should we skip LB lots, convert them to pieces, or quote them as they are with a note?
2. **Lots with no price ("Send RFQ").** Some lots, mostly SV (serviceable) condition, show a "Send RFQ" button instead of a price. Today they become No Quote lines. Should they be skipped, stay as No Quote, or get a note that DASI quotes them on request?
3. **Trace on combined lines.** When many lots are combined, the Trace field gets long, e.g. "FAA 129, FAA 121, CHINA SOUTHERN, REPUBLIC AIRWAYS". It's shortened to fit 50 characters, and on one line it couldn't be fitted at all and was left blank. Should combined lines list only the trace types (FAA 129, FAA 121)?
4. **Tags on combined lines.** If only some of the combined lots have a tag, we write e.g. "OEM C OF C ONLY (1 of 2 lots)". Is that acceptable, or should lots with a different tag stay on separate lines?

## What we are fixing next

- Combining will also require the same unit of measure. One MS20470AD3-4 line wrongly combined EA and LB lots.
