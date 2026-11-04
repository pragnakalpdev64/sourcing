# Proponent: 52 parts, one-by-one vs bulk search, Opus 5.5 vs Sonnet 5.5

Analysis of test runs **07** and **08** (2026-10-14). The same 52 parts were searched on Proponent
four times: two search modes, each with two LLMs. The raw logs, per-part reports and saved pages
are in the two run folders (see [§12](#12-files-and-how-to-reproduce)).

---

## Contents

1. [Summary](#1-summary)
2. [Test setup](#2-test-setup)
3. [Results at a glance](#3-results-at-a-glance)
4. [Speed](#4-speed)
5. [LLM cost and tokens](#5-llm-cost-and-tokens)
6. [Agreement between runs](#6-agreement-between-runs)
7. [Problems found](#7-problems-found)
8. [Login, session and reliability](#8-login-session-and-reliability)
9. [Recommendation](#9-recommendation)
10. [Limits of this test](#10-limits-of-this-test)
11. [Per-part results (all 52 parts)](#11-per-part-results-all-52-parts)
12. [Files and how to reproduce](#12-files-and-how-to-reproduce)

---

## 1. Summary

| | One by one, Opus | One by one, Sonnet | **Bulk, Opus** | **Bulk, Sonnet** |
|---|---|---|---|---|
| Result | 50 done, 2 not found | 52 done | 51 done, 1 not found | 52 done |
| Correct statuses | 51 / 52 | 51 / 52 | **52 / 52** | 51 / 52 |
| Whole run | 1158s (19.3 min) | 973s (16.2 min) | 388s (6.5 min) | **349s (5.8 min)** |
| Per part | 22.3s | 18.7s | 7.5s | **6.7s** |
| LLM cost | $1.5018 | $0.6785 | $0.7713 | **$0.3484** |
| LLM cost per part | $0.0289 | $0.0130 | $0.0148 | **$0.0067** |

- **Bulk search is about 3x faster and costs about half as much in LLM fees**, for both models,
  with the same offers as one-by-one search.
- **Both models extracted the same offers on 50 of 52 parts.** The only other differences are
  stock quantities that changed on the portal between runs.
- **Sonnet costs about 2.2x less than Opus** but made one mistake in both runs: a part the portal
  doesn't recognise (row 46, `0684-12-28`) was marked DONE with an empty offer instead of
  NOT_FOUND. This can be fixed in code (see [§7.2](#72-sonnet-marks-an-unrecognised-part-as-done)).
- **One one-by-one search read the results grid before it had loaded** and reported a part as not
  found when it was in stock (row 30, Opus). This is a timing problem in the search code, not a
  model problem (see [§7.1](#71-false-not_found-when-the-results-grid-loads-slowly)).
- **No CAPTCHA, no 2FA, no login failures**, and no failed sheet writes in any of the four runs.
- **Not done yet:** checking the extracted values against the portal by hand. This report compares
  the runs with each other. Where all four agree, a wrong value would have to be wrong in the
  same way four times.

**Recommendation:** use bulk search (25 parts per search) for runs of 25 or more parts. Use
Sonnet once the empty-offer fix is in; until then Opus is the safer choice. Details in [§9](#9-recommendation).

---

## 2. Test setup

| Item | Value |
|---|---|
| Vendor | Proponent, `https://procart.proponent.com` |
| Login | Skylink shared account, from GCP Secret Manager (`sourcing-vendor-test`, via `VENDOR_SECRET_OVERRIDES`) |
| Sheet | Google test sheet `…aF7CVqak`, `Requests` rows 2–53 (52 parts) |
| Browser | Browserbase, session recording on, saved Context |
| Models | `claude-opus-5-5` and `claude-sonnet-5-5`, both `LLM_EFFORT=medium` |
| Store | local file store (`.local_store.json`) |
| Pages | `--dump-pages`: every page or grid text the model read was saved |

**Run 07, one by one:** each part's results page is opened directly
(`PartSearchResults.aspx?id=<part>`), the jqGrid records are read, and one LLM call is made per part.

**Run 08, bulk:** the parts are typed into Proponent's BULK SEARCH box, 25 per search
(`--bulk 25 --set proponent:bulk_batch_size=25`), so 3 searches of 25 + 25 + 2 parts. The grid is
set to 50 results per page and its records are read; then 1–2 LLM calls are made per search. Parts
a bulk search misses are searched again one by one before logout.

| Run | Model | Run id | Started | Finished |
|---|---|---|---|---|
| 07 | Opus | `local-20261014-111129-6fa4` | 16:41:29 | 17:00:52 |
| 07 | Sonnet | `local-20261014-122426-3f12` | 17:54:26 | 18:10:43 |
| 08 | Opus | `local-20261014-132418-040c` | 18:54:18 | 19:00:49 |
| 08 | Sonnet | `local-20261014-133949-9f88` | 19:09:49 | 19:15:44 |

Times are local (IST). The four runs ran one after another within 2.5 hours, so live stock on the
portal can change between them.

---

## 3. Results at a glance

| | 07 Opus | 07 Sonnet | 08 Opus | 08 Sonnet |
|---|---|---|---|---|
| DONE | 50 | 52 | 51 | 52 |
| NOT_FOUND | 2 | 0 | 1 | 0 |
| FAILED | 0 | 0 | 0 | 0 |
| Offers written | 77 | 79 | 78 | 79 |
| – for the requested part number | 51 | 53 | 52 | 53 |
| – alternates / related part numbers | 26 | 26 | 26 | 26 |
| – in stock with a price | 51 | 52 | 52 | 52 |
| – out of stock (lead time only) | 25 | 25 | 25 | 25 |
| – empty (no price, qty or condition) | 1 | 2 | 1 | 2 |
| Prices written with "USD" in them | 0 | 11 | 0 | 5 |

- **Status counts:**
  - Opus's extra NOT_FOUND in run 07 is row 30, which is false ([§7.1](#71-false-not_found-when-the-results-grid-loads-slowly)).
  - Sonnet's extra DONE is row 46, which is wrong ([§7.2](#72-sonnet-marks-an-unrecognised-part-as-done)).
- **Alternates:** the 26 alternate lines are the same in all four runs. They are the related part
  numbers Proponent itself lists on the results page.
- **Empty offers:**
  - The empty offer on row 52 is in all four runs. It is a variant spelling
    (`71550KE-10T-05SN-LC`) that the portal marks "Part currently not supported", with no stock.
  - Sonnet's second empty offer is row 46.
- **"USD" in prices:** this is a formatting difference only. The number is the same
  ([§7.3](#73-sonnet-sometimes-writes-usd-into-the-price)).

---

## 4. Speed

### 4.1 Whole run

| | 07 Opus | 07 Sonnet | 08 Opus | 08 Sonnet |
|---|---|---|---|---|
| Whole run | 1158s | 973s | 388s | 349s |
| Per part | 22.3s | 18.7s | 7.5s | 6.7s |
| Estimate for 100 parts | ~37 min | ~31 min | ~13 min | ~11 min |
| Estimate for 1,000 parts | ~6.2 h | ~5.2 h | ~2.1 h | ~1.9 h |

"Whole run" covers everything from reading the sheet to releasing the browser: login, searches,
LLM calls, sheet writes and logout. The estimates scale the measured per-part time linearly.

### 4.2 Where the time goes: one by one (run 07, average per part)

| Step | Opus | Sonnet |
|---|---|---|
| Open the part's results URL | 8.5s | 7.8s |
| Expand details and read the grid | 5.7s | 5.4s |
| LLM call | 5.2s | 2.7s |
| Write the row to the Google Sheet | 1.8s | 1.8s |
| **Total per part** | **~21s** | **~18s** |

The browser accounts for about two-thirds of the time. Sonnet answers about 2.5s faster per call than Opus.

### 4.3 Where the time goes: bulk (run 08, one batch of 25 parts)

| Step | Opus | Sonnet |
|---|---|---|
| Type 25 parts into the bulk box and submit | 11s | 11s |
| Results page loads | 15–17s | 17s |
| Set 50 results per page, expand details, read grid | 24s | 24s |
| LLM (2 calls for 25 parts) | 56–59s | 26–34s |
| Write 25 rows to the Google Sheet | 43s | 40s |
| **Total per batch** | **~149s (6.0s/part)** | **~118s (4.7s/part)** |

- **Sheet writes:** in bulk mode about a third of the time goes to writing rows to the sheet one at a
  time (~1.6s per row). Writing a batch's rows in one call would save about 35–40s per 25 parts
  (see [§9](#9-recommendation)).
- **Batch time in the per-part reports:** each part in a bulk run shows the whole batch's search
  time (e.g. 77.6s), so compare bulk and one-by-one by the whole-run time, not that column.

### 4.4 Fixed costs per run

| Step | Time |
|---|---|
| Read the sheet, open a Browserbase session | ~6–10s |
| Login (when needed) | ~26–36s |
| Logout and release the session | ~8s |

In run 07 Opus the saved Browserbase Context was missing, so a fresh one was created and the cookie
banner was declined automatically. Run 08 Opus skipped login: the Context was still signed in from
an earlier attempt that had not logged out ([§8](#8-login-session-and-reliability)).

---

## 5. LLM cost and tokens

| | 07 Opus | 07 Sonnet | 08 Opus | 08 Sonnet |
|---|---|---|---|---|
| LLM calls | 52 | 52 | 6 | 6 |
| Input tokens | 282,099 | 282,632 | 113,866 | 113,964 |
| Output tokens | 18,671 | 11,322 | 15,793 | 12,043 |
| Input tokens per part | 5,425 | 5,435 | 2,190 | 2,192 |
| Output tokens per part | 359 | 218 | 304 | 232 |
| **LLM cost** | **$1.5018** | **$0.6785** | **$0.7713** | **$0.3484** |
| Per part | $0.0289 | $0.0130 | $0.0148 | $0.0067 |
| Estimate for 1,000 parts | ~$28.90 | ~$13.00 | ~$14.80 | ~$6.70 |

- **Bulk uses ~60% fewer input tokens per part.** The page's fixed text (header, menus, pager) is
  sent once per 25 parts instead of once per part.
- **Opus vs Sonnet:** Opus costs about 2.2x what Sonnet costs in both modes.
- **Where the costs come from:** the costs are the run's bill from the log's run summary. In bulk runs the per-part
  report splits each batch's cost across its rows and adds up slightly lower ($0.7566 / $0.3414),
  so use the log figure for totals.
- **Not included:** Browserbase minutes (19.3 / 16.2 / 6.5 / 5.8 min of session time for these
  runs), which are billed separately.
- **Earlier runs for comparison:** run 06 measured Haiku at about $0.001 per part (one by one,
  20 parts). Haiku wasn't part of this test.

---

## 6. Agreement between runs

The four runs gave **identical offers on 46 of 52 parts**: same part numbers, prices, quantities,
conditions and lead times. The 6 parts that differ:

| Row | Part | What differs | Cause |
|---|---|---|---|
| 2 | AS15001-1P | qty 151,632 (07 Opus) vs 150,132 (later runs) | Live stock changed between runs |
| 23 | 57230-630-2 | alternate M83248/1-010 qty 67,209 → 67,059 → 68,559 | Live stock |
| 28 | 5904761 | alternate 1744587-01 qty 978 (run 07) vs 970 (run 08) | Live stock |
| 33 | 600-052 | qty 5,690 (07 Opus) vs 5,688 (later runs) | Live stock |
| 30 | 5E2675-1 | 07 Opus NOT_FOUND, others found it | Grid not loaded yet ([§7.1](#71-false-not_found-when-the-results-grid-loads-slowly)) |
| 46 | 0684-12-28 | Opus NOT_FOUND, Sonnet DONE with an empty offer | Sonnet mistake ([§7.2](#72-sonnet-marks-an-unrecognised-part-as-done)) |

- **The quantity changes follow time, not the model or the mode.** The earliest run (07 Opus, 16:41)
  has the old figure and every later run has the new one, and runs 07 and 08 are each internally
  consistent. Prices were identical everywhere.
- **Excluding live stock changes, both models agree on 50 of 52 parts in both modes,** and bulk
  and one-by-one search found the same offers for every part they both found.
- **The value check flagged nothing.** It confirms each extracted value appears on the page,
  and it didn't flag or drop a value in any of the four runs.

---

## 7. Problems found

### 7.1 False NOT_FOUND when the results grid loads slowly

- **What happened:** run 07, Opus, row 30, `5E2675-1`. The results page opened in 7.3s and
  "expanded all details", but the grid had no records yet. The page text the model received
  (`vendor_pages/opus/20261014-165216-single-5E2675-1.txt`) contains only the page frame
  (menus, pager, "Results per page"), with no result lines. Opus correctly answered "no results".
- **Why it counted as NOT_FOUND:** the auth probe confirmed the session was still signed in, so
  the row was marked NOT_FOUND. The part is in stock ($14,338.26, qty 2): every other run found it.
- **Impact:** a buyer would be told a part isn't on Proponent when it is. 1 of 104 one-by-one
  searches in these runs.
- **Fix:** before reading, wait for the grid's record count, or for its "no records" message,
  not just the page load. Or search a one-by-one NOT_FOUND again once, the way bulk search
  already does. Bulk search wasn't affected, since it already retries its misses.

### 7.2 Sonnet marks an unrecognised part as DONE

- **What happened:** row 46, `0684-12-28`, in both runs. The portal shows one grid record:
  *"This part number is not recognized in Proponent system"* and *"This product is restricted for
  sale"*, with qty 0. Opus returns no offers, so the row is NOT_FOUND, which is correct. Sonnet
  returns one offer with the part number and no price, quantity, condition or lead time, so the
  row becomes DONE.
- **Impact:** a buyer sees DONE and an empty result line for a part that can't be bought.
- **Fix (code, model-independent):**
  - Drop offers that have no price, no quantity, no condition and no lead time.
  - Treat "not recognized" / "restricted for sale" records as not found.
  - This would also remove the empty "not supported" line on row 52 that all runs write.

### 7.3 Sonnet sometimes writes "USD" into the price

- **What happened:** 11 of Sonnet's offers in run 07 and 5 in run 08 have prices like `6.33 USD`.
  Opus always writes `6.33`.
- **Impact:** the Results tab has a separate Currency column, so the Price column becomes a mix of
  numbers and text. Sorting and formulas break on those cells.
- **Fix:** strip the currency from the price after extraction and put it in Currency. The value
  check already confirms the number is on the page.

### 7.4 Smaller observations

- **Wrong cost total in bulk reports:** in bulk runs, `scripts/run_report.py` reports a slightly
  lower cost than the real bill, and a "search time per part" that is really the batch time. Both
  are accounting issues in the report, not in the run.
- **Session left signed in:** an aborted attempt (18:39–18:43, before run 08 Opus) did not log out, so the next run
  found the saved Context still signed in and skipped login. The code handles this correctly, but
  an interrupted run leaves the shared Proponent session signed in until the portal expires it.

---

## 8. Login, session and reliability

| | 07 Opus | 07 Sonnet | 08 Opus | 08 Sonnet |
|---|---|---|---|---|
| Browserbase Context | missing → new one created | reused | reused, still signed in | reused |
| Login | automatic, 1st attempt OK | automatic, 1st attempt OK | skipped (already signed in) | automatic, 1st attempt OK |
| Cookie banner | declined automatically | — | — | — |
| CAPTCHA / bot check | none | none | none | none |
| 2FA | none | none | none | none |
| Session expired mid-run | no | no | no | no |
| Bulk search fell back to one by one | — | — | no | no |
| Logout | OK | OK | OK | OK |
| Google Sheet write errors | none | none | none | none |

- **Shared login:** all runs used the Skylink shared login. There were no personal logins and
  no Requested By values in this sheet.
- **Recording:** session recording was on for debugging. Turn it off for production runs,
  because the recordings show the login page.
- **Bulk retry:** in run 08 the one-by-one retry of bulk misses worked. Row 46 was the only miss,
  and it was retried and resolved in ~15s.

---

## 9. Recommendation

1. **Use bulk search for runs of 25 or more parts** (`--bulk 25`, 25 parts per search). It was 3x
   faster and half the LLM cost with the same offers. Its built-in retry of misses also covers the
   slow-grid case that produced the false NOT_FOUND in one-by-one mode. Smaller runs can stay one
   by one.
2. **Model:**
   - **Sonnet:** matches Opus on every value tested and costs 2.2x less. Use it once the
     empty-offer fix (§7.2) is in.
   - **Opus:** until then, Opus is the safer choice; it handled the unrecognised part correctly
     in both modes.
   - **Haiku:** a bulk run with Haiku on the same 52 parts would show whether the cheapest model
     (~$0.001 per part in run 06) is good enough, since bulk sends the model cleaner grid data.
3. **Fix before production:**
   - wait for the results grid (or retry one-by-one NOT_FOUNDs once) (§7.1)
   - drop empty offers / treat "not recognized" records as not found (§7.2)
   - normalise prices to numbers (§7.3)
4. **Speed-up worth doing:** write each batch's rows to the Google Sheet in one call instead of
   one row at a time. That would save ~35–40s per 25 parts, about a third of bulk run time.
5. **Before choosing the default model:** hand-check a sample of parts against the portal: for
   example 10 in-stock parts (price, qty, lead time) and 5 out-of-stock parts (lead time, due dates).
   Use the saved pages in `vendor_pages/` and the Browserbase recordings.

---

## 10. Limits of this test

- **One run per combination.** Timing can vary with portal load and time of day; these runs were
  in the evening (16:41–19:15 IST).
- **Live stock** changed between runs, so quantity differences can't all be put down to the model.
- **One vendor and one account** (Proponent, shared login), and 52 parts, mostly fasteners and
  connectors with 1–6 result lines each.
- **Not checked against the portal by hand.** Agreement between runs shows consistency, not
  correctness.
- **Cost estimates** for 100 or 1,000 parts assume linear scaling and the same mix of parts.

---

## 11. Per-part results (all 52 parts)

✓ = DONE, ✗ = NOT_FOUND, ⚠ = wrong status. **Offers** = result lines written, alternates included.
**Price**, **Qty** and **Lead time** are for the requested part number from the latest run
(08 Sonnet). For row 16 they are for the part number Proponent lists for it. "—" = not shown on the
portal (out of stock).

| Row | Part number | 07 Opus | 07 Sonnet | 08 Opus | 08 Sonnet | Offers | Price (USD) | Qty | Lead time | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | AS15001-1P | ✓ | ✓ | ✓ | ✓ | 1 | 6.33 | 150,132 | In stock (518 days if out of stock) | qty 151,632 in the first run, 150,132 later (live stock) |
| 3 | AS1634A20H0144 | ✓ | ✓ | ✓ | ✓ | 2 | — | Out of stock | 324 days (next due Mar 29, 2027, qty 44) |  |
| 4 | AS1895/1-200 | ✓ | ✓ | ✓ | ✓ | 2 | — | Out of stock | 294 days (next due Mar 15, 2027, qty 202; Mar 15, 2027, qty 250; Mar 15, 2027, qty 250) |  |
| 5 | AS1895/7-300 | ✓ | ✓ | ✓ | ✓ | 3 | — | Out of stock | 370 days (next due Nov 27, 2026, qty 713; Nov 27, 2026, qty 3500; Dec 30, 2026, qty 1500) |  |
| 6 | AS3069-09 | ✓ | ✓ | ✓ | ✓ | 1 | — | Out of stock | 154 days |  |
| 7 | AS3237-12 | ✓ | ✓ | ✓ | ✓ | 1 | 7.2 | 147,030 | In stock (514 days if out of stock) |  |
| 8 | AS3578-226 | ✓ | ✓ | ✓ | ✓ | 1 | 13.26 | 41,637 | In stock (72 days if out of stock) |  |
| 9 | AS3582-210 | ✓ | ✓ | ✓ | ✓ | 1 | 9.11 | 26,870 | In stock (85 days if out of stock) |  |
| 10 | AS43013-118 | ✓ | ✓ | ✓ | ✓ | 1 | 5.26 | 32,067 | In stock (112 days if out of stock) |  |
| 11 | AS5860P10-112 | ✓ | ✓ | ✓ | ✓ | 1 | 4.81 | 2,055 | In stock (126 days if out of stock) |  |
| 12 | ASNA2151-09 | ✓ | ✓ | ✓ | ✓ | 1 | 32.04 | 1,099 | In stock (112 days if out of stock) |  |
| 13 | ASNA2373JA10 | ✓ | ✓ | ✓ | ✓ | 2 | 12.4 | 900 | In stock (166 days if out of stock) |  |
| 14 | B14056-910 | ✓ | ✓ | ✓ | ✓ | 1 | 7.91 | 603 | In stock (88 days if out of stock) |  |
| 15 | B215-06 | ✓ | ✓ | ✓ | ✓ | 1 | 97.91 | 67 | In stock (104 days if out of stock) |  |
| 16 | B7210 | ✓ | ✓ | ✓ | ✓ | 1 | — | Out of stock | 109 days | Portal lists it as 624018-1 |
| 17 | BA-4120-50LD | ✓ | ✓ | ✓ | ✓ | 4 | 44.01 | 2,082 | In stock (140 days if out of stock) | 3 alternates listed (all runs) |
| 18 | BAC1521-715 | ✓ | ✓ | ✓ | ✓ | 1 | 3.96 | 1,346 | In stock (152 days if out of stock) |  |
| 19 | BAC1522-180-706 | ✓ | ✓ | ✓ | ✓ | 1 | 51.12 | 760 | In stock (112 days if out of stock) |  |
| 20 | BAC1530-10 | ✓ | ✓ | ✓ | ✓ | 1 | 22.25 | 2,440 | In stock (175 days if out of stock) |  |
| 21 | 561-515-0001 | ✓ | ✓ | ✓ | ✓ | 1 | 122.43 | 452 | In stock (75 days if out of stock) |  |
| 22 | 56B3A11 | ✓ | ✓ | ✓ | ✓ | 1 | 2711.59 | 10 | In stock (209 days if out of stock) |  |
| 23 | 57230-630-2 | ✓ | ✓ | ✓ | ✓ | 2 | 162.33 | 49 | In stock (137 days if out of stock) | alternate M83248/1-010 qty 67,209 → 67,059 → 68,559 (live stock) |
| 24 | 57739-1 | ✓ | ✓ | ✓ | ✓ | 1 | 447.13 | 23 | In stock (144 days if out of stock) |  |
| 25 | 583-522-9001 | ✓ | ✓ | ✓ | ✓ | 1 | 13.09 | 3,280 | In stock (186 days if out of stock) |  |
| 26 | 590-53AD33-104 | ✓ | ✓ | ✓ | ✓ | 1 | 170.28 | 65 | In stock (52 days if out of stock) |  |
| 27 | 59000S-19-2SN-H63 | ✓ | ✓ | ✓ | ✓ | 1 | 394.28 | 101 | In stock (239 days if out of stock) |  |
| 28 | 5904761 | ✓ | ✓ | ✓ | ✓ | 6 | 677.73 | 5 | In stock (63 days if out of stock) | alternate 1744587-01 qty 978 → 970 (live stock) |
| 29 | 5934001-217 | ✓ | ✓ | ✓ | ✓ | 1 | — | Out of stock | 129 days (next due Nov 20, 2026, qty 65; Dec 14, 2026, qty 50; Feb 5, 2027, qty 50) |  |
| 30 | 5E2675-1 | ✗ not found | ✓ | ✓ | ✓ | 1 | 14338.26 | 2 | In stock (283 days if out of stock) | 07 Opus: false NOT_FOUND (grid not loaded yet) |
| 31 | 5L0036400104000 | ✓ | ✓ | ✓ | ✓ | 1 | 52.16 | 96 | In stock (104 days if out of stock) |  |
| 32 | 600-0115-1/4 | ✓ | ✓ | ✓ | ✓ | 1 | 1.47 | 12,848 | In stock (210 days if out of stock) |  |
| 33 | 600-052 | ✓ | ✓ | ✓ | ✓ | 5 | 21.77 | 5,688 | In stock (19 days if out of stock) | qty 5,690 → 5,688 (live stock) |
| 34 | 6001-99-0538 | ✓ | ✓ | ✓ | ✓ | 1 | 522.48 | 22 | In stock (227 days if out of stock) |  |
| 35 | 601NB4E-1 | ✓ | ✓ | ✓ | ✓ | 2 | 385.64 | 41 | In stock (98 days if out of stock) |  |
| 36 | 606338-2 | ✓ | ✓ | ✓ | ✓ | 1 | 2429.08 | 876 | In stock (74 days if out of stock) |  |
| 37 | 60B40018-32 | ✓ | ✓ | ✓ | ✓ | 3 | — | Out of stock | 117 days (next due Feb 12, 2027, qty 10) |  |
| 38 | 612739 | ✓ | ✓ | ✓ | ✓ | 1 | 530.03 | 14 | In stock (74 days if out of stock) |  |
| 39 | 617954005 | ✓ | ✓ | ✓ | ✓ | 1 | 8.2 | 284 | In stock (154 days if out of stock) |  |
| 40 | 620-1-200 | ✓ | ✓ | ✓ | ✓ | 2 | 19.53 | 81 | In stock (53 days if out of stock) |  |
| 41 | 6205-89-63 | ✓ | ✓ | ✓ | ✓ | 1 | — | Out of stock | 187 days (next due Nov 29, 2026, qty 1; Dec 29, 2026, qty 2) |  |
| 42 | 622WR1-B-F | ✓ | ✓ | ✓ | ✓ | 1 | 2685.76 | 42 | In stock (146 days if out of stock) |  |
| 43 | 63600-505 | ✓ | ✓ | ✓ | ✓ | 1 | 112.25 | 35 | In stock (80 days if out of stock) |  |
| 44 | 651-535-9002 | ✓ | ✓ | ✓ | ✓ | 1 | 3297.66 | 6 | In stock (154 days if out of stock) |  |
| 45 | 66569-3F0 | ✓ | ✓ | ✓ | ✓ | 1 | 2061.16 | 62 | In stock (301 days if out of stock) |  |
| 46 | 0684-12-28 | ✗ not found | ⚠ empty offer | ✗ not found | ⚠ empty offer | 0 | — | — | — | Portal: "not recognized … restricted for sale". Opus NOT_FOUND (correct); Sonnet DONE with an empty offer (wrong) |
| 47 | 69-77023-1 | ✓ | ✓ | ✓ | ✓ | 1 | 67.95 | 270 | In stock (190 days if out of stock) |  |
| 48 | 69494J112 | ✓ | ✓ | ✓ | ✓ | 2 | 14.01 | 52 | In stock (26 days if out of stock) |  |
| 49 | 6TC6-10 | ✓ | ✓ | ✓ | ✓ | 2 | 184.38 | 18 | In stock |  |
| 50 | 702409-1078 | ✓ | ✓ | ✓ | ✓ | 2 | 937.75 | 4 | In stock (60 days if out of stock) |  |
| 51 | 7119SD | ✓ | ✓ | ✓ | ✓ | 1 | — | Out of stock | 155 days (next due Nov 12, 2026, qty 230) |  |
| 52 | 71550KE10T05SN-LC | ✓ | ✓ | ✓ | ✓ | 3 | 264.3 | 391 | In stock (282 days if out of stock) | Includes a "not supported" variant line with no values (all runs) |
| 53 | 723747 | ✓ | ✓ | ✓ | ✓ | 1 | 4135.65 | 1 | In stock (55 days if out of stock) |  |

---

## 12. Files and how to reproduce

| What | Where |
|---|---|
| Run 07 (one by one) | `test_runs/07_2026-10-14__52-parts__one-by-one__direct-url__medium-effort__opus-vs-sonnet/` |
| Run 08 (bulk) | `test_runs/08_2026-10-14__52-parts__bulk-search__25-per-search__medium-effort__opus-vs-sonnet/` |
| Full logs | `<model>-medium__<result>__<cost>usd__<seconds>s.log` in each folder; `Row N offer:` lines hold every extracted offer |
| Per-part reports | `<model>-medium__per-part-report.txt|csv` (`scripts/run_report.py`) |
| Pages the models read | `vendor_pages/opus/`, `vendor_pages/sonnet/` in each folder (git-ignored: they show the Skylink account and prices) |
| Session recordings | Browserbase dashboard; replay links are in each log (`Session replay: …`) |

The 08 Sonnet log was overwritten by an accidental re-run and restored from that run's terminal
output.

Commands used (from the repo root; the sheet's Status column cleared before each run):

```bash
# Run 07: one by one
LLM_MODEL=claude-opus-5-5   LLM_EFFORT=medium .venv/bin/python -m service.job_main --dump-pages
LLM_MODEL=claude-sonnet-5-5 LLM_EFFORT=medium .venv/bin/python -m service.job_main --dump-pages

# Run 08: bulk, 25 per search
LLM_MODEL=claude-opus-5-5   LLM_EFFORT=medium .venv/bin/python -m service.job_main --bulk 25 --set proponent:bulk_batch_size=25 --dump-pages
LLM_MODEL=claude-sonnet-5-5 LLM_EFFORT=medium .venv/bin/python -m service.job_main --bulk 25 --set proponent:bulk_batch_size=25 --dump-pages

# Per-part report after each run
.venv/bin/python scripts/run_report.py --csv <folder>/<model>-medium__per-part-report.csv
```
