# Not yet in the main copy (`/media/pragnakalpl19/projects1/Backup/sourcing-proto`)

Work done in this share copy after the 2026-10-19 merge. To be added to the main copy in one go,
once the other vendors' logins and secrets are in (decided 2026-10-21). Tick items when ported.

The main copy has its own vendor registry, generic adapter (`vendors/generic.py`, `web.py`),
`scripts/manage_vendors.py` and set-up-from-secret flow, so several items need adapting rather
than copying; those are marked **adapt**.

## 1. Login retry only when safe (2026-10-21)

- [ ] `sourcing/vendors/base.py`: `LoginFailed(message, retryable=True, credentials_sent=True)`.
- [ ] `sourcing/vendors/proponent.py`: wrong password checked before server errors; still on the
      login form with no message → `retryable=False`; `SERVER_ERROR_HINTS` → retryable; extra
      `BLOCKED_HINTS` ("too many requests", "unusual activity", "suspicious activity", "verify you
      are human", "are you a robot"); no login URL / no form → `credentials_sent=False`.
- [ ] **adapt** `sourcing/vendors/generic.py` (main copy only): give its `LoginFailed` raises the
      same `retryable` / `credentials_sent` meaning.
- [ ] `sourcing/auth/manager.py`: non-retryable `LoginFailed` → `NEEDS_ATTENTION`.
- [ ] Tests: `tests/test_login_retry.py`; in `tests/test_proponent_adapter.py` the `login_error`
      option of `MockProcart` and the two login-classification tests.

## 2. Per-vendor login and search limits (2026-10-21)

- [ ] New `sourcing/limits.py` (defaults, validation, login policy, `VendorLimiter`).
- [ ] `requirements.txt`: `tzdata`.
- [ ] `sourcing/store.py`: `get_vendor_usage` / `update_vendor_usage` in all three stores,
      `vendor_usage` in `FileStore._SAVED`; Firestore `update_vendor` / `update_vendor_usage` merge
      by top-level field (`merge=list(fields)`). **adapt**: the main copy already has
      `list_vendors` / `update_vendor`, but its Firestore `update_vendor` uses `merge=True`, which
      keeps removed `limits` keys; change it.
- [ ] `sourcing/store.py` `make_store`: refuse `file` / `memory` on Cloud Run.
- [ ] `sourcing/vendors/base.py`: `default_limits`, `limits` / `limits_set` properties,
      `RateLimited`, `RATE_LIMIT_HINTS`, `check_rate_limited`. **adapt**: the main copy's base also
      applies the vendor record's `display_name`; keep that.
- [ ] `sourcing/vendors/proponent.py`: `check_rate_limited` on single and bulk results pages and
      the bulk portal message. **adapt** `generic.py` the same way.
- [ ] `sourcing/vendors/dummy.py`, `tests/fakes.py`: `default_limits` with no gap; fake portal
      script answer `"rate_limited"`.
- [ ] `sourcing/auth/manager.py`: `login_block` before every attempt, `LoginDeferred`, attempt and
      failure counters, cooldown skipped for the in-run retry.
- [ ] `sourcing/pipeline.py`: `VendorLimiter`, gate before the lease, `before_request` before every
      search / bulk batch, `RateLimited` pass-through and back-off, `_wait_for_limit`,
      `_save_default_limits`. **adapt**: the main copy resolves vendors through its registry
      (`_vendor_key`, `vendors.record`); a vendor set up from its secret should also get the default
      limits saved; keep its `NEW_VENDOR_CHECK` flag.
- [ ] **adapt** `scripts/vendor_limits.py` → fold into the main copy's `scripts/manage_vendors.py`
      (`limits` subcommands, or make its `set` route `limits.*` keys through `limits.validate`), so
      there is one vendor tool.
- [ ] Fixes found by the end-to-end check (2026-10-21):
      `job_main` dummy factory passes `selectors` / `vendor_config` to `DummyAdapter`;
      `VendorAdapter.real_portal` (False for `DummyAdapter`) so dry runs never write a vendor's
      `limits`; a new vendor gets an empty, unconfirmed `limits` (not a frozen copy of the defaults);
      `LimitReached.kind` so each limit has its own once-a-day Chat message; no run summary when
      every row only went back to `NEW`.
- [ ] Tests: `tests/test_vendor_limits.py`, the extra cases in `tests/test_login_retry.py`.
- [ ] Docs: `docs/PIPELINE.md` §5b and the store table, `COMMANDS.md` (vendor_limits block).

## 3. MOQ quoting (client, 2026-10-21; done in another session the same day)

- [ ] "Maximum qty available for purchase" is stock, not an order limit: warehouse note says
      "max qty available …" (`pipeline.warehouse_note`, `extract.py` `max_qty`, prompt 3.7).
- [ ] Below the MOQ (or where price breaks start) Qty Quoted is the minimum (`models.rfq_quote`,
      `min_order_qty`), and Internal Comments says "MOQ 50 EA, asked 1" (`pipeline.moq_note`,
      `min_order`). Check the session's own diff for every file it touched before porting.

## 4. Wording: "team member", not "buyer" (2026-10-21)

- [ ] SourcingAI's users are Skylink team members; "buyer"/customer is who Skylink quotes. Replace
      "buyer" meaning Skylink staff in docs (`PIPELINE.md`, `RUN_FLOW_OVERVIEW.md`,
      `RFQ_Sample_Column_Review.md`, `COMMANDS.md`, `CLAUDE.md`, `SETUP.md`, …), code comments and
      `docs/SourcingAI_Sheet_Columns.xlsx` ("Filled by" = Team member). Keep the ILS "Buyer Bundle"
      name and the vendor-quote meaning in the extraction prompt (3.12). The xlsx "Condition codes"
      row now says unmatched wording is left empty.

## 5. Generic adapter brought INTO this copy (2026-10-22, for the 2FA test on a new portal)

The main copy's generic-vendor work was ported here (not a 3-way merge: too many conflicts).
Already in the main copy; when porting back, take these share-copy changes on top of it:

- [ ] `sourcing/vendors/generic.py`: `LoginFailed` raises carry `retryable` / `credentials_sent`
      (no URL / no form / no fields → `credentials_sent=False`; still on the form or unclear after
      submit → `retryable=False`); `SERVER_ERROR_HINTS` check after submit; `check_rate_limited`
      on results pages. (This ticks the **adapt** generic.py items in §1 and §2.)
- [ ] `sourcing/vendors/web.py`: the extra `BLOCKED_HINTS` and `SERVER_ERROR_HINTS` from §1.
- [ ] `sourcing/onboarding.py`: `RateLimited` stops the run (in `STOP_REASONS`, re-raised from the
      part loop); "team member" wording.
- [ ] `sourcing/pipeline.py`: registry code ported as is; with the §2 limits a vendor set up from
      its secret gets its default limits saved through the same `_no_limits` path.
- [ ] `scripts/vendor_limits.py`: resolves the vendor through `VendorRegistry` (aliases work).
- [ ] "team member" wording in `registry.py`, `vendors/__init__.py`, `manage_vendors.py`,
      `onboard_vendor.py`; `COMMANDS.md` §6b written for this copy.

Not ported (not needed here yet): `check_against_record` and the other main-copy changes to
`proponent.py` (this copy's Proponent keeps its own selector lists instead of importing `web.py`),
`job_main.py` flag code, `check_access.py` changes, the `absent` field in `models.py`, and
`tests/test_vendor_from_secret.py`.

## 6. 2FA handoff over Google Chat: Live View link + code form (2026-10-22)

New in this copy (the main copy has none of it):

- [ ] `sourcing/auth/hitl.py` (new): `PersonHandoff`, `accept_code`, `code_card`,
      `login_with_handoff`, `CodeNotGiven`.
- [ ] `sourcing/auth/manager.py`: `handoff`, `people`, `NeedsHuman.asked`.
- [ ] `sourcing/pipeline.py`: `Deps.code_handoff`, requesters passed as `people`; a code nobody
      sent puts the account in `NEEDS_LOGIN` (not blocked), message no longer says "not built".
- [ ] `sourcing/vendors/base.py`: `MfaRequired.input_selector`, `accepts_mfa_codes`,
      `submit_mfa_code`. **adapt** main's generic.py: `_pick_button`, code page after Next,
      passwordless path, `_type_code`, `_llm_code_input`, `submit_mfa_code` (take this copy's file).
- [ ] `sourcing/vendors/web.py`: `first_visible_button`, `OTHER_SIGN_IN_WORDS`, exact-text
      `NEXT_SELECTORS`, more code inputs / `MFA_TEXT_HINTS`.
- [ ] `sourcing/credentials/base.py`: `passwordless`.
- [ ] `sourcing/store.py`: `get_hitl` / `update_hitl` (FileStore: one file per request in
      `<store>.hitl/`).
- [ ] `sourcing/notify/chat.py`: `ChatAppSender`. `sourcing/config.py`: `CHAT_APP_SPACE`,
      `CHAT_APP_AUDIENCE`, `HITL_CODE_WAIT_SECONDS`. `service/job_main.py`: `build_person_handoff`.
- [ ] `service/api.py`: `/hitl/code` (GET/POST), `/chat/events`.
- [ ] `sourcing/onboarding.py` + `scripts/onboard_vendor.py`: handoff, `--login-only`,
      `--login-file`, `--headed`, `--notify`; no `check_record` for a record built from a secret.
- [ ] `.gitignore` / `.dockerignore`: `.local_store.json.hitl/`, `.local_login*.json`.
- [ ] Live View: `PersonHandoff.watch_live_view` (checks bounded by `LIVE_CHECK_TIMEOUT`),
      `adapter.person_finished_login` / `accepts_person_login` (base + generic),
      `LocalBrowserClient(live_view=)` + `LOCAL_BROWSER_LIVE_VIEW` + `--live-view`,
      `build_person_handoff` (was build_code_handoff), class renamed `PersonHandoff`.
- [ ] Fixes from the Notion test: `PASSWORD_SELECTORS` skip aria-hidden / tabindex=-1 boxes,
      `_LIST_ELEMENTS_JS` leaves them out and reports `role`, `[role='button']:has-text()` Next,
      `config.quiet_http_logs()` (webhook URL with key/token was logged by httpx).
- [ ] Saved selectors (generic.py): only `confirmed_learned()` is saved (login keys after a
      confirmed sign-in check, search keys after a search with offers / "not sold");
      `VendorAdapter.confirmed_learned` (base: everything); `_learn` never keeps index marks or
      `>> nth=`; `_selector_for` returns (use now, save) and skips generated ids/names
      (`looks_generated`), saving buttons by text and inputs by autocomplete / aria-label /
      placeholder; `_saved_fit` skips a saved selector that now finds the wrong kind of element.
      Pipeline and onboarding save `confirmed_learned()`; the report lists what wasn't saved.
- [ ] Tests: `tests/test_code_handoff.py`, local live view in `tests/test_browsers.py`.
      Docs: `COMMANDS.md` §6b, `.env.example`.

## 7. Client quoting rules: price breaks, delivery, substitutes (2026-10-23)

- [ ] `sourcing/extract.py`: prompt 2.2 / 3.4-3.6 / final check, out-of-stock lines keep price,
      breaks, currency, condition; `_apply_stock_rule` blanks them only with no stock shown.
- [ ] `sourcing/pipeline.py`: `price_qty`, `quote_price`, `_break_row`, `_stock`, `stock_note`
      (was `warehouse_note`), new `moq_note` / `price_break_note` wording, `join_comments` one
      newline, `same_part` + look-alike lines dropped in `_record` (NOT_FOUND when only those),
      `alternate_notes` (B / B lines, pointer line), `flag_for` without PN_VARIANT / DIFFERENT_PN,
      `lead_days` without `in_stock_too`, `_record` (`priced`, `quote_price`, Delivery `0` in stock).
- [ ] `sourcing/extract.py`: `description` in OFFERS_SCHEMA + prompt 3.13 + final check 7,
      checked in `_verify` (words on the page, line breaks ignored; `DESCRIPTION_NOT_ON_PAGE`).
- [ ] `sourcing/models.py`: `ResultRow.quote_price` / `points_to`, `POINTER_PRICE`, `rfq_quote`
      (pointer line, Qty Quoted = stock, Unit Price = quote_price), `as_rfq_record` (Quantity
      Requested = Qty Quoted when there's stock), `rfq_delivery` docstring.
- [ ] `sourcing/vendors/proponent.py`: two comments; Procart's new results page (seen
      2026-10-23, jqGrid commented out): `_MARK_SUBSTITUTE_BUTTONS_JS` + `_show_substitutes`
      (click "Show N Approved Substitutes for …"), `_SHOW_SELECTED_WAREHOUSE_JS` (a row's
      warehouse dropdown read as its selected warehouse), `_wait_details_loaded` +
      `_DETAILS_LOADING_JS` + `DETAILS_WAIT_SECONDS` (rows' details, with Part Condition, load
      after the page), all in `_expand_all_details`. Pipeline: `NO_CONDITION` flag for an
      in-stock line without a condition code.
- [ ] `scripts/vendor_limits.py reset-login --today` (also today's login count); COMMANDS.md §6.
- [ ] **Do not port as is:** `sourcing/browser/browserbase.py` logs the session page and live view
      link on every session (`REMOVE-BEFORE-DEPLOY`, for test runs); remove before deploying.
- [ ] Tests: `tests/test_pipeline.py`, `tests/test_conditions_and_extract.py`, `tests/fakes.py`
      (`FakeSink.rows`). Docs: PIPELINE.md §8, RFQ_Sample_Column_Review.md.

## 8. Vendor login update page (plan step 6, built 2026-10-23)

- [ ] New: `service/web_auth.py`, `service/credentials_ui.py`, `sourcing/credentials/access.py`,
      `scripts/manage_logins.py`, `tests/test_credentials_ui.py`.
- [ ] `service/api.py`: routers added, `/credentials/update` 501 stub removed.
- [ ] `sourcing/config.py`: `_env_list`, OAuth / session / admin settings.
- [ ] `sourcing/store.py`: `list_accounts()` on all stores.
- [ ] `sourcing/credentials/base.py`: `update(..., must_exist=False)`.
- [ ] `sourcing/pipeline.py`: `Deps.public_base_url`, update-page link in the wrong-password message;
      `service/job_main.py` passes it.
- [ ] `cloudbuild.yaml` (API env + secrets, worker `PUBLIC_BASE_URL`), `infra/setup_gcp.sh`,
      `infra/secrets.env.example`, `.env.example`.
- [ ] Tests: `tests/test_api.py`, `tests/test_pipeline.py`, `tests/test_sheets_and_creds.py`.
      Docs: `docs/CREDENTIAL_UPDATE_UI_PLAN.md` (rewritten), PIPELINE.md §1/§4, COMMANDS.md §8/§9,
      CLAUDE.md.
## 9. Internal Comments wording (2026-10-23)

- [ ] `sourcing/pipeline.py`: `moq_note` says `PRICED AT` instead of `QUOTED` (Qty Quoted is the
      stock, 0 out of stock); `stock_note` leaves warehouses showing 0 out of `STK`, `OUT OF STOCK`
      when all show 0; `_by_date` (2026-10-26): same-date deliveries one entry, added up.
      Tests: `tests/test_pipeline.py`. Docs: PIPELINE.md §8.

## 10. RFQ fixed values for AvSight (client, 2026-10-26)

- [ ] `sourcing/models.py` `as_rfq_record`: Serialized `N`, UOM `EA`, Type Requested `Outright`,
      Condition Code = Condition Quoted; Delivery empty on No Quote lines. `rfq_quote` +
      `qty_due_in`: out of stock, Qty Quoted = total Qty due in. Tests:
      `tests/test_excel_sheet.py`. Docs: RFQ_Sample_Column_Review.md.

## 11. Login update page restyle + Cloud Run plan (2026-10-26)

- [ ] `service/web_auth.py` `page(..., wide=False)` / `_PAGE`: header bar, card centred on the
      screen (narrow for forms, wide for the table), clamp() sizes, light/dark colours, full-width
      buttons and stacked table on phones; sign-in page `<h1>` + lead.
      `service/credentials_ui.py`: `_STATE_BADGE`, `<h1>` + lead line per page, field hints,
      `wide=True` on the login list, "Last change" column, back links.
      Docs: `docs/CLOUD_RUN_DEPLOY_PLAN.md` (new).

## 12. Out-of-stock quoting rules (client answers, 2026-10-27)

- [ ] `sourcing/models.py`: `parse_date`, `due_in`, `soonest_due_in` (replace `qty_due_in`),
      `lead_time_days`, `_out_of_stock`; `rfq_quote` (soonest-date qty, customer's qty when nothing
      is due in, No Quote without a delivery), `rfq_delivery` (days to the soonest due date).
      `sourcing/pipeline.py`: `stock_note` (`X DAY LEAD TIME, FOB: …`), `flag_for` (`NO_DUE_DATE`).
      Tests: `tests/test_excel_sheet.py`, `tests/test_pipeline.py`. Docs: PIPELINE.md §8,
      RFQ_Sample_Column_Review.md.

## 13. Login update page redesign (from sourcing-login-update-pack.zip, 2026-10-27)

- [ ] `service/web_auth.py`: new `_PAGE` (navbar, cards, badges with dots, show/hide password and
      "passwords match" inline script), new sign-in card, `page()` without `wide`, user pill.
      `service/credentials_ui.py`: card markup, `_STATE_TOOLTIP` + `_status_badge`, breadcrumb
      back link, "Password last changed" column, alerts with icons. Replaces item 11's look.
      `service/api.py`: `security_headers` middleware (CSP, no framing). Test:
      `test_credentials_page_polish`. Sign-in refusals: `signin_problem` card ("Access denied", the
      allowed domain, "Sign in with a different account"), `_google_button`, `_domains_text`.
      Not taken from the zip: its `credentials/local.py` (file
      passwords), older `models.py` / `pipeline.py`, `python-multipart`.

## 13. Substitute without stock, quoting without a condition (client, 2026-10-27)

- [ ] `sourcing/pipeline.py` `alternate_notes`: `APPROVED SUBSTITUTE ALSO OFFERED WITH N DAY LEAD TIME`
      (days added 2026-10-28, grouped by days)
      for substitutes without stock; `NO_CONDITION` also on a quoted out-of-stock line (imports
      `rfq_quote`). `sourcing/models.py` `rfq_quote`: condition code needed only in stock,
      Condition Quoted empty when unknown. Tests: `tests/test_pipeline.py`,
      `tests/test_excel_sheet.py`. Docs: PIPELINE.md §8, RFQ_Sample_Column_Review.md.

## 14. Proponent closest-match suggestion rows left out (2026-10-27)

- [ ] `sourcing/vendors/proponent.py`: `_CLOSEST_MATCH`, `_RECORD_SPLIT`, `_drop_suggestions`;
      `_grid_text` returns the filtered records (raw ones still dumped). Test:
      `tests/test_proponent_adapter.py`. Docs: PIPELINE.md §8.

## 15. Google Sheets read quota (429) and RFQ lines kept (2026-10-28)

- [ ] `sourcing/sheets.py`: `HEADER_SECONDS`, `unmarked_requests`, `_request_key`; `SheetsClient`
      caches the RFQ worksheet, headers (per tab, 60s) and open requests; `_append` is no longer
      static; `supersede_results` reads the tab only when needed. `sourcing/sinks/sheet.py`:
      `QUOTA_WAIT`, `QUOTA_RETRIES`, `over_quota`, `write_rfq` (public, returns the error).
      `sourcing/sinks/base.py`: `WriteOutcome.rfq_error`. `sourcing/pipeline.py`: `unsaved_rfq` in
      `_write_results` / `_write_unsaved_results`. `sourcing/store.py`: `rows_with_unsaved_results`
      also returns `unsaved_rfq` rows. Tests: `tests/test_sheets_and_creds.py`,
      `tests/test_pipeline.py`. Docs: CLAUDE.md, PIPELINE.md.

## 16. ILS part search script; admins add logins on the login page (2026-10-28)

- [ ] `scripts/ils_part_search.py` (new): GetPartsAvailability with the login kept safe
      (Secret Manager only, TLS 1.2+, no redirects, password scrubbed, DOCTYPE refused).
- [ ] `/credentials/new` (add a login) and `/credentials/editors` (who may change a shared
      login), admins only: `service/credentials_ui.py`; `/auth/grant` + encrypted `sai_gcp`
      cookie (`gcp_credentials`, `forget_grant`, `exchange_code(with_token=)`, `signin_problem(link=)`,
      textarea CSS): `service/web_auth.py`; `create_login_secret`: `sourcing/credentials/gsm.py`;
      `is_admin`: `sourcing/credentials/access.py`; `cryptography` in `requirements.txt`.
      Tests: `tests/test_credentials_ui.py`. Docs: CREDENTIAL_UPDATE_UI_PLAN.md, CLAUDE.md.
- [ ] `/credentials/find` (list a login whose secret was made with gcloud) in
      `service/credentials_ui.py`; `scripts/manage_logins.py` `_vendor(..., check_secret=)`,
      `_secret_exists`: `add` accepts a vendor without a record when its secret exists.

## 16. Bulk batch saved in 3 writes (2026-10-28)

- [ ] `sourcing/pipeline.py`: `_batch_results` / `_batch_statuses`, `_write_statuses` (one call,
      retries, 429 wait), `_note_status`, `_save_results` (one sink write for many rows, failure
      keeps every row), `_process_bulk` collects then saves. `sourcing/sheets.py`:
      `set_statuses` / `_status_updates` (Google Sheet), `set_statuses` (Excel). Tests:
      `tests/test_pipeline.py`. Docs: CLAUDE.md, PIPELINE.md.

## 17. SUPERSEDED only for rows of an earlier run (2026-10-28)

- [ ] `sourcing/sheets.py`: comment above `SUPERSEDED`; `superseded_flags(..., this_run)`;
      `SheetsClient._written` / `ExcelSheet._written` (rows this run wrote), used by
      `supersede_results`. Tests: `tests/test_sheets_and_creds.py`, `tests/test_pipeline.py`.
      Docs: PIPELINE.md.

## 17. Log out after a failed login that may have gone through (2026-10-28)

- [ ] `sourcing/vendors/base.py` `may_be_signed_in`; `sourcing/auth/manager.py`
      `_maybe_signed_in`, `_attempt`, `maybe_signed_in`, `logout_if_maybe_signed_in` (called in
      `open_authenticated`'s error path); `sourcing/pipeline.py` cleanup after a failed re-login;
      `sourcing/onboarding.py` logout when `maybe_signed_in`. Tests: `tests/test_login_retry.py`.
      Docs: PIPELINE.md.

## 18. Proponent results page without jqGrid: substitutes, suggestions, warehouses (2026-10-28)

- [ ] `sourcing/vendors/proponent.py`: `WAREHOUSE_SWITCH_SECONDS` / `WAREHOUSE_SWITCHES_MAX`;
      `_MARK_COLLAPSED_ROWS_JS`, `_HIDE_SUGGESTIONS_JS`, `_MARK_WAREHOUSE_SELECTS_JS`,
      `_SWITCH_WAREHOUSE_JS`, `_READ_WAREHOUSE_ROW_JS`, `_ADD_WAREHOUSE_ROWS_JS`; `_GRID_EXTRAS_JS`
      skips hidden rows; `_SUBSTITUTE_NOTE`, `_listed_substitutes`, `_keep_listed_substitutes`
      (called in `_read_results` and `_offers_from_pages`); `_expand_all_details` (no early return,
      new steps) with `_hide_suggestions`, `_open_collapsed_rows`, `_read_other_warehouses`,
      `_pick_warehouse`. `sourcing/extract.py`: look-alike rule in 3.9. Tests:
      `tests/test_proponent_adapter.py` (new-layout page). Docs: PIPELINE.md.

## 19. Substitute note when the requested part is quoted out of stock (2026-10-28)

- [ ] `sourcing/pipeline.py` `alternate_notes`: the requested part's line says
      "APPROVED SUBSTITUTE ALSO OFFERED[ WITH N DAY LEAD TIME]: PN B" whenever it is
      quoted (price, and in stock or a delivery via `rfq_delivery`), not only in stock;
      "QUOTING PN B" only when it can't be quoted. Test:
      `test_out_of_stock_requested_part_with_a_lead_time_still_offers_the_substitute`.

## 20. Data entry rules as tests (2026-10-28)

- [ ] `docs/DATA_ENTRY_RULES.md` (client's checklist + where the code follows it) and
      `tests/test_data_entry_rules.py` (one test per rule, open gaps marked `GAP`); CLAUDE.md line.

## 21. No Quote Unit Price "0", not "0.00" (2026-10-28)

- [ ] `sourcing/models.py` `rfq_quote`: No Quote Unit Price `"0"`. Tests: `tests/test_excel_sheet.py`,
      `tests/test_data_entry_rules.py`. Docs: PIPELINE.md, RFQ_Sample_Column_Review.md.

## 22. Logout link inside an account menu (2026-10-29, DASI)

- [ ] `sourcing/vendors/generic.py` `_logout_in_menu` / `_open_menu`: no visible logout link →
      open the profile / account menu (saved, `web.ACCOUNT_MENU_SELECTORS`, then the LLM once per
      run; `_NOT_A_MENU` words never clicked), used after the login submit, in the sign-in check
      and at logout; page text read again before the LLM classifies the page. `web.py`: the list
      and `title` in `_LIST_ELEMENTS_JS`. `extract.py` `classify_login_page`: signed-in signs.
      Tests: `tests/test_generic_adapter.py` (`test_logout_in_a_profile_menu_...` and 3 more).

## 23. Generic search: ways to run a search, and no NOT_FOUND when it didn't run (2026-10-29, DASI)

- [ ] `sourcing/vendors/generic.py` `_open_results_by_form`: paste + Enter → Enter again (part typed
      again if the box emptied) → type key by key + search button (list, then LLM
      `SEARCH_BUTTON_QUESTION`; a saved button is clicked straight away) → `_search_by_llm_steps`
      (LLM names one new step at a time: click / type / enter / results / give_up, up to
      `MAX_SEARCH_STEPS`, refused words `_NOT_A_SEARCH`) within `SEARCH_FORM_BUDGET_SECONDS`.
      Nothing worked → RuntimeError listing every step tried, and `search_failed` stops the run's
      other parts at once. "Did it run" ignores drop-downs (`web.content_text`). `extract.py`
      `next_search_step` + `SEARCH_STEP_SCHEMA`. `web.py`: `SEARCH_BUTTON_SELECTORS` (title,
      never advanced / multiple / bulk), `textarea` in `_LIST_ELEMENTS_JS`. Search box LLM question
      names header placeholders. Tests: `tests/test_generic_search.py` (keystroke box, button next
      to it, history drop-down, box emptied by Enter, Multiple box via LLM steps, refused step,
      search that never runs + next part not tried).

## 24. DASI adapter and per-vendor extraction prompt (2026-10-29)

- [ ] `sourcing/vendors/dasi.py` (DasiAdapter on GenericAdapter: `exact=true` addresses, bulk
      `type=multiple&exact=true&product=...`, Fulfilled by = DASI Only, Location / Ship From = DASI
      Miami HQ (FL) (`ship_from`), opens all blocks with one script click, reads each grid as lot
      records with the Trace/Tag hover text (`RECORDS_JS`); View clicks off (`view_details_max`);
      DASI rules 1-2 (`_combine`: same cond / ship from / lead / MOQ / price = one line, qty added,
      traces listed; Miami only) in docs/DATA_ENTRY_RULES.md + `test_rule_dasi_*`); page scripts run
      on Playwright's blocks (`evaluate_all`, never `document.querySelectorAll`: hidden by Salesforce);
      bulk records read in `LOTS_PER_CALL` pieces at once; a cut-off LLM answer = BulkResultsIncomplete)
      and `sourcing/vendors/dasi_prompt.py` (shared prompt + "## 5. DASI Pages"). `vendors/__init__.py`:
      registered; an auto-made generic record no longer pins a vendor that has its own adapter.
      `extract.py`: `system=` on `extract_offers` / `extract_offers_bulk`. `generic.py`:
      `extract_system`, `_results_text` hook. Tests: `tests/test_dasi_adapter.py`.

## 25. RFQ tab by default, Results tab only with --results (2026-10-29)

- [ ] `service/job_main.py` (`--results` -> `WRITE_RESULTS_TAB`; `--rfq` kept, no effect),
      `sourcing/sinks/sheet.py` (`results=`; RFQ-only write retried, `_write_rfq_only`),
      `sourcing/sheets.py` `has_rfq`. Docs: COMMANDS.md, PIPELINE.md, RFQ_Sample_Column_Review.md,
      CLAUDE.md. Tests: `test_job_flags.py`, `test_rfq_tab_alone_is_the_write_a_row_depends_on`.

## 26. No second sign-in check right after a login that saw Log out (2026-10-30, DASI)

- [ ] `sourcing/vendors/base.py` `signed_in_proof`; `sourcing/vendors/generic.py` `_proven`
      (from `_signed_in` and the already-signed-in path of `_open_login_page`, reset in
      `login`); `sourcing/auth/manager.py` `login` skips `is_authenticated` when it is set.
      Tests: `tests/test_generic_adapter.py`, `tests/test_login_retry.py`.

## 27. DASI trace: hover-text source in brackets (2026-10-30)

- [ ] `sourcing/vendors/dasi_prompt.py` §5.2 Trace row: `FAA 129 (CHINA SOUTHERN AIRLINES)`;
      combined lines `FAA 129, FAA 121 (CHINA SOUTHERN AIRLINES, REPUBLIC AIRWAYS)`.

## 28. DASI lots with another UOM are not combined (2026-10-30)

- [ ] `sourcing/vendors/dasi.py` `COMBINE_BY` + `uom`. Test:
      `test_rule_dasi_1_lots_with_another_uom_are_not_combined`. Docs: DATA_ENTRY_RULES.md.

## 29. No Quote keeps a known condition (2026-10-30, DASI SV lots)

- [ ] `sourcing/models.py` `rfq_quote`: No Quote Condition Quoted = the condition code when known,
      `-` otherwise. Test: `test_rfq_line_without_price_is_no_quote`. Docs: PIPELINE.md §8,
      RFQ_Sample_Column_Review.md.

## 30. Tag Date from the page (2026-10-30, DASI Tag Details)

- [ ] `sourcing/extract.py`: `tag_date` in OFFERS_SCHEMA (required), prompt 2.2 / 3.12, checked
      against the page like trace. `sourcing/models.py`: `Offer.tag_date`, `ResultRow.tag_date`,
      RFQ `Tag Date`. `sourcing/pipeline.py` `request_details`. `sourcing/vendors/dasi_prompt.py`
      Tag Details row. Tests: `test_tag_date_is_read_and_checked_against_the_page`,
      `test_rfq_line_without_price_is_no_quote`.

## 31. Lines in the request's UOM only; RFQ UOM = the request's (2026-10-30, LB requests)

- [ ] `sourcing/extract.py`: `uom` in OFFERS_SCHEMA (required), prompt 3.14, checked against the
      page; `sourcing/vendors/dasi_prompt.py` UOM line. `sourcing/models.py`: `_UOM_ALIASES`,
      `uom_code`; RFQ `UOM` = request's, else line's, else EA. `sourcing/pipeline.py`: `same_uom`,
      filter + NOT_FOUND note in `_record`. Tests: `test_rule_7_uom_...`, `test_rule_7_no_line_...`,
      `test_rfq_fixed_values_whatever_the_request_says`. Docs: DATA_ENTRY_RULES.md.

## 32. DASI trace: whole wording, no 50-character limit (2026-10-30)

- [ ] `sourcing/vendors/dasi_prompt.py` §5.2 Trace row: copy the paired Trace/Tag as written,
      never shorten or drop a source. Test:
      `test_rule_dasi_1_trace_is_whole_with_every_source_of_the_same_trace`.

## 33. Aviall adapter: Boeing ID after a 2FA screen, shop.boeing.com search (2026-10-30)

- [ ] New `sourcing/vendors/aviall.py` (`AviallAdapter` on the generic adapter; `_mfa` tries
      "Log in with your Boeing ID" from the home page once, then the usual person handoff; Log in
      link in the avatar menu; `boeingsearch?query=` address; bulk via the "Multi-line Ordering"
      dialog's Copy & Paste box, "part, qty" lines, "recently searched" unticked, never a cart/order
      button; `_reveal` writes hover text, icon labels and the Price Breaks pop-up into each card; `logout` tries again from the home page when the first try can't confirm it, 2026-11-02) and `sourcing/vendors/aviall_prompt.py` (shared
      prompt + "## 5. Aviall Pages"). `sourcing/vendors/__init__.py`: `aviall` in `ADAPTER_TYPES` and `ADAPTERS`. **adapt** to the
      main copy's generic adapter. Tests: `tests/test_aviall_adapter.py`.

## 34. Worker deploy plan + access check for several vendors (2026-10-30)

- [ ] New `docs/SOURCING_WORKER_DEPLOY_PLAN.md`; `docs/CLOUD_RUN_DEPLOY_PLAN.md` marked superseded.
- [ ] `scripts/check_access.py`: `--vendor proponent,dasi,aviall` checks each vendor's secret
      and adapter; the vendor line names the adapter class that will run. **copy**.

## 35. Logout handoff: a person logs out through Live View (2026-11-02)

- [ ] `sourcing/auth/hitl.py`: `LOGOUT_TRIES`, `PersonHandoff.watch_logout`, `logout_with_handoff`
      (2 program tries, then the Live View link to the requesters; checked every 15s in a second tab).
      `sourcing/vendors/base.py`: `accepts_person_logout`, `person_finished_logout`.
      **adapt** `sourcing/vendors/generic.py`: `signed_out_check_url`, `person_finished_logout`.
      `sourcing/vendors/aviall.py`: `signed_out_check_url` = home page.
      `sourcing/pipeline.py` `_logout(…, people)` and `sourcing/auth/manager.py`
      `logout_if_maybe_signed_in(…, people)` use `logout_with_handoff`.
      Tests: `tests/test_logout_handoff.py`; Aviall test fixture opens its own context.

## 36. Aviall sign-in steps from the team (2026-11-02)

- [ ] `sourcing/vendors/aviall.py`: replaces the Boeing ID fallback of item 33. `_open_login_page` /
      `_click_to_form` click "Log in to Boeing Distribution", then "Log in with Boeing OneLogin
      ID" (vendor config `distribution_login_button`, `onelogin_button`; LLM pick otherwise) to the
      username page; on the 2FA box `_mfa` opens the login URL again once (`_after_relogin`,
      nothing typed), then the usual person handoff (method `relogin`). **adapt** with item 33.
      Tests: `tests/test_aviall_adapter.py` (mock portal follows the new steps).
- [ ] Same day, later: step 1 opens a dialog, not a page (`_click_and_wait` waits for the dialog,
      the next button, a form, or a new page whose address stopped changing); on the 2FA box the
      login URL is reopened and steps 1-2 clicked again; longer waits for Boeing (action 45s,
      page load 180s). `sourcing/onboarding.py`: logs out with
      `logout_with_handoff` (2 tries, then Live View), as the worker does. **adapt**.
- [ ] After the first live run (2026-11-02): back on the same address after the OneLogin click
      counts as arrived and is checked for Log out in the avatar menu; `_loaded` (load event +
      2s) instead of waiting for a quiet network; Boeing's username box / LOG IN button known
      (`KNOWN_SELECTORS`). `sourcing/vendors/web.py`: `_none_visible` (one query for a whole
      selector list, used by `_first_visible` / `first_visible_button`), OneTrust button ids in
      `_consent_button`. **copy** the web.py part.
- [ ] Second live run (2026-11-02, saved pages): sign-in confirmed from the avatar beside the cart
      (`_avatar`: "Welcome, <name>" / Log out in `#my-account-profile`), never My Account; log out
      from the avatar, account page as fallback; sign-in check on the home page (`probe_url`);
      "Shopping as" bar as proof where the avatar isn't opened. Bulk search inside the Multi-line
      dialog only (Copy & Paste tab, paste event, Search button by its words; dialog HTML saved on
      failure). Results: feature tour closed, full `=` part number from the product link written
      into each card, card = one product (not the header), only icons beside stock words hovered,
      mini-cart / recommendations / tour removed from the text, condition `NE` when not shown
      (`condition_not_shown`). `aviall_prompt.py` updated from the live pages. `web.py`:
      `LOGIN_LINK_SELECTORS` skip logout addresses (Boeing's `.../accesslogin/Logout.faces`
      signed the run out). **adapt** / **copy** web.py.
- [ ] Third live run (2026-11-02, team's screenshots): the Aviall flow written out (4 steps with a
      "done" check each, logged as "Aviall step ..."); `_llm_step` (the LLM picks a step's element
      from the visible elements, open pop-ups first, told the job; cart/order/delete/account
      changes refused). Multi-line box = `boeing-generic-popover` / `textarea.bulkSearchTextArea`,
      Search found around the box. Logout confirm dialog ("Are you sure you want to log out?").
      WalkMe tour hidden by CSS + No thanks. Superseded card's own link (not View Replacement);
      Stock By Location pop-up read. **adapt**.
- [ ] Pop-ups by a click, not hover (team, 2026-11-02): every pop-over button of a card
      (`popoverclass` / `ngbpopover`, icon-only buttons; never links, cart, quantity, switches), back
      if a click changes the page; hover off by default. Product page (`/cpd/...`, second tab) read
      for "Lead Time (Days)" only for a priced, out-of-stock line without a lead time
      (`product_pages`, max 10 a search). **adapt**.
- [ ] Fourth live run (2026-11-02): Copy & Paste box focused from code, never clicked (a click
      timed out under the editor's own layer); Search click falls back to the element's own click.
      Logout confirm found by its words outside the avatar dropdown (`CONFIRM_LOGOUT_JS`, link or
      button), sign-out hosts allowed for that click only (`SIGN_OUT_HOSTS`), page saved when no
      confirm is found. Cards = the site's `[data-testid=productListView]`. **adapt**.
- [ ] Fifth live run (2026-11-02, logout still open): the confirm is the Log out inside the box
      that asks "Are you sure you want to log out?" (not the last Log out on the page); up to 3 such
      dialogs answered (`CONFIRM_ROUNDS`); signed out only on proof (home avatar / "Shopping as"
      not signed in AND the login page shows "Log in to Boeing Distribution"). **adapt**.
- [ ] Login clicks that timed out (2026-11-02, Browserbase session a13f1695: /aviation-supply now
      goes to /distribution; 45s of nothing, twice): sign-in clicks fall back to the element's own
      click after 10s (`_click`), cookie banner + feature tour handled on the login pages, login page
      wait 60s and logged, page HTML + screenshot saved when stuck (`_dump_html`). `web.py`: OneTrust
      Allow all / close ids, "Allow" wording. **adapt** / **copy** web.py.
- [ ] Faster login (2026-11-02, from the Browserbase log of a working login): waits poll every 0.3s
      (`POLL_SECONDS`), no full-load wait after a click, address stable 2s; after the 2FA box
      straight to /distribution (`DISTRIBUTION_URL`, vendor config `relogin_url`); signed-in check
      reads the "Shopping as" bar before opening the avatar. The OneLogin button stays a click: its
      address carries a one-time PKCE code_challenge made by the page. **adapt**.
- [ ] Known selectors first (2026-11-02, 15:32 run: 98s to see the login button, 3m40s re-login):
      each login step looks for its few known selectors (`LOGIN_PAGE_KNOWN`, `KNOWN_*`) and only after
      `KNOWN_FIRST_SECONDS` (20s) the general lists; cookie banner looked at once per page
      (`_cookies`); Akamai "Access Denied" ends the wait at once. Measured on Browserbase: one known
      selector 0.3s, the joined general list 3-5s, a pass through it up to ~40s. **adapt**.
- [ ] Bulk + debugging (2026-11-02): the Multi-line dialog closed with its X after Search (it stays
      over the results); signed in / out read from the results (Add to Cart, price / "log in to
      view") and on the home page from the "Shopping as" bar or sign-in button before the avatar.
      Every click / open / type logged as "Aviall action N" plus each page address; DEBUG_TRACE=1
      saves a Playwright trace at logout or a failed login. Dry-run on the saved live dialog and
      results page. **adapt**.
- [ ] Supplier + pop-ups (2026-11-02, 17:27 run: pop-ups stopped after 90s, 10 of 18 read, the last
      cards had no Stock By Location): only cards of "Boeing Distribution (formerly Aviall)" read
      (`[data-testid=panda-supplierName]`, vendor config `supplier`, "" = all); Shelf Life pop-ups
      skipped; Stock By Location / Price Breaks first; all pop-ups read in the page in one pass
      (`READ_POPUPS_JS`, one round trip instead of ~9s each). **adapt**.
- [ ] "On Order (Available 11/26/2026)" (2026-11-02, MS20995C32=8D went out as No Quote): the date
      becomes days from today (`_date_to_days`), a passed date is dropped so the product page's
      Lead Time (Days) is read; prompt row added. **adapt**.
- [ ] Speed from the 18:01 / 18:09 runs: bulk Search = the button's own click, then a wait for the
      results on the same page (was a 10s blocked click + 45s wait for a new page); sign-in check
      reads the current page first (bar / Add to Cart), home page only when it doesn't tell (~20s
      a not-found part); logout checked on the login page only (~15s). **adapt**.

## 37. AAR adapter (2026-11-03)

- [ ] New `sourcing/vendors/aar.py`, `sourcing/vendors/aar_prompt.py`, `tests/test_aar_adapter.py`;
      `aar` / `aarcorp` / `aar_corp` in `sourcing/vendors/__init__.py`. **adapt** (registry).
- [ ] `browser_stealth` = Browserbase proxy only (2026-11-04): advanced stealth is Enterprise-only
      (403), dropped from `sourcing/browser/browserbase.py`; proxies not in the plan either (402),
      so AAR has `browser_stealth` off. **adapt**.
- [ ] `human_typing` vendor setting (2026-11-04, on for AAR): login fields typed key by key
      after a mouse move + click (`GenericAdapter._type_login_field`). **adapt**.
- [ ] AAR signed-in check / logout through the account page (2026-11-04): `_account_state`
      (`/customer/account/` with the browser's cookies, 200 vs redirect to login), Magento's
      `/customer/account/logout/`; unknown post-login page saved with `DEBUG_DUMP_PAGES`. **adapt**.
- [ ] Alternate part numbers in Internal Comments only, for Wencor and Aviall (team, 2026-11-04):
      `SearchOutcome.alternates`, `GenericAdapter.listed_alternates`, Wencor `_interchanges`,
      Aviall `_note_replacements` / `REPLACEMENT_PNS_JS`, `pipeline.add_listed_alternates`,
      `web.loose_key`; tests in test_wencor_adapter / test_aviall_adapter / test_pipeline. **adapt**.
- [ ] Onboarding never closes a session that may be signed in (team rule, 2026-11-04):
      `_hold_until_logged_out` in `sourcing/onboarding.py` waits with no time limit for a
      person (shown window / live view) or retries the logout; not after a 2FA stop.
      `LocalBrowser.shows_window`. Worker path not changed yet. **adapt**.

## 38. Monroe Aerospace adapter (2026-11-04)

- [ ] New `sourcing/vendors/monroe.py`, `sourcing/vendors/monroe_prompt.py`, `tests/test_monroe_adapter.py`;
      `monroe` / `monroeaerospace` / `monroe_aerospace` in `sourcing/vendors/__init__.py`
      (the login update page's vendor list reads `ADAPTERS`, so it lists Monroe with no UI change;
      test added to `tests/test_credentials_ui.py`). **adapt** (registry).
- [ ] Generic login: sign-in form inside an iframe (`_form_frame`, `_frame_usable`,
      `_wait_for_form`, `_login_text`; typing/clicking in the frame), LLM picks the login link
      once when the rules' link shows no form (`_llm_login_link`); `web.LOGIN_LINK_SELECTORS`
      never match forgot / reset / register. Tests in `tests/test_generic_adapter.py`. **adapt**.
- [ ] Monroe product pages in their own tabs (3 at once, images blocked): variants table (stock,
      per-choice stock -> Internal Comments), Bulk Discounts pop-up (price breaks); one more try
      without the table, then fetched HTML (`PRODUCT_HTML_JS`), then the browser; parts not
      listed even alone -> not-found outcome (no pipeline retry). **adapt**.
- [ ] `pipeline.min_order`: a stated MOQ always wins over where price breaks start (Monroe,
      2026-11-04). Monroe: variants table totals only (no filter choices, no per-choice note);
      `tagged_by` only from a named tag / cert source label (`_TAG_SOURCE`); never
      `Industry Standards/Certification` (a spec standard, team 2026-11-05). Offers checked against
      `page_facts` (`MonroeAdapter._finish`): MOQ / tag / stock / price / breaks the page's own,
      trace and warehouses cleared, offers for no product read dropped. **adapt**.
- [ ] Monroe currency from the page's `data-currency-code` ("Currency code of the page: USD"),
      "SOLD IN INCREMENTS OF N" in Internal Comments when N > 1. **adapt**.
- [ ] `extract.py`: `"In stock"` without a number kept by `_verify` (`IN_STOCK_STATUS`), counted
      by `in_stock()`; prompt 2.2 / 3.2 say so (Monroe run 16:28: every line No Quote). **adapt**.
- [ ] `docs/NEW_VENDOR_CHECKLIST.md`. **copy**.
- [ ] `sourcing/vendors/assist.py` (PageAssist) + `VendorAdapter.assist`; generic `confirmed_learned`
      keeps `assist_*` recipes. Proponent: warehouse dropdown click on the new custom control
      (`bb-custom-select`), LLM fallback with the row check, gaps flagged `WAREHOUSES_INCOMPLETE`
      + "OTHER WAREHOUSES NOT READ"; "2758 CHARGED" substitute parsing. Monroe: LLM opens the
      Bulk Discounts pop-up when its rates aren't read, else `PRICE_BREAKS_NOT_READ`. Pipeline:
      `WAREHOUSES_SHORT` flag, page actions in the Chat summary. **adapt**.
- [ ] Login says signed in but the probe can't prove it (`login_claimed`, set in
      `GenericAdapter._signed_in`): the auth manager searches once instead of logging in again
      (audit `login_unconfirmed`); empty results still need the probe for NOT_FOUND. Monroe:
      `account.php` 302 to `account.php?...` = signed in; already signed in -> no login page.
      Extraction prompt: "sign in to see price" pages are `login_page`. **adapt**.
- [ ] Not signed in on the live portal yet; the secret `sourcing-vendor-monroe` is added later
      (`/credentials/new`). Checked against the public pages only (cards, product page).

## Aircraft Spruce adapter (2026-11-05)

- [ ] `sourcing/vendors/aircraft_spruce.py` + `aircraft_spruce_prompt.py`, registered in
      `vendors/__init__.py` (`aircraft_spruce`, `aircraftspruce`, `spruce`, ...); tests
      `tests/test_aircraft_spruce_adapter.py`. Product page by `catalog/pnpages/<part>.php`, else
      the search box (a POST) with the part number, else with the Part Description / Keyword
      (`FOUND_BY_DESCRIPTION`); the requested quantity typed into the Quantity box (stock line per
      quantity, no stock number on the site); signed in = navbar logout link. **copy** (built on
      `GenericAdapter`). Login fields pinned (`KNOWN_SELECTORS`, `human_typing`); reCAPTCHA image
      challenge -> `MfaRequired("captcha")` (person through Live View, no retry).
- [ ] Secret `sourcing-vendor-aircraft-spruce` is added later (`/credentials/new`). Checked
      against the public pages only.

## CAPTCHA solving trial switch (2026-11-05, development only)

- [ ] `DEV_ALLOW_CAPTCHA_SOLVING` (`config.captcha_solving_allowed`, default off): Browserbase
      `solve_captchas` on, Aircraft Spruce waits up to 45s for the solver before asking a person.
      Client paused the "never solve a CAPTCHA" rule for a few days. **don't port** unless the
      client keeps it; code marked `REMOVE-AFTER-CAPTCHA-TRIAL`.

## Incora adapter (2026-11-05)

- [ ] `sourcing/vendors/incora.py` + `incora_prompt.py`, registered in `vendors/__init__.py`
      (`incora`, `shop_incora`, `wesco`, `wesco_aircraft`); tests `tests/test_incora_adapter.py`.
      Angular app on shop.incora.com, sign-in at Azure AD B2C (login.incora.com), OAuth tokens in
      localStorage (signed in = live `access_token`). Search = the Bulk Search page with the parts
      written to localStorage `ecom.products.bulk.search.search.code`; the result is read from
      the page's stored data `ecom.products.bulk.search` (all parts, all stock lines, every table
      page); price breaks per stock line from the $ pop-up's own address (fetched inside the
      page). Logout = the app's logOut (tokens removed, B2C end-session -> /logged-out).
      **copy** (built on `GenericAdapter`).
- [ ] First live runs 2026-11-05 (test_runs/22): login only from shop.incora.com (B2C addresses
      never saved/reused), token refresh waited for, Shelf Life 0 left out, empty price list ->
      "NO PRICE SHOWN ON INCORA", `pricingUnitOfMeasure` -> `PRICING_UOM_DIFFERS`; prompt: the seen
      certification wordings. **copy**.
- [ ] Vendor setting `delivery_days` (`VendorAdapter.default_delivery_days`, Incora "7"): RFQ
      Delivery of an in-stock line instead of 0 (client, 2026-11-06). **copy**.
- [ ] Pipeline: `LlmKeyRejected` / `llm_key_rejected` (a 401/403 from the LLM provider stops the
      account's searches; rows FAILED). Tests `tests/test_llm_key_rejected.py`. **copy**.
- [ ] Sheet trigger (2026-11-06): `apps_script/` (Code.gs, appsscript.json, README), `/trigger`
      run-again flag (`RERUN_FLAG` in store.py, passes in `job_main.run`), `TRIGGER_RUN_LOCAL` /
      `TRIGGER_LOCAL_ARGS` (config.py, api.py). Tests `tests/test_sheet_trigger.py`. **copy**.
- [ ] Sheets add-on (2026-11-12): `apps_script/addon/`, script moved to `apps_script/per_sheet/`;
      `/trigger` accepts the add-on user's Google token (`_caller`, `_addon_refusal`,
      `ADDON_OAUTH_CLIENT_IDS`), notes `last_edit_at`; `job_main.wait_until_quiet`
      (`TRIGGER_QUIET_SECONDS`); local-mode wait in api.py. Tests in `tests/test_sheet_trigger.py`. **copy**.

## Textron Aviation adapter (2026-11-12)

- [ ] `sourcing/vendors/textron.py` + `textron_prompt.py`, registered in `vendors/__init__.py`
      (`textron_aviation`, `textron`, `textronaviation`, `txtav`); tests
      `tests/test_textron_adapter.py`. ww2.txtav.com (ASP.NET MVC): login with the reCAPTCHA-v3
      Log In button wait, 2FA by the Live View handoff, signed in = /Dashboard; one part by
      `PartsDetail/<part>`, bulk by the Multiple Part Search form (part + qty per row) with tabs
      for what it misses (`bulk_mode`, `tabs_parallel`, `qty_box_selector` vendor settings); the
      site's 404 page -> `LoginBlocked`. Not yet signed in on the live portal; secret
      `sourcing-vendor-textron-aviation` comes later. **copy** (built on `GenericAdapter`).

- [ ] This file itself, once everything above is ticked.
