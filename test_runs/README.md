# Test runs: logs and reports

Every real run against the Proponent portal (procart.proponent.com) so far, one folder per test.
**The folder and file names carry the result**, so you can read the table below or just the
names without opening anything.

Name format:

- Folder: `NN_<date>__<parts>__<search mode>__<what was tested>__<model / effort>`
- Log: `<model>-<effort>__<outcome>__<LLM cost>usd__<whole-run seconds>s.log`
- Report: `<model>-<effort>__per-part-report.txt|csv` (written by `scripts/run_report.py`: per part status, offers, seconds, tokens, cost)
- Results sheet (run 01 only): `<model>-<effort>__results-sheet.xlsx` (the local `.xlsx` the run wrote into)

All runs used the Skylink **shared** Proponent login (secret `sourcing-vendor-test` via
`VENDOR_SECRET_OVERRIDES`), `STORE_BACKEND=file`, Browserbase with session recording on, and Anthropic models.
Runs 01 used a local `test.xlsx`; runs 02–06 used the Google test sheet `1V-S8J4b…aF7CVqak` through the Apps Script web app.

## Summary

| # | Date | Parts | Search mode | Model / effort | Result | LLM cost | Whole run | Per part |
|---|---|---|---|---|---|---|---|---|
| 01 | 10-12 | 106 | bulk box, 25 per search, page text | haiku / medium | 105 done, 1 not found | $0.0125 | 459s | 4.3s |
| 01 | 10-12 | 106 | same | sonnet / medium | 105 done, 1 not found | $0.1731 | 372s | 3.5s |
| 01 | 10-12 | 106 | same | opus / medium | 105 done, 1 not found | $0.3828 | 406s | 3.8s |
| 02 | 10-13 | 10 | bulk box, page text | opus / medium | 9 done, 1 not found | $0.1186 | 152s | 15.2s |
| 03 | 10-13 | 10 | bulk box, **results-grid records** | opus / medium | 10 done | $0.1471 | 162s | 16.2s |
| 04 | 10-13 | 10 | one by one (search form), old prompt | opus / high | 10 done | $0.4077 | 330s | 33.0s |
| 05 | 10-13 | 20 | one by one (search form) | haiku / high | 20 done | $0.0233 | 560s | 28.0s |
| 05 | 10-13 | 20 | same | sonnet / high | 20 done | $0.3404 | 526s | 26.3s |
| 05 | 10-13 | 20 | same | opus / high | 20 done | $0.7626 | 590s | 29.5s |
| 06 | 10-13 | 20 | one by one, **direct results URL** | opus / medium | 20 done | $0.7045 | 471s | 23.6s |
| 06 | 10-13 | 20 | same | sonnet / medium | 20 done | $0.3011 | 501s | 25.0s |
| 06 | 10-14 | 20 | same | haiku / medium | 19 done, 1 failed (sheet write) | $0.0191 | 487s | 24.3s |
| 07 | 10-14 | 52 | one by one, direct results URL | opus / medium | 50 done, 2 not found (1 false) | $1.5018 | 1158s | 22.3s |
| 07 | 10-14 | 52 | same | sonnet / medium | 52 done (1 wrongly) | $0.6785 | 973s | 18.7s |
| 08 | 10-14 | 52 | bulk box, 25 per search, results-grid records | opus / medium | 51 done, 1 not found | $0.7713 | 388s | 7.5s |
| 08 | 10-14 | 52 | same | sonnet / medium | 52 done (1 wrongly) | $0.3484 | 349s | 6.7s |
| 09 | 10-15 | 3 | one by one, direct URL, **local Chromium** | haiku / medium | 3 done | $0.0026 | 65s | 21.7s |
| 09 | 10-15 | 3 | same, **Browserbase** | haiku / medium | 3 done | $0.0025 | 105s | 35.0s |
| 11 | 10-19 | 21 | bulk box, results-grid records, **local Chromium**, local `.xlsx`; price breaks in Internal Comments | opus / medium | 20 done, 1 not sold | $0.3494 | 170s | 8.1s |

### Runs 14–18 (logs moved here from the project root, 2026-11-04)

These were `tee`'d into the project root as `run_<vendor>_<HHMM>.log`; the vendor in the old name
was often wrong (e.g. `run_aviall_1307.log` was a Wencor run), so they are filed by what the log
shows. File names inside: `<HHMM>__<parts>__<outcome>__<LLM cost>usd.log`. All Google test sheet `…aF7CVqak`.
Run 10's extra files (`run_bulk100*.log`, `report_bulk100_v2.csv`, the sheet after v2) went into `10_16_10_bulk_local/`.

| # | Date | Vendor | Browser | What the logs show |
|---|---|---|---|---|
| 14 | 10-26 | Proponent, 22 parts | local | 22 done, $0.3888 |
| 15 | 10-28 | DASI | Browserbase | login-only onboarding (generic selectors found); one run stopped before any row |
| 16 | 11-02 | Aviall, 5–20 parts | Browserbase | early runs: daily login cap (10), login timeouts, Browserbase 402 (free minutes used up); then 1623 → 1846 working: 20 parts, 15 done / 3 not found / 2 not sold, $0.7641 |
| 17 | 11-03 | Wencor, 10–11 parts | Browserbase + local | all done (10/10, 11/11), ~$0.53–0.86 per run; 1354 Browserbase 402; "Availability for All Warehouses" click timed out on 2 parts (1715, 1742) |
| 18 | 11-03 | AAR, 4 parts | Browserbase | no successful login: "reCAPTCHA verification failed" every time (1605–1646 stopped as SUSPENDED; 1801 retried 2x → NEEDS_LOGIN); 1905 with `browser_stealth` → 403 "Verified mode is only available on the Enterprise plan" |
| 19 | 11-04 | AAR, login only | Browserbase proxy / local | proxy: 402 "Proxies are not included in the free plan" (never reached AAR); local headless Chromium: avatar → "Sign In or Register" → form works, but the reCAPTCHA still fails after submit, so a different IP alone doesn't fix it |
| 19 | 11-04 | AAR, login only | local, shown window, `human_typing` | 1100: no CAPTCHA, home page after submit (signed in, team saw it), but not confirmed and **not logged out** (logout link only in the avatar menu); fixed with the account-page check + Magento logout address. 1102: stopped by the team before the login. 1157: Browserbase, no proxy, human typing: reCAPTCHA failed again (back on the login form, not signed in, so no logout needed). Same typing passes in a shown local browser on the office connection and fails on Browserbase. 1219: local **headless** + human typing, office connection: reCAPTCHA failed (not signed in). So the shown window matters, and the office connection alone isn't enough |
| 20 | 11-04 | Wencor, 3 parts (local `.xlsx`, `--results`) | local headless | **substitutes off checked**: N120-47934-071 / 21FA407G / AS3209-120 had 8 / 5 / 1 interchange lines on 11-03; now 3 done, 1 offer each (requested part only), no Interchanges pages opened, RFQ + Results tabs 3 rows; logout confirmed; $0.1767, 305s |
| 21 | 11-04 | AAR, 4 parts (local `.xlsx`, `--results`) | local, **shown window**, `human_typing` | **first AAR search run**: login without CAPTCHA (account check 200), bulk search 4 parts, 4 done, logout confirmed (account check redirects to login); 44066 out of stock (0, on order 97677) and C100-013080 "$RFQ" (938 in stock, no price) went out as Qty 0 / price 0; $0.1036 LLM |
| 22 | 11-05 | Incora (Wesco), 17 parts (Google test sheet) | Browserbase | **first Incora runs**: 1637 login from the adapter default (secret `url` `https://login.incora.com/` opened nothing, 31s), bulk search read the page's stored data: 17 parts, 8 with stock, 14 stock lines, every line's price breaks read (7.5s); the LLM key was invalid (401), so bulk was tried twice, then one by one, then again before logout (~40 live searches, none recorded); signed out ~3.5 min after the login (token). 1644: LLM key invalid again, at the login check -> 4 FAILED. 1646: the B2C address saved at 1639 was opened again (one sign-in each) -> "Still on the sign-in form", 17 NEEDS_ATTENTION. Fixed: Incora starts on the shop only, never saves a B2C address; token refresh waited for; a rejected LLM key stops the account's searches |
| 23 | 11-05 | Incora (Wesco), 17 parts (Google test sheet) | Browserbase | **first Incora offers**: 1716 login from the shop (token 29.9 min), 17 parts, 8 done (14 lines), 9 NOT_FOUND (Incora's × "Stock Not Found", same as each part searched alone at 1639-1642), $0.2506, 153s; page check: part row opened, $ pop-up 8 rows = data, live page showed 207,169 / 326,148 (team's screenshots at 18:14 showed 203,453 / 324,248 and MS24665-132 1,241 vs 1,191: stock moved in the hour, not a reading error). Trace emptied on 13 of 14 lines (the LLM's shortened wording failed the page check): now set by the program; × hover text read from its `mattooltip`; note when a larger break costs less (25+ at $1.0000 vs 10 at $25.0000) |

"Whole run" includes login, logout and sheet writes. LLM cost is the model bill only (Browserbase minutes are extra).

## What the runs show

Full analysis of runs 07/08 (52 parts, one by one vs bulk, Opus vs Sonnet): [ANALYSIS__runs-07-08__52-parts__one-by-one-vs-bulk__opus-vs-sonnet.md](ANALYSIS__runs-07-08__52-parts__one-by-one-vs-bulk__opus-vs-sonnet.md).

- **Login works** with the shared account every time (no 2FA, no CAPTCHA seen). A cookie banner appears on a fresh Browserbase Context and is declined automatically.
- **Bulk search is fast** (~4s per part at 25 per search) but reads one long flattened page where values can be matched to the wrong part. Run 03 reads the portal's jqGrid records instead (`sourcing/vendors/grid.py`); each part's fields then arrive together.
- **One-by-one search is the current default** (`default_bulk_min_parts = None` in `ProponentAdapter`): ~20s per part, simpler to check. Run 06 opens `PartSearchResults.aspx?id=<part>` directly instead of filling the search form, which saved ~3–5s per part compared with run 05.
- **Model cost per part (one by one, medium effort):** haiku ≈ $0.001, sonnet ≈ $0.015, opus ≈ $0.035. Search time is about the same for all three; the browser is the slow part.
- The only failure (run 06, haiku, row 4) was a dropped connection while writing to the Google Sheet, not a search problem.
- **Runs 07/08 (52 parts, opus vs sonnet, one by one vs bulk):** both models extracted the same offers on 50 of 52 rows.
  Sonnet marks a part the portal doesn't recognise (0684-12-28) as DONE with an empty offer; opus correctly says NOT_FOUND.
  One one-by-one opus search read the results grid before it loaded and gave a false NOT_FOUND (5E2675-1).
  Bulk search (grid records, 25 per search) was about 3x faster and half the LLM cost, with the same offers.
- **Run 11 (price-break tables in Internal Comments):** tables for 6 parts, the qty-needed row marked correctly
  (checked against the grid records). Found a bug, now fixed: ACHC3-13 (a separate record, $8004.22, no
  breaks) was matched to ACHC-3-13's record when punctuation was ignored, so it got the wrong Unit Price,
  Price at Qty Needed and table. The saved results sheet still shows those wrong ACHC3-13 values (written before the fix).
- **Not checked yet:** whether the extracted price/qty/condition values match the portal for each model (runs 07/08 only compare the models with each other). The offers for each row are in the logs (`Row N offer: pn=… price=… qty=… cond=… lead=…`), so models can be compared line by line.

## Other things in this folder

- `state_snapshot__2026-10-13_1732__before-20-part-runs/`: taken before runs 05/06.
  `google-sheet-export__…xlsx` is the Google test sheet at that time (the 20 parts used in runs 05/06 are its `Requests` tab).
  `local_store__runs-01-to-04.json` is the local store with the per-row records of runs 01–04 (git-ignored).
- `vendor_pages/` inside runs 02–04: the raw results pages / grid records the model was given (`DEBUG_DUMP_PAGES=1`).
  Files starting with `earlier-attempt-no-log__` come from a try just before the logged run. **Git-ignored**: they show the
  signed-in Skylink account and Proponent prices, so they only exist on the machine that ran them.

## Known gaps in these files

- Run 06 sonnet: the original report file was a copy of the opus report, and the row records had already been overwritten,
  so `sonnet-medium__per-part-report__rebuilt-from-log.*` was rebuilt from the log (no per-part token counts).
- Runs 02, 03 and 05 have no per-part report; their logs have the same numbers per row.
- Runs 02–06 wrote their results to the Google test sheet, not to a local file.

## Adding a new run

```bash
.venv/bin/python -m service.job_main 2>&1 | tee test_runs/new.log
.venv/bin/python scripts/run_report.py --csv test_runs/new_report.csv > test_runs/new_report.txt
```

Then move them into a new `NN_…` folder named in the format above, add a short `README.md`, and add a row to the table.
Report a run before re-running the same sheet rows (the row records get overwritten).
