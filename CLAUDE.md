# SourcingAI prototype: context for AI agents

Read this first. Then `docs/PIPELINE.md` for how the code works, `test_runs/README.md` for what
real runs showed, and `docs/SourcingAI_CloudRun_Prototype_Plan.md` for the plan being followed.
Before a new vendor adapter or prompt: `docs/NEW_VENDOR_CHECKLIST.md` (lessons from the working
adapters; start from the closest one, prefer direct addresses over clicks).

## What this is

SourcingAI looks up aviation spare-part prices for the **Skylink** (client) team. A team member adds a row
(part number, qty, condition, vendor) to a Google Sheet. A worker logs in to the vendor's portal
with Skylink's account, searches the part, has an LLM read the results page into a fixed schema,
checks each value against the page, and writes the offers to the sheet's `RFQ` tab (client's RFQ
layout; the `Results` tab too only with `--results`, 2026-10-29).

- Vendor supported so far: **Proponent** (`https://procart.proponent.com`, ASP.NET site with jqGrid results).
  Other portals: the generic adapter (ported from the main copy 2026-10-22), set up from the
  vendor's login secret or a vendor record; checked with `scripts/onboard_vendor.py`.
- Runs on **Google Cloud Run** (API service + worker job), browser in **Browserbase**, state in
  **Firestore**, vendor logins in **GCP Secret Manager**, LLM = **Anthropic Claude** (OpenAI/Gemini pluggable).
- Prototype stage: everything has been run locally against the real portal; **nothing is deployed yet**.
- History: an earlier MCP/Stagehand prototype (separate repo, `stagehand-browser-mcp/src`) proved the
  idea; this is the rewrite. `docs/old_mcp_prototype_proponent_results.json` is that prototype's output.
  The client has said SourcingAI should eventually live in their `mie-agent` repo.

## Client rules (do not break these)

1. Vendor logins live in **GCP Secret Manager**, project **`skylink-material-planning`**. Not
   Bitwarden (replaced on 2026-10-09), not `.env`, not code.
2. **No new GCP project**, no billing on our own `source-proto` project.
3. Secret names: `sourcing-vendor-<vendor>` (shared login), `sourcing-vendor-<vendor>__<email-with-dashes>`
   (personal), infra keys `sourcing-*`. Our account can read/add versions but **cannot list** secrets;
   new vendor secrets are created by the client.
4. Live portal: few searches, outside busy hours (team members use the same login daily).
5. Skylink staff won't use the GCP console, hence the planned password-update form (step 6).

## Status against the plan

| Step | State |
|---|---|
| 0 GCP setup | `infra/setup_gcp.sh` + `scripts/check_access.py` ready, not run in the client project |
| 1 Skeleton on Cloud Run | Done in code (Dockerfile, cloudbuild.yaml, `--dummy`); not deployed |
| 2 Proponent search | **Done and tested on the real portal** (see `test_runs/`): login, direct-URL search, jqGrid reading, extraction, verification, Results writing, per-row cost |
| 3 Automatic trigger | `/trigger` + Cloud Scheduler sweep; not deployed. **Apps Script back (2026-11-06)**: `apps_script/Code.gs` calls `/trigger` after 90s without edits, fills Requested By; rows added during a run → the worker reads the sheet again (`RERUN_FLAG`, at most 5 passes); `TRIGGER_RUN_LOCAL=1` runs the worker on this machine for testing through ngrok. Tested with mocks and a local dummy run, not yet from a real sheet. **Sheets add-on (2026-11-12)**: `apps_script/addon/` (Editor add-on, admin install; each person: Extensions > SourcingAI > Turn on for this sheet), calls `/trigger` on every edit with the user's Google token (checked: `ADDON_OAUTH_CLIENT_IDS` + `ALLOWED_EMAIL_DOMAINS`, no secret in the add-on), the 90s quiet wait moved to the worker (`wait_until_quiet`, `TRIGGER_QUIET_SECONDS`; add-on timers may run only hourly); listing in its own small GCP project (client's, no secrets/billing; Google won't share `skylink-material-planning`'s MIE Assistant listing project). Per-sheet script kept in `apps_script/per_sheet/`. Tested with mocks only |
| 4 Session robustness | Partly: expiry detection, re-login once, NOT_FOUND only after auth probe, leases, login cap. Missing: morning warm-up, session-lifetime measurement |
| 5 Human 2FA handoff | TOTP typed automatically. **Chat handoff built (2026-10-22, generic adapter, `auth/hitl.py`)**: Live View link in Chat, the person logs in, run carries on (Browserbase, or local DevTools with `--live-view`); fallback code form `/hitl/code`; Chat-app card (`/chat/events`) waits for the client's Chat app. Tested on Notion 2026-10-22: Browserbase Live View OK, local live view OK, code form OK; Notion's logout (in a menu) not found. `/hitl/view` (sign-in check in front of the link) not built. **Logout handoff (2026-11-02)**: logout not confirmed after 2 tries → requesters get the Live View link to log out by hand (an open session blocks the team's sign-in elsewhere); checked in a second tab; tested with mocks only |
| 6 Credential update form | **Built 2026-10-23, tested with fakes only**: `/credentials` behind Google OAuth sign-in → pick vendor → pick a login you may change (own / shared as editor / admin) → new Secret Manager version; Chat link after a wrong password; `scripts/manage_logins.py`. Needs the client's OAuth client + admin list (`docs/CREDENTIAL_UPDATE_UI_PLAN.md`). 2026-10-28: admins add a login (`/credentials/new`, secret created with the admin's own Google access) and choose a shared login's editors (`/credentials/editors`); creating tested locally on the real Secret Manager 2026-10-28 (as the developer's gcloud login), Google consent step not yet |
| 7 Passkey spike | Not started |

Latest findings (2026-10-14): one-by-one search via direct URL, ~20s per part; LLM cost per part
haiku ≈ $0.001, sonnet ≈ $0.015, opus ≈ $0.035. Runs 07/08 (52 parts, opus vs sonnet): both give
the same offers on 50/52 rows; sonnet marks an unrecognised part DONE with an empty offer (opus:
NOT_FOUND); bulk search (25 per search) is ~3x faster and half the LLM cost with the same offers.
Values have only been compared between models, **not** against the portal yet.
2026-10-15: local Chromium (`--browser local`) vs Browserbase, same 3 parts: identical offers, no
bot check; local ~10s per part vs ~18s, whole run 65s vs 105s. LLM_PROVIDER/MODEL/EFFORT have no
defaults any more (run stops if unset).

## Layout

```
sourcing/            the library
  pipeline.py          rows → accounts → login → search → record (start here)
  vendors/proponent.py Proponent adapter (login, search, bulk, results grid, logout); biggest file
  vendors/grid.py      read jqGrid records instead of page text
  vendors/base.py      VendorAdapter base + vendor config keys
  vendors/web.py       shared browser helpers + generic selector/wording lists (any vendor)
  vendors/registry.py  vendor records in the store: ids, aliases, enabled (manage_vendors.py)
  vendors/generic.py   GenericAdapter (`adapter: generic`): login (TOTP too) + search any portal from its record; LLM only as fallback
  vendors/dasi.py      DASI adapter (generic login; exact / multiple search addresses, DASI Only + Miami HQ only, grid read as lot records, no View clicks) + dasi_prompt.py (its own extraction prompt)
  vendors/aviall.py    Aviall adapter (shop.boeing.com: login URL -> "Log in to Boeing Distribution" -> "Log in with Boeing OneLogin ID" -> username -> password; on the 2FA box it opens the login URL again once (team, 2026-11-02), then asks a person; boeingsearch?query= / "Multi-line Ordering" dialog, "part, qty" lines; hover text, icon labels and Price Breaks pop-up written into each card before reading) + aviall_prompt.py (from the team's screenshots); 2026-10-30, not yet tried on the live portal
  vendors/aar.py       AAR adapter (shop.aarcorp.com, Magento: sign in from the landing page via the avatar -> "Sign In or Register" (team, 2026-11-03), then the generic login; direct login page refused twice with "reCAPTCHA verification failed." 2026-11-03 (a CAPTCHA is retried for now, see Known gaps; Browserbase proxy/stealth off: not in our plan (402/403), 2026-11-04; email + password typed key by key (`human_typing`), headed local login went through without CAPTCHA 2026-11-04; signed in / logout checked through the account page (`/customer/account/` 200 vs redirect) and Magento's `/customer/account/logout/`, `default_browser_stealth`); catalogsearch/result/?q=<part>, several parts joined by %2C+ as its bulk; product cards of every results page read, exact part first) + aar_prompt.py (price or RFQ only); 2026-11-03, from the team's description, not yet tried on the live portal, no secret yet
  vendors/monroe.py    Monroe Aerospace adapter (monroeaerospace.com, BigCommerce: login page = the secret's `url` only (its Sign In is a pop-up in an iframe: the generic login looks in frames since 2026-11-04), search.php?search_query_adv=<part> / several joined by %2C+; the search matches words (thousands of results), so only cards whose part number is a requested one are kept, then each product page is opened by its address in its own tab (3 at once): the script-drawn variants table (stock, per-manufacturer stock), the Bulk Discounts pop-up (price breaks), minimum order; fetched HTML as fallback; signed in / logout through /account.php and /login.php?action=logout) + monroe_prompt.py (from the public pages, 2026-11-04); not yet tried signed in on the live portal, no secret yet (added later through `/credentials/new`: type "Monroe Aerospace" -> `sourcing-vendor-monroe`)
  vendors/aircraft_spruce.py  Aircraft Spruce adapter (aircraftspruce.com, 2026-11-05: login page = the secret's `url`, signed in = the navbar's logout link; product page by catalog/pnpages/<part>.php (Page Not Found = HTTP 200), else the search box (a POST, no results address) with the part number (cards whose Part# or MFR Model# is it), else with the Part Description / Keyword (lines `other` + FOUND_BY_DESCRIPTION); the requested quantity is typed into the Quantity box: no stock number on the site, its stock line is per quantity, so In Stock -> qty = the requested qty (noted), Partial Stock -> the qty that ships now; one part per search; login fields pinned (User ID = #user_email, the generic list had matched the footer newsletter box), typed key by key; Google's reCAPTCHA image challenge after Sign In -> MfaRequired("captcha"): a person through Live View, no retry) + aircraft_spruce_prompt.py; not yet signed in on the live portal, no secret yet (`sourcing-vendor-aircraft-spruce`)
  vendors/incora.py    Incora adapter (shop.incora.com, Angular app, 2026-11-05: login URL = secret's `url` (team: https://shop.incora.com/) -> Azure AD B2C login.incora.com (Email Address, Password, SIGN IN) -> /home; signed in = live OAuth `access_token` in localStorage; search = Bulk Search: parts written to localStorage `ecom.products.bulk.search.search.code`, /shop/products/bulk-search opened, its stored result `ecom.products.bulk.search` read (every part and stock line, all table pages; paginator count checked); price breaks per stock line from the $ pop-up's own API address, fetched inside the page; values set from the data (`_finish`), Site = warehouse, Manufacturer / Revision / Shelf Life to Internal Comments; logout = the app's logOut (tokens removed, B2C end-session -> /logged-out); login always starts on shop.incora.com (a B2C address is one sign-in each: never saved / reused, run 22); a token near its end is left to the app to refresh before the next page; once per search the page itself is checked as a person would: the first part with stock opened by its row, its first `$` pop-up read, a × hovered ("Stock Not Found"), compared with the data (`PAGE_CHECK_MISMATCH`; with --dump-pages the page HTML is saved); without stored data every table page is clicked through: each ✓ part opened, every `$` read) ; RFQ Delivery = 7 for every quoted in-stock line (client, 2026-11-06: "they typically take 7 days to ship"; `default_delivery_days`, vendor config `delivery_days`; No Quote lines none, out of stock unchanged) + incora_prompt.py; first live runs 2026-11-05 (test_runs/22): search and price breaks worked (17 parts, 14 lines), no offers written yet (LLM key was invalid); secret `sourcing-vendor-incora`
  vendors/textron.py   Textron Aviation adapter (ww2.txtav.com, ASP.NET MVC, 2026-11-12: login page = the secret's `url` (`https://ww2.txtav.com/Account/Login`); #Email / #password-field typed key by key, then wait for Log In (#btnSignIn) to be enabled by Google's invisible reCAPTCHA v3; the "I'm not a robot" box instead -> MfaRequired("captcha"), a person through Live View; the 2FA code page after Log In -> Live View handoff, the person types the code, the run carries on once /Dashboard opens (signed in = /Dashboard 200, signed out = redirect to /Account/Login; the page's hidden #logoutForm is there signed out too); one part = Parts/PartSearch/PartsDetail/<part>; bulk = the Multiple Part Search form (<=20, part + qty per row when the row has a qty box; signed out it has part boxes only), parts it misses opened by their PartsDetail address in tabs (3 at once), `bulk_mode: "tabs"` = tabs only; per part block the page's own hidden fields (YourPriceUS/EUR, Uom, MinOrder, iMROCondition, PotentialLeadTime, ErrorMsg "Part Not Found.", Substitutes) are read by the program and set on the line; alternates to Internal Comments (substitutes off); logout = the header's Log Off / the page's logout form, confirmed by /Dashboard; the site answered headless local Chromium with its 404 "Page Could Not Be Found" page on every address 2026-11-12 -> LoginBlocked, never NOT_FOUND; headed local Chromium (`--browser local --headed`) got through the same day: signed-out read-only check of single page, Multiple form (3 parts), tabs, Part Not Found, Log In button enabled, /Dashboard check all OK; Browserbase not tried: free plan minutes used up (402); a page of misses only is NOT_FOUND only after the /Dashboard check) + textron_prompt.py; first signed-in run 2026-11-12 (test_runs/25, onboarding, 3 parts OK, 2FA by a person in the shown window); 19-part Multiple Part Search signed in OK (test_runs/26); since then the program chooses Email and clicks Send Code on the 2FA page (the person types only the code; LLM page action if the coded clicks miss), sets warehouse / lead time / no-price (0, "contact us for pricing") / notes itself, "NO LONGER AVAILABLE" -> NOT_SOLD with the options listed; no quantity box recognised on the signed-in form yet (row inputs logged); 2026-11-13 (test_runs/27): Email + Send Code by the program worked on the real page (select#SelectedProvider, button#btnSendCode, input#Code); the Live View wait now ends when the sign-in ends (back on the login form after the code step, or on Send Code over 3 min) and the run closes the session; secret `sourcing-vendor-textron-aviation`
  vendors/assist.py    PageAssist: a page action the coded way couldn't do (pick a dropdown entry, open a pop-up) is done by the LLM, checked by the adapter, saved as a recipe; still not done -> the line's Flag says so (2026-11-04, team: complete results or a visible gap)
  onboarding.py        onboarding run for a vendor (scripts/onboard_vendor.py): login, 1-3 parts, report
  extract.py           LLM prompt, JSON schema, value verification, stock rule
  llm/                 provider clients (anthropic, openai, gemini) + price tables
  auth/manager.py      Context reuse, login ladder, account states
  browser/browserbase.py, local.py   BROWSER_BACKEND=browserbase | local (Chromium in the container)
  credentials/         Secret Manager provider; access.py = who may change which login
  sheets.py            Google Sheets / local .xlsx backends
  store.py             Firestore / file / memory store
  sinks/, notify/      Results writer, Google Chat
service/api.py       FastAPI: /trigger (+ run-again flag, TRIGGER_RUN_LOCAL), 2FA code form (+ 501 stubs)
apps_script/         addon/ (Sheets add-on) and per_sheet/ (script in one sheet): row added -> /trigger; README: setup, checks, ngrok
service/web_auth.py, credentials_ui.py   Google sign-in + vendor login update page (/credentials)
service/job_main.py  worker entrypoint
infra/, cloudbuild.yaml, Dockerfile   deploy
scripts/             check_access, manage_sheets, manage_vendors, manage_logins, onboard_vendor, vendor_limits, run_report
tests/               133 tests with fakes and mock pages (no network)
samples/             clean 20-part request sheet for local runs
test_runs/           logs + reports of every real run, named by outcome
docs/                plan docs + PIPELINE.md
```

## Working on it

Full command and flag reference: `COMMANDS.md`.

```bash
.venv/bin/python -m pytest -q                 # must stay green
.venv/bin/python -m service.job_main          # one run: SHEET_ID from .env, bulk search
.venv/bin/python -m service.job_main --help   # --sheet, --bulk N, --one-by-one, --dummy, --record, --dump-pages, --set
.venv/bin/python scripts/run_report.py        # per-part time/tokens/cost of the latest run
```

- Local runs: `STORE_BACKEND=file`, `SHEET_ID=<file>.xlsx` or a Google Sheet id (gcloud login
  with the Sheets scope). `--dummy` needs no keys. LLM model/effort only from `.env`. Apps Script in the sheet (removed
  2026-10-14, back 2026-11-06: `apps_script/README.md`, local test through ngrok) calls `/trigger`; the Scheduler sweep
  stays as backstop; sheets join via `manage_sheets.py add` or the script's first call.
- A local run searches only `SHEET_ID`; the registry (`manage_sheets.py`) is used only with
  `--all-sheets`, which the Cloud Run job passes. Bulk search by default, `--one-by-one` for per part.
- Real runs so far used `VENDOR_SECRET_OVERRIDES=sourcing-vendor-proponent=sourcing-vendor-test`.
- After a real run, save the log and report into a new `test_runs/NN_…` folder (naming rules in
  `test_runs/README.md`) and add a row to its table.
- Data entry rules (client's checklist, 2026-10-28): `docs/DATA_ENTRY_RULES.md`, checked by
  `tests/test_data_entry_rules.py` (one `test_rule_<n>_...` per rule). A change that breaks one
  is fixed in the code; open gaps are marked `GAP` there.
- Conventions: plain, short comments that say *why*; dated observations from the portal in comments
  ("seen in logs 2026-10-13"); vendor-specific behaviour goes in the adapter or vendor config, never in
  the pipeline; any value the LLM returns must be checkable against the page.
- Never commit `.env`, `infra/secrets.env`, `.local_store.json`, `debug_pages/`, `test_runs/**/vendor_pages/`.

## Known gaps / gotchas

- **CAPTCHA rule paused for development (client, 2026-11-05, a few days only).**
  `DEV_ALLOW_CAPTCHA_SOLVING=1` (`config.captcha_solving_allowed`, default off) turns on
  Browserbase's `solve_captchas` and makes Aircraft Spruce wait up to 45s for the image
  challenge to clear before asking a person. Our Browserbase account is the free plan, which
  has no solver (Developer, $20/mo, has one); Steel.dev's free tier needs a $10 paid deposit
  first. Code marked `REMOVE-AFTER-CAPTCHA-TRIAL` goes when the trial ends; never set it on Cloud Run.

- CAPTCHA wording on a login page (AAR: "reCAPTCHA verification failed.") is, **for now
  (2026-11-03), retried**, not a stop: `CaptchaFailed` (vendors/base.py), tried again up to
  `MAX_LOGIN_ATTEMPTS`, not counted towards the vendor's lockout (no cooldown either). CAPTCHA on
  every attempt: rows `NEEDS_ATTENTION`, the account stays `NEEDS_LOGIN` (not `SUSPENDED`), so
  the next run logs in again. Lockouts and bot checks ("verify you are human", "unusual
  activity") still stop. To stop on CAPTCHA again: put "captcha" back in `BLOCKED_HINTS`
  (`vendors/web.py`, `vendors/proponent.py`). Browserbase still never solves CAPTCHAs.
- Vendor setting `show_substitutes` (adapter default `default_show_substitutes`, true): **off for
  Wencor and Aviall** (team, 2026-11-03). Wencor's Interchanges pages and Aviall's View
  Replacement pages aren't opened, and the pipeline drops any `substitute` line for them. The
  code for both stays; `show_substitutes: true` in the vendor config turns it back on. **Their
  alternates still go into Internal Comments** (team, 2026-11-04): one line "• ALTERNATE PN
  LISTED: PN A, PN B" on the requested part's row, read from the page (Wencor "Interchanges:
  ...", Aviall "Superseded by part number ...") with no page opened and no entry of their own
  (`SearchOutcome.alternates`, `listed_alternates`, `pipeline.add_listed_alternates`).
- Vendor setting `browser_stealth` (adapter default `default_browser_stealth`): Browserbase
  sessions through a Browserbase proxy. Off everywhere since 2026-11-04 (was on for AAR
  after the 2026-11-03 reCAPTCHA failures); ignored with `--browser local`. Advanced stealth was dropped
  2026-11-04: our plan refuses it (403 "Verified mode is only available on the Enterprise
  plan"). Proxies cost bandwidth and may need a paid plan too.
- Onboarding (`scripts/onboard_vendor.py`) never closes a browser whose logout isn't
  confirmed (team rule, 2026-11-04): it waits, no time limit, for a person to log out in the
  shown window (`--headed`) or live view (`--live-view` / Browserbase), else retries the
  logout. The worker's logout still ends with the Live View handoff's 10-minute wait.
- A rejected LLM key (401/403 from the provider) stops that account's searches at once (`pipeline.LlmKeyRejected`, 2026-11-05): rows FAILED "LLM API key rejected". Before, it counted as a failed search: bulk again, then one by one, then again before logout (~40 live Incora searches in run 22).
- Row ids are sheet row numbers; deleting rows mid-sheet shifts them (accepted for the prototype).
- `/trigger` is public, protected only by `X-Trigger-Secret`.
- The login update page's register is the account records: a login shows up there after the worker
  used it once, after it was added on `/credentials/new`, found on `/credentials/find` (secret made
  with gcloud; looked up by name, we can't list), or `manage_logins.py add`. `NO_CREDENTIALS` rows go back to `NEW` by hand.
- OpenAI/Gemini clients are untested on real pages and their price tables are empty.
- Page-change detector (`sourcing/health.py`, 2026-10-30): each run records per-vendor health
  rates (grid-fallback, unrecognised page, Check-page, not-found, needs-attention) to the store
  and warns in the Chat summary when a run deviates far from that vendor's recent runs — a cue
  to re-run `onboard_vendor.py <vendor> --fresh` and redesign the adapter. It only warns; it
  never changes extraction/login or disables a vendor. Needs ≥3 prior runs and ≥3 parts before
  it fires. Inspect history with `scripts/check_page_health.py`.
- Bulk search is the default (2026-10-16, client's choice): `job_main` sets `bulk_min_parts=2`
  (`--bulk N` changes it, `--one-by-one` sets None), so a stored vendor config value has no effect.
  Code marked `REMOVE-IF-PER-PART-DROPPED` goes if the senior asks to drop per-part search as an option.
- Google Sheet writes can drop a connection (`RemoteDisconnected`); `sinks/sheet.py` tries 3 times
  (checking the rows weren't saved already). If all fail the row is `FAILED` and its Results rows
  are kept in the store; the next run writes them before searching anything.
- Sheets quota: 60 read requests per minute per user (429 seen 2026-10-27 in a 24-part bulk run,
  an RFQ line was lost). `SheetsClient` now reads each tab's header at most once a minute and the
  Results tab only when a request may have older rows to mark; a 429 waits 65s (up to 3 times).
  RFQ lines that still can't be written are kept in the store (`unsaved_rfq`), the row stays
  `DONE` with a note, and the next run writes them. A bulk batch is saved in 3 writes (Results,
  RFQ, statuses) instead of 3 per part (2026-10-28); one-by-one search still writes per part.
