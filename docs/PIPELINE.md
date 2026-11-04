# How the pipeline works

From one row in a sheet to offers in the `Results` tab. File references are relative to the
repo root.

```
Sheet row (Status empty/NEW)
  │  Cloud Scheduler sweep every 15 min
  ▼
POST /trigger  (service/api.py)        secret check → debounce/lock → start job
  ▼
Cloud Run Job sourcing-worker  (service/job_main.py --all-sheets)   one run id, every enabled sheet in parallel
  ▼
Pipeline.run  (sourcing/pipeline.py)
  read NEW rows → RUNNING → group by vendor + requester → resolve login → group by account
  ▼  (accounts run in parallel; one account = one Browserbase session)
lease account → open Browserbase Context → login if needed  (sourcing/auth/manager.py)
  ▼
search each part  (sourcing/vendors/proponent.py)
  direct results URL  →  read jqGrid records (or page text)  →  LLM extracts offers  (sourcing/extract.py)
  ▼
verify values against the page → stock rule → flags → write Results (sourcing/sinks/sheet.py)
  ▼
row Status DONE / NOT_FOUND / NOT_SOLD / FAILED / … → logout → release session + lease → run summary to Chat
```

Locally there is no trigger: you run `python -m service.job_main` by hand, and it processes only
`SHEET_ID` (a Google Sheet id or a local `.xlsx` path), in bulk (one part at a time with
`--one-by-one`). The Cloud Run job runs with `--all-sheets`: `SHEET_ID` plus every enabled sheet in the
registry. Commands and flags: [COMMANDS.md](../COMMANDS.md).

## 1. Trigger (`service/api.py`)

- `POST /trigger` needs header `X-Trigger-Secret`. The Scheduler sweep sends no body. There is no
  Apps Script in the sheet, so new rows wait for the next sweep.
- Sheets join through `scripts/manage_sheets.py add <id>` (store `sheets` collection), no
  redeploy. A caller that sends `{sheet_id, label}` to `/trigger` registers that sheet too.
- If a worker is already running (`WORKER_LOCK`) or a run was started seconds ago (`TRIGGER_LOCK`
  debounce), it returns `started: false`. Otherwise it starts the Cloud Run Job.
- `/hitl/view`, `/hitl/done` return 501 (plan step 5, not built).
- `/credentials` (login update page, plan step 6, built 2026-10-23): Google sign-in
  (`service/web_auth.py`), then vendor → login → new password (`service/credentials_ui.py`); see §4.

## 2. Worker job (`service/job_main.py`)

- Takes the worker lock (one run at a time). Sheets: `SHEET_ID` only, or with `--all-sheets` (Cloud
  Run) `SHEET_ID` registered plus every enabled sheet in the store.
- Search is bulk for runs of 2+ parts (`--bulk N` changes the threshold, `--one-by-one` turns it
  off); this overrides `bulk_min_parts` in the stored vendor config.
- Builds shared parts once (`build_shared_deps`): store, credential provider (Secret Manager),
  Browserbase client, LLM extractor, Chat notifier. `--dummy` / `DUMMY_MODE=1` swaps in the fake
  vendor (`sourcing/vendors/dummy.py`): no Browserbase, Secret Manager or LLM needed.
- Runs `Pipeline` for every sheet in parallel, each with its own sheet client and Results sink.

## 3. Reading rows (`sourcing/sheets.py`)

- Columns are found **by header name**, so column order doesn't matter.
  `Requests`: `RFQ ID | Vendor | Part Number | Qty Needed | Condition Wanted | Requested By | Status | Run ID | Notes`.
- Picks rows that have a part number and a Status that is empty or `NEW` (and `RUNNING` on the
  first read, to pick up rows from a crashed run). They are set to `RUNNING` at once.
- The run keeps reading while new rows keep arriving.
- Two backends behind the same interface: Google Sheets (gspread) and local `.xlsx` (openpyxl).

## 4. Logins (`sourcing/credentials/`)

**One Secret Manager secret per login** (decided 2026-10-21; one secret per vendor holding every
team member's login was considered and dropped: two team members saving at once would overwrite each other, every
read would load everyone's password, and the audit log couldn't say whose login changed).

| Login | Secret name | Example |
|---|---|---|
| Shared (whole team) | `sourcing-vendor-<vendor>` | `sourcing-vendor-proponent` |
| One team member's own | `sourcing-vendor-<vendor>__<email-with-dashes>` | `sourcing-vendor-wencor__victoria-skylink-com` |
| Infra keys (not logins) | `sourcing-<name>`, plain string | `sourcing-anthropic-api-key` |

- **Vendor name → secret name.** The sheet's `Vendor` cell is lowercased and every run of
  characters other than a-z/0-9 becomes one separator (`models.slugify`, then `base.secret_key`):
  `Jamaica Bearings` → `sourcing-vendor-jamaica-bearings`. Secret Manager allows only letters,
  digits, `-` and `_`, so this step can't be skipped. Aliases for one portal (`procart` →
  `proponent`) are in `ADAPTERS` (`sourcing/vendors/__init__.py`). A vendor with no adapter →
  row `FAILED` ("not supported yet") before any secret is read.
- **Requester → owner.** `Requested By` is trimmed and lowercased and must be the team member's email;
  in the secret name, `.` and `@` become `-`.
- For each (vendor, requester): the personal secret first, then the shared one. The worker reads
  version `latest`.
- Secret value, JSON: `{"username", "password", "totp_secret"?, "url"?}`. Planned: `schema: 1`,
  `updated_at`, `updated_by` (not secret, show the login's age). `url` is a fallback; the login page
  belongs in the vendor config (`vendors/<id>.login_url`).
- `VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test` points a vendor at another
  secret (all test runs so far used this).
- No secret → rows `NO_CREDENTIALS` + Chat message to the requester.
- Rows are then grouped by the **account** actually used, so two people sharing one login queue
  on it instead of racing.

**Who writes the secrets** (plan step 6, `docs/CREDENTIAL_UPDATE_UI_PLAN.md`):

- *Password change* (built 2026-10-23): the team member signs in with Google at `/credentials`,
  picks the vendor and a login they may change (own login; a shared one only as its editor;
  admins in `CREDENTIAL_ADMINS` all; `sourcing/credentials/access.py`). The runtime account adds a
  new version (`secretVersionAdder` on `sourcing-vendor-*` only), merged into the current JSON so
  `totp_secret` and `url` are kept (`CredentialProvider.update(must_exist=True)`: never creates a
  secret). Account `NEEDS_CREDENTIAL_UPDATE` → `NEEDS_LOGIN`; its `NO_CREDENTIALS` rows are set
  back to `NEW` by hand (the page and the Chat message say so). The wrong-password Chat message
  links to the page when `PUBLIC_BASE_URL` is set. Editors: `scripts/manage_logins.py`.
- *New login* (planned, not built): a Skylink admin uses the admin page: pick a vendor (only vendors with an adapter),
  shared or personal, and the team member. The secret is created **with the admin's own Google access**
  (OAuth), so our service account gets no create permission and Secret Manager's audit log names the
  admin (IAP alone gives no such token, so this needs a Google OAuth sign-in on the admin page; a
  name-prefix condition can't limit `secrets.create`, which is checked on the project). Labels:
  `app=sourcing`, `vendor=<id>`, `login-type=shared|personal`. The admin may leave the
  password empty: the team member then gets a Chat link to set it, and rows stay `NO_CREDENTIALS`
  until then. What reading a secret with no version returns is not documented; to test before
  building (`gsm.py` maps only NotFound / PermissionDenied to "no login"). An existing secret is
  never overwritten.
- Our account can't list secrets, so the `accounts/<vendor>__<owner>` record is the list of logins:
  owner email (exact), login type, secret name, state, version written / last version that logged
  in, created/changed by and when. Never a password. Every change also goes to `audit_log`.
- Later, if the client agrees: disable the previous version once the new one has logged in, destroy
  it after N days (needs `secretVersionManager` on the prefix). Never disable the latest version:
  `latest` doesn't skip it, so reads would fail.
- API check against Google's docs (2026-10-21): `docs/CREDENTIAL_UPDATE_UI_PLAN.md`, last section.

## 5. Session and login (`sourcing/auth/manager.py`, `sourcing/browser/browserbase.py`)

- Each account has a **lease** (store `accounts/<id>`). If another run holds it, rows go back to `NEW`.
- Each account has a saved **Browserbase Context** (cookies). It is reused; if it no longer exists
  (other Browserbase project), a new one is made.
- If the Context was signed in last time, an auth probe checks it is still signed in (step 0);
  otherwise it logs in (step 1): password, then TOTP if the secret has `totp_secret`.
- Outcomes that stop the account instead of retrying:
  - 2FA that TOTP can't answer → `NEEDS_ATTENTION` rows + Chat (step 5 will hand this to a human via Live View).
  - Wrong password → account `NEEDS_CREDENTIAL_UPDATE`, rows `NO_CREDENTIALS`.
  - Too many failed logins (`MAX_LOGIN_ATTEMPTS`) → `SUSPENDED`.
- A second login attempt only after a failure the portal didn't judge (timeout, server error page,
  nothing sent yet: `LoginFailed(retryable=True)`). Still on the login form with no message →
  `NEEDS_ATTENTION` at the first attempt (2026-10-21: a portal may lock after one failed login).
- Lockout / bot check wording on the login page: the login stops (`LoginBlocked`, account `SUSPENDED`).
- CAPTCHA wording: for now (2026-11-03) `CaptchaFailed`, tried again up to `MAX_LOGIN_ATTEMPTS`
  (`web.CAPTCHA_HINTS`); not counted as a failed login. On every attempt: rows `NEEDS_ATTENTION`,
  account stays `NEEDS_LOGIN`, so the next run tries again.

## 5b. Per-vendor limits (`sourcing/limits.py`, 2026-10-21)

Keep every vendor below its lockout and request limits. One dictionary per vendor:
`DEFAULT_LIMITS` ← the adapter's `default_limits` ← the vendor record's `limits` (store
`vendors/<id>`) ← `--set VENDOR:limits.KEY=VALUE` for one run. Edit with
`scripts/vendor_limits.py` (checks keys and values, writes the audit log).

| Key | Default | Used where |
|---|---|---|
| `max_login_attempts_per_run` | `MAX_LOGIN_ATTEMPTS` (2) | auth manager |
| `vendor_lockout_after`, `lockout_window_minutes`, `login_safety_margin` | 3, 1440, 1 | before each login: stop when the failed logins in the window reach `lockout_after − margin` (at least 1) → account `NEEDS_ATTENTION` |
| `login_cooldown_minutes` | 60 | no new run logs in this soon after a failed login (the one in-run retry is allowed) |
| `max_logins_per_day` | 10 | per account |
| `min_seconds_between_requests` | 5 | gap between searches of a vendor, all its accounts together |
| `max_searches_per_run`, `max_searches_per_day` | 50, 200 | `count_bulk_as` `search` (bulk = 1) or `parts` |
| `allowed_hours`, `timezone` | any, UTC | e.g. `18:00-08:00` |
| `backoff_minutes` | 120 | after a "too many requests" page (`RateLimited`) |

- **Counters** in the store: login attempts / failures on the account record (`logins_day`,
  `logins_today`, `login_failures`, `last_failure_at`; a success clears the failures; a failure
  before the credentials were sent is not counted), searches on `vendor_usage/<vendor>` (`day`,
  `searches_today`, `last_request_at`, `backoff_until`, `notified_<kind>`).
- **Order:** the vendor's day cap, allowed hours and back-off are checked before the account lease
  and the browser; the login checks before every attempt; the gap and caps before every search.
- **When a limit holds rows back** they go back to `NEW` with the reason in Notes and are picked up
  by a later run; the requesters get one Chat message per vendor, kind and day. A login held by the
  failure budget needs a person: `vendor_limits.py reset-login`. `resume` ends a back-off early.
- **New vendor** (no `limits` in its record): runs on the defaults; after the run the record gets
  an empty `limits` with `limits_confirmed: false` (so it keeps following the current defaults) and
  one Chat message asks for the vendor's real rules. `vendor_limits.py set` marks them confirmed.
  Dry runs (`--dummy`, `real_portal = False`) never write this.
- On Cloud Run the store must be Firestore: `make_store` refuses `file` / `memory` there (the
  counters would be wiped after every run).

## 6. Search (`sourcing/vendors/proponent.py`)

**The same part on several rows (2026-10-30).** Rows of one login asking the same part number
(case and outer spaces aside) are searched once: the first row is searched, the others (its
twins, `same_part_groups`) are recorded from its search right after it, so their RFQ and Results
lines sit together and in a bulk batch go out in the same write. Each twin still applies its own
UOM, quantity and condition (`twin_outcome` gives it its own copy of the offers); a status for the
whole search (login, limits, search error) reaches the twins through `_set`. Dry run on the saved
DASI page: `test_runs/12_…/run_18-42_dasi-bulk-10-parts/dry_run_2026-10-30_same-part-searched-once/`.

**One by one (default for Proponent).** For each part:

1. Open `https://procart.proponent.com/PartSearchResults.aspx?id=<part>` directly (learned
   `part_url_template`). If that fails once, the site's PART NUMBER search form is used for the rest
   of the run.
2. Check we are still signed in (login page or a Login button in the header → `SessionExpired`
   → re-login once and retry; twice → `NEEDS_ATTENTION`).
3. Set the results page size, expand all detail rows, then (`_expand_all_details`): open the
   "Show N Approved Substitutes" lists, open every row still closed ("+"; substitute rows come
   closed, and their Part Condition is only in those details, 2026-10-28), hide closest-match rows
   the portal won't sell, and read every warehouse of a row with a warehouse dropdown (each other
   warehouse picked in turn, its numbers added as a row below, the first one picked again).
4. Read the results **jqGrid records** (`sourcing/vendors/grid.py`): each result line as labelled
   fields. If the page has no grid, fall back to the flattened page text (Procart's results page
   has had no jqGrid since about 2026-10-23: rows, substitute rows and detail rows only). Cut at `MAX_PAGE_CHARS`.
   A grid with no rows is waited on for up to 20s. If it is still empty and the page says
   "No data found", the page is loaded once more; still empty → `empty_page`, no LLM call.
5. Parts whose search timed out, failed with an error, or gave an empty page go on the
   end-of-run retry list instead of getting their final status (see below).

**Bulk (the default since 2026-10-16).** `job_main` sets `bulk_min_parts` to 2 (or N from `--bulk N`;
`--one-by-one` sets it to null, which forces one by one): runs with at least that many parts use the BULK SEARCH box, `bulk_batch_size` (default 25) parts per search,
paging through results. Grid records longer than `MAX_PAGE_CHARS` go to the LLM in several calls,
split between records. A batch whose results still can't all be read (too long to split, past the
pages read) is tried again at half the size, and that size is kept for the run; below 2 parts the
run goes one by one. Any other batch failure is retried once, and two in a row switch the run to one by one. Parts
the bulk search missed are searched again one by one before logout (`retry_not_found`).

**End-of-run retry.** Before logout, every part on the retry list is searched once more, one by one
(at most `retry_not_found_max`): parts a bulk search missed, empty results pages, and single
searches that timed out or failed. A second empty page → `NOT_FOUND` with a note; a second error →
`FAILED` with the error. `retry_not_found: false` in the vendor config turns all of this off.

Timeouts: 300s per single search, 600s per bulk batch; 60s per page action (Procart is slow).

## 7. Extraction (`sourcing/extract.py`, `sourcing/llm/`)

- One system prompt (`EXTRACT_SYSTEM`) for single and bulk pages; output is forced to a strict JSON
  schema (`OFFERS_SCHEMA`): `found`, `page_kind`, and per result line `part_number, price, currency,
  qty_available, condition, lead_time, source_url, warehouses, listing`.
- Key prompt rules: copy only what the page shows, `"Not Present on website"` for anything missing;
  stock decides what else is read (no price/condition unless qty > 0).
- After the model answers, the code **checks every value against that part's own page data**
  (`_verify`): numbers not on the page are removed and flagged `PRICE_NOT_ON_PAGE`, `QTY_NOT_ON_PAGE`, …
  The stock rule is enforced again in code. Condition words are mapped to codes (`sourcing/conditions.py`,
  exactly the client's wordings; a condition matching none of them is left empty).
- `page_kind = login_page` → `SessionExpired`. An empty result only becomes `NOT_FOUND` after an
  auth probe passes (so a silent logout can't produce false "not found").
- **Not sold:** a part with no real offer (no price, stock, lead time or warehouses) whose own
  records carry the vendor's "not sold" wording (`not_sold_phrases`; Proponent: "not recognized in
  Proponent system", "not supported by Proponent", "restricted for sale") → `NOT_SOLD`, with the
  portal's own sentences. Checked in code, not by the LLM.
- Provider-agnostic: `LLM_PROVIDER=anthropic|openai|gemini` + `LLM_MODEL` + `LLM_EFFORT`. Only Anthropic
  has been run on real pages and only its price table is filled in.
- Vendor passwords never go to the LLM, only page text.

## 8. Writing results (`sourcing/pipeline.py` `_record`, `sourcing/sinks/sheet.py`)

- One `Results` row per offer: `RFQ ID, Vendor, Requested Part, Part Number, Unit Price, Price at Qty Needed,
  Currency, Qty Needed, Qty Available, MOQ, Warehouses, Condition, Lead Time, Source URL, Fetched At,
  Requested By, Flag, Internal Comments, Approved`. Written by header name; the header is created on
  first use, and a heading missing from an existing tab is added at its end.
- `Unit Price` (called `Price` before 2026-10-16; an old heading is renamed in place): the price of one piece, so a team member can compare it with `Price at Qty Needed`. Proponent:
  the 1-piece row of the part's price-break table when the page shows it (the line's own price can
  already be a break price, since the bulk search sends the quantity needed), else the line's price.
- `Price at Qty Needed`: the unit price for the quantity needed, read by the adapter from the part's
  own price-break table (Proponent: "Order Quantity / Price / 1-249 $23.70 …"). Qty 1 or no quantity
  pricing → the unit price; table not on the page → `Check page (quantity pricing)`.
- Empty cells say why: `Not shown by <vendor>` when the portal doesn't show the value, `Check page
  (see Flag)` when verification removed it (the Flag names the field). Out-of-stock lines keep the
  price and condition the page shows, for comparison (client, 2026-10-23); they're blank only when
  no stock is shown at all. Lead Time is blank in stock, else days only (other units as the page shows them).
- **Only the requested part and portal-named substitutes are written** (client, 2026-10-23:
  Requested and Quoted part numbers are never different, because of how Avsight's Part Research
  shows them). A look-alike number (ACHC3-13 for ACHC-3-13) or another part the portal lists is
  logged and left out; when nothing else is left the row is `NOT_FOUND`, Notes `Only other part
  numbers listed: …`. Proponent's "closest match" rows that can't be bought (`You entered 426EN10-8,
  closest match based on characters is 426EN9-8.`, no price, no add-to-cart) are removed from the
  grid records (`proponent._drop_suggestions`, 2026-10-27) and hidden on the page
  (`_HIDE_SUGGESTIONS_JS`, 2026-10-28) before the model reads them: one showed the asked number and
  was written as a second, priceless line, its stock in the real line's comment.
- `NOT_SOLD` parts get one row too, so a team member can check: Part Number, `Qty Available` = the
  portal's own words, `Flag` = `NOT_SOLD`, Source URL = the results page. The Requests row gets
  Status `NOT_SOLD` and the portal's words in Notes.
- A newer search of the same request (same RFQ ID, Vendor, Requested Part, Requested By) marks the
  older rows' Flag `SUPERSEDED by search of <time>; <old flag>`; nothing is deleted, approvals stay.
  Only rows from an earlier run are marked: the same part asked twice in one sheet keeps both sets
  current (2026-10-28; 426EN10-8 in rows 10 and 25 of the 2026-10-27 run had marked each other).
- Writes are tried 3 times. If all fail, the rows are kept in the store (`rows/<row_id>.unsaved_*`),
  the row is `FAILED`, and the next run writes them first (no new search) and sets the status they
  would have had. Not written if that Requests row now holds another part.
- Sheets quota (2026-10-27/28): Google allows 60 read requests a minute per user. A run reads each
  tab's header at most once a minute (`HEADER_SECONDS`), and the whole Results tab once, then again
  only when a request it writes may have older unmarked rows (`unmarked_requests`). After a 429 the
  sink waits `QUOTA_WAIT` (65s), up to `QUOTA_RETRIES` (3) times, without the "already saved?" read
  (a 429 writes nothing). RFQ tab lines still not written are kept as `unsaved_rfq`: the row stays
  `DONE` with Notes `RFQ tab not written (...); written on the next run`, and the next run writes
  only those lines.
- A bulk batch is saved in one go (`_process_bulk`, 2026-10-28): every part's lines and status are
  collected while the batch is recorded, then written in 3 requests: one Results append, one RFQ
  append, one status write (`SheetsClient.set_statuses`, each row its own status and note). A
  24-part batch went from about 73 writes to about 4. If the Results write fails after its retries,
  every row of the batch is `FAILED` and kept for the next run, as for a single row; a status write
  is retried (safe to send twice), after a 429 once `QUOTA_WAIT` has passed. One-by-one search
  still writes each part as it's found.
- `MOQ` (asked 2026-10-19): the line's minimum order quantity, only when the page states one
  (prompt 3.3; a money minimum such as "MIN ORDER $50" is not one), else blank. Proponent reads it
  from the part's own record ("Minimum order quantity for this part is 50 EA"). The column holds
  only a stated MOQ.
- **Below the vendor's minimum** (client, 2026-10-21; `pipeline.min_order`, `priced_qty`): the
  minimum is the stated MOQ, or, when none is stated, where the price-break table starts if above 1
  (`50-649 pcs: 0.27`). Qty Needed below it → the minimum is quoted: Flag `BELOW_MOQ`, Price at Qty
  Needed = the price at the minimum, RFQ Qty Quoted = the minimum (at most the stock; stock under
  the minimum → also `QTY_SHORT`). No total cost is written.
- `Flag` is for the team member: `QTY_SHORT`, `BELOW_MOQ`, `CONDITION_DIFFERS`, `NO_PRICE`, `NO_CONDITION` (no condition code on a line that needs one: in stock, the RFQ line becomes No Quote; out of stock with a price and a delivery, it is quoted with Condition Code empty for a person to fill in, 2026-10-27), `NO_DUE_DATE` (out of stock, stock due in but no date to read: all of it is quoted and Delivery is the lead time), `SUBSTITUTE`,
  plus any `*_NOT_ON_PAGE` from verification.
- **Substitutes** (`alternate_notes`, client 2026-10-23): a substitute B the portal lists for the
  requested A is its own line, B / B (Requested Part = Part Number = B), with `APPROVED SUBSTITUTE
  FOR PN A`. A's own line gets `APPROVED SUBSTITUTE ALSO OFFERED: PN B` when A is quoted (a
  price, and in stock or out of stock with a delivery; 2026-10-28) (`APPROVED SUBSTITUTE ALSO OFFERED WITH 146 DAY LEAD TIME: PN B` for a B without stock, the days from B's lead time, grouped by days, `WITH LEAD TIME` when none is shown, 2026-10-28; client
  2026-10-27, 426EN10-8 / 426EN9-8; one point each when some substitutes have stock and some
  don't), otherwise `QUOTING PN B`. When the portal shows no line for A, a pointer line is added
  (A / A, Unit Price `0.01`, `QUOTING PN B`); in the RFQ tab it is Qty Quoted `0`, Unit Price `0.01`,
  Condition and Type Quoted empty (the client named only qty, price and comment). Only lines the
  LLM marked `listing=substitute` count, and only when the page itself says so for that part
  (Proponent: `This is an approved substitute part for part #A` in B's row,
  `proponent._keep_listed_substitutes`, 2026-10-28); any other is a look-alike, left out.
- **Internal Comments** use the client's wording, in capitals (2026-10-23), each note a `• ` point
  and its rows (price breaks, incoming stock) `    ◦ ` sub-points below it, in this order:
  1. MOQ and stock location (`stock_note`, `moq_note`), the client's wording in one point:
     `• MOQ 22 FOR THIS PRICE BREAK, STK BREA, CA` when the quantity quoted falls in a price-break
     row starting above 1 (22 = the least that gets that row's price); `MOQ 50, PRICED AT 50 (ASKED
     10)` below the vendor's minimum (not "QUOTED": Qty Quoted is the stock, or out of stock what's due in / the quantity asked);
     `MOQ 50` for a stated MOQ that's met; every warehouse with stock after `STK`, joined with
     ` / ` (a warehouse showing 0 is left out; `OUT OF STOCK` when all show 0, 2026-10-23). Below it one sub-point per warehouse with its stock (`◦ BREA, CA:
     7915 ON HAND`), plus incoming stock only for a line not in stock (`, NEXT IN MAY 20, 2027:
     200`; deliveries on the same date are one entry, added up, 2026-10-26): no restock lead
     time for in-stock lines. Out of stock with nothing due in and a lead time, the client's own
     line instead (2026-10-27, 406Q80-1): `• 336 DAY LEAD TIME, FOB: NETHERLANDS` (every
     warehouse after FOB, joined with ` / `), the MOQ point below it. Order limits stay in the
     Warehouses column.
  2. Price breaks (`price_break_note`): `• PRICE BREAKS:` then `◦ 1-4 $15.64` … `◦ 173+ $1.06` (`$` for
     USD, else the currency code). The LLM returns the table as `price_breaks` (prompt 3.5); every
     number must be in the part's own data or the table is dropped (`PRICE_BREAKS_NOT_ON_PAGE`).
     Proponent replaces it with the rows parsed from the part's own grid record.
  3. Substitute note (above).
  MOQ and price breaks only for lines with a price. The RFQ tab's Internal Comments is the same text.
- **RFQ tab** (always since 2026-10-29; the `Results` tab only with `job_main --results`; before,
  `--rfq` turned the RFQ tab on, 2026-10-19): each offer is written to an `RFQ` tab
  (created when missing) in the client's `RFQ_Spreadsheet_Sample` layout (`ResultRow.as_rfq_record`,
  `RFQ_HEADER`, 24 columns). Quote-side columns follow the client's own quote-reading rules
  (`inputai-skill.md` in test_runs/11, 2026-10-19; `models.rfq_quote`, `rfq_delivery`): a line
  can be quoted when it has a price and a lead time (client, 2026-10-27); without a unit price, an
  in-stock condition code, or (out of stock) a delivery it is `No Quote` (an out-of-stock line with
  a price and a delivery is quoted even without a condition code, Condition Quoted empty:
  426EN9-8) (Condition Quoted = its condition code, `-` when unknown (DASI SV "Send RFQ" lots,
  2026-10-30), Qty Quoted
  `0` or the stock due in, Unit Price `0` (client wrote `0.00`; plain `0` since 2026-10-28), no Delivery); otherwise Type Quoted = `Outright` (the deal type; Proponent only sells outright),
  Quantity Requested = Qty Quoted = the site's whole stock (client, 2026-10-23, their AS3491-01
  line: 50 asked, 7915 / 7915 at $2.15); out of stock with stock due in: what comes in on the
  soonest date, deliveries that day added up across warehouses (`soonest_due_in`, client
  2026-10-27: 4140073-1, 75 on Dec 7 and 200 later → 75 / 75; 418-09104, 1 + 3 + 3 on one date →
  7); out of stock with nothing due in: the customer's quantity in both (406Q80-1: 1 / 1); no stock
  figure on the page: the quantity requested (the minimum when less is asked). Results keeps
  the team member's Qty Needed. Unit Price = the price-break price for the customer's quantity,
  or for the stock when the site has less (`price_qty`, `quote_price`; out of stock: at the
  quantity asked), or the line's grid price when it has no price-break table; the MOQ in
  Internal Comments is that row's start. Results keeps one piece's price in `Unit Price`. Delivery = `0` in stock;
  out of stock with stock due in, the days from the quote date (Fetched At) to the soonest date
  (client, 2026-10-27: 4140073-1 quoted Oct 26, due Dec 7 → 42; a date already past → the lead
  time); else the line's lead time as a number of days (client, 2026-10-23), weeks x 7, months x
  30, a range's larger end. Trace and Tagged By are read
  by the model from the line's certification notes (prompt 3.11 / 3.12, the client's rules; Proponent:
  `CertNotes`), at most 50 characters, and every word must be in the part's own page data
  (`extract._words_on_page`; the allowed abbreviations DR, F1, C of C pass on the words they stand
  for), else removed and flagged `TRACE_NOT_ON_PAGE` / `TAGGED_BY_NOT_ON_PAGE`. Type Requested is read from a `Type Requested` column in Requests.
  Still ours until the client answers (docs/RFQ_Sample_Column_Review.md): Vendor Reference = vendor
  name, Quoted Date = the date of Fetched At. Validity, Serial Number, Tag
  Date and External Comments are left blank. The request columns
  (Alternate Part Number, Keyword, Part Description, Serialized, UOM) are read from Requests when the
  tab has them; Part Description falls back to the vendor's description, read by the model
  (prompt 3.13, 2026-10-23; every word must be on the page, line breaks ignored, else
  `DESCRIPTION_NOT_ON_PAGE`), or Proponent's `Item_description1` when its grid records are there;
  UOM falls back to Proponent's `Um`. Requests also accepts the client's headings
  `Vendor Reference`, `Quantity Requested` and `Condition Code`. A failed RFQ write is logged and
  never fails the Results write. Alternates in the request are copied, not searched yet.
- Then the request row's Status is set, and the store gets a row record with seconds, LLM calls,
  tokens and cost (`scripts/run_report.py` reads these).

## 9. End of an account and of the run

- Learned selectors (search box, login page, results URL template) are saved to the store
  (`selectors/<vendor>`), so the next run doesn't need the LLM to find them.
- Logout from the portal (`logout_after_run`, default true), release the Browserbase session, release the lease.
- Also after a **failed login that may have gone through** (2026-10-28, DASI: signed in, but the
  check couldn't tell, so the session was left open): `may_be_signed_in` (vendors/base.py) says
  when; the login manager and the onboarding run then log out before closing the browser (audit
  `logout_after_failed_login`). Not after a CAPTCHA / bot check (touch nothing), a
  rejected password, or a failure before the password was sent. Many portals allow only a few
  sessions per login at once.
- Job record (`jobs/<run_id>`): counts per status, total tokens and cost. One Chat message with the summary.

## Row statuses

| Status | Meaning |
|---|---|
| empty / `NEW` | waiting to be picked up |
| `RUNNING` | picked up by the current run |
| `DONE` | offers written to Results |
| `NOT_FOUND` | portal shows no result (only after the signed-in check passed; empty pages are searched twice) |
| `NOT_SOLD` | portal says it doesn't sell the part (its words in Notes and on the Results row) |
| `NO_CREDENTIALS` | no login for this vendor/requester, or the password is wrong |
| `NEEDS_ATTENTION` | 2FA needs a person, or the session keeps expiring |
| `WAITING_AUTH` | reserved for the step 5 human handoff |
| `FAILED` | unsupported vendor, search error/timeout, or results could not be written (see Notes) |

## Store (`sourcing/store.py`)

Same interface in three backends: `firestore` (Cloud Run), `file` (`.local_store.json`, kept between
local runs so Context reuse works), `memory` (lost at exit).

| Collection | What |
|---|---|
| `locks` | worker lock, trigger debounce |
| `accounts` | per (vendor, user): Context id, state, lease, last login/logout, failures, secret name; planned: login type, secret versions, created/changed by (§4) |
| `vendors` | per-vendor config overrides: `login_url`, `probe_url`, `bulk_min_parts`, `bulk_batch_size`, `logout_after_run`, `default_condition`, `currency`, `allowed_hosts`, `limits` (§5b), … |
| `vendor_usage` | per vendor: searches today, last request, back-off, last limit message (§5b) |
| `sheets` | registered sheets, enabled flag, tab names |
| `selectors` | learned selectors per vendor |
| `jobs` | one per run: status, counts, cost |
| `rows` | one per sheet row: status, run id, seconds, tokens, cost, error |
| `audit_log` | login/logout/credential events |
