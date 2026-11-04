# Plan: any vendor from data, not code (generic login + search)

Status: 2026-10-15, phase 1 done (shared helpers in `sourcing/vendors/web.py`), phase 2 done (vendor registry,
`scripts/manage_vendors.py`), phase 3 done (generic login, `sourcing/vendors/generic.py`), phase 4 done (generic search;
`adapter: generic` is selectable); phase 5 done (`scripts/onboard_vendor.py`; live run 11: the generic adapter
matched the Proponent adapter on 11 of 12 parts, the 12th only on a "not supported" variant line);
phase 6 waits for the client's next vendor.

## Goal

**Since 2026-10-16: a new vendor needs only its login secret** (section 0). The record +
onboarding flow below still works and is the way to debug a vendor that doesn't set itself up.

## 0. Vendor from its secret alone (2026-10-16)

```
admin creates sourcing-vendor-<vendor>:                buyer types the vendor name in the sheet
{"url": "<login page>", "username", "password",   ──►  worker: no record + no adapter code
 "totp_secret"?, "allowed_hosts"?}                       → record built from the secret
                                                         (generic adapter, enabled)
                                                       → login, search, results as usual
                                                       → first run's Results flagged NEW_VENDOR_CHECK,
                                                         Chat "New vendors" note
                                                       → after one run that finished a search:
                                                         first_run_done, no more flags
```

- **Login sites** = the secret URL's site and its subdomains (`shop.wencor.com` → `wencor.com`,
  `vendor.co.uk` kept whole) + the secret's optional `allowed_hosts` (e.g. an SSO page).
  Only a Secret Manager admin can write the secret, so the URL is as trusted as the password.
  **The sheet never supplies a URL** (see Decisions): whoever edits it would choose where the
  password is typed. No secret → row NO_CREDENTIALS "not set up"; secret without `url` →
  NEEDS_ATTENTION. Both send a Chat message saying what the admin must add.
- **Not-sold wording is learned:** when a search has no real offer and no known wording
  matches, the LLM is asked for the page's sentence saying the part isn't sold
  (`Extractor.not_sold_quote`, at most 5 per run). The quote must be on the page next to the
  part; it is then saved (`selectors/<vendor>.not_sold_phrases`) and matched by rule after that.
- **First-run flag:** records with `first_run_done: false` (made from a secret, or by
  `manage_vendors.py add`) flag every Results row `NEW_VENDOR_CHECK` until a run finishes a
  search (DONE / NOT_FOUND / NOT_SOLD). A local test run counts as that check; Cloud Run's store
  is separate, so its first live run is flagged once more.
- `onboard_vendor.py <vendor>` works for a secret-only vendor too (record built the same way,
  saved only with `--save`).
- Code: `credentials/base.py` (`allowed_hosts`), `vendors/registry.py` (`site_domain`,
  `record_from_secret`), `pipeline.py` (`_set_up_from_secret`, `_first_run_check`,
  `NEW_VENDOR_FLAG`), `generic.py` (`_ask_not_sold`), `extract.py` (`not_sold_quote`).
  Tests: `tests/test_vendor_from_secret.py`.

The record-first flow (2026-10-15):

```
admin adds vendor record (website, allowed hosts)   ─┐
client creates secret sourcing-vendor-<vendor>      ─┼─► onboarding run (1-2 known parts)
                                                     │      learns login fields, search box,
                                                     │      results address; saves them
                                                     └─► person checks the offers → vendor enabled
                                                            → buyers can use it in the sheet
```

Proponent keeps its own tuned adapter (`ProponentAdapter`). It is also the **test bench** for
the generic one: we have a working login there, so the generic adapter must reach the same
offers on Proponent before we trust it on a vendor we've never seen.

## Decisions (2026-10-15)

- **The login URL is not in the sheet.** Anyone who can edit the sheet could otherwise choose
  where Skylink's password gets typed. Buyers type only the vendor name. The URL lives in the
  secret's `url` (section 0, the normal way since 2026-10-16) or the vendor record
  (`login_url` / `website` + `allowed_hosts`), and a checked-in `vendors.json` is an optional
  seed for a fresh store.
- **The LLM is a fallback only:** saved selectors → generic lists → LLM, for the login link and
  the username / password / submit fields (and the search box, as today).
- **2FA / OTP later.** TOTP from the secret's `totp_secret` keeps working as it does for
  Proponent; SMS / email / push stop as NEEDS_ATTENTION until the handoff (plan step 5) is decided.

## What stays as it is

Pipeline, account grouping, leases, Secret Manager lookup, browser backends (Browserbase /
local), re-login on expiry, extraction + value verification, stock rule, flags, Results
writing, Chat summary. All of this is already vendor-neutral.

---

## 1. Vendor registry (the "db or local" part)

Today a vendor exists only if it is in the code dict `ADAPTERS` (`sourcing/vendors/__init__.py`).
The store already has a `vendors/<id>` record (Firestore on Cloud Run, `.local_store.json`
locally) used for config overrides; it becomes the registry.

`vendors/<vendor_id>` record:

```json
{
  "display_name": "Wencor",
  "aliases": ["wencor", "wencor group"],          // values buyers may type in the Vendor column
  "adapter": "generic",                            // "generic" | "proponent" (code adapter)
  "enabled": false,                                // true only after onboarding is checked
  "website": "https://www.wencor.com/",            // start page (login link is found from here)
  "login_url": "https://shop.wencor.com/login",    // optional, better than website
  "allowed_hosts": ["shop.wencor.com", "login.wencor.com"],   // credentials typed only here
  "part_url_template": "",                         // optional; learned if the site has one
  "currency": "USD",                               // when the page shows only "$"
  "not_sold_phrases": [],
  "action_timeout_seconds": 20, "page_load_timeout_seconds": 90,
  "notes": "who set it up, when"
}
```

- Learned things (fields, search box, results address, signed-in marker) stay in
  `selectors/<vendor_id>`, as Proponent's do today, so the record is what a person set and
  selectors are what the code found.
- Lookup order for a Vendor cell: code adapters (`ADAPTERS`) → registry by id → registry by alias.
  Not found → `FAILED "Vendor 'X' is not set up"`. Found but `enabled: false` →
  `FAILED "Vendor 'X' is not enabled yet"`.
- New script `scripts/manage_vendors.py add | show | list | enable | disable | set KEY=VALUE`
  (same style as `manage_sheets.py`), works on Firestore or the local file.
- `manage_vendors.py export vendors.json` / `import vendors.json` (added 2026-10-16): the records
  as a file that can be checked in, and a seed for a fresh store (Firestore on Cloud Run). Import
  adds only records the store doesn't have, checked like `add`, so it is safe to re-run.
- Code change: `adapter_class()` / `canonical_vendor_id()` are module functions with no store
  access today; they become a small `VendorRegistry(store)` passed in `Deps`, used by
  `Pipeline._process_batch` and `default_adapter_factory`.

## 2. Shared web toolkit (moved out of proponent.py)

Most of Proponent's browser code is already generic. Move it, unchanged, to
`sourcing/vendors/web.py` so both adapters use the same pieces:

`_first_visible`, `_settle`, `accept_cookies` (+ consent lists), `_submit_and_wait`,
`_body_text`, `_LIST_INPUTS_JS`, the word lists (`BLOCKED_HINTS`, `BAD_PASSWORD_HINTS`,
`MFA_TEXT_HINTS`, `EXPIRED_HINTS`), the generic selector lists (username/password/submit/
login-link/logout/MFA/search input), `host_allowed`.

Proponent keeps only its own ids (`#MainContent_*`, `a#loginbutton`, jqGrid id, bulk box).
The existing 167 tests must pass unchanged after this step: it is a move, not a rewrite.

## 3. GenericAdapter (`sourcing/vendors/generic.py`)

The rule for every step: **fixed rules first, then learned selectors, then the LLM only to name
an element from a list the code built. The code does every click and every keystroke.**

### 3a. Login

```
start URL: login_url → saved login page → secret url → website
 ├─ accept_cookies()
 ├─ host check (off-site redirect → try next URL)
 ├─ find fields, per step (handles 1-step and 2-step "email → Next → password" logins, max 3 steps):
 │     saved selectors → generic lists → LLM pick_login_fields(visible inputs + buttons)
 │     LLM gets: tag, type, id, name, placeholder, label, button text. Never values.
 │     LLM returns: {username: i, password: j, submit: k, next: m} (indexes or null)
 │     code checks: password index is type=password, all on an allowed host
 │     no login form at all on the page → find a Login link (generic list → LLM pick)
 ├─ fill username / password (from Secret Manager) and click submit (code)
 └─ outcome after submit, in this order:
       1. BLOCKED words (captcha, locked, access denied) → LoginBlocked, stop    ← always first
       2. code box / MFA words → TOTP if the secret has a seed, else MfaRequired
       3. still on the form + bad-password words → InvalidCredentials
       4. logout link visible / form gone → signed in
       5. unclear → LLM classify_login_page(page text) →
          signed_in | bad_credentials | mfa | blocked | still_login | unknown
          (rules 1-3 can't be overridden by the LLM; "unknown" → LoginFailed)
 save: login_page_url, field selectors, signed-in marker (the logout link that was found)
```

### 3b. Signed-in check (`is_authenticated`)

Open the page reached after login (saved as `home_url`, or the record's `probe_url`):
- password field or Login link visible, or redirected to the login page → signed out
- the saved logout-link selector visible → signed in
- neither → signed out (safe side: the pipeline then logs in, never searches logged out,
  so there are no false NOT_FOUND results)

### 3c. Search one part

```
1. results address known (record part_url_template or learned)? → open it directly
2. else find the search box: saved → generic list → LLM pick_search_input (exists today)
   fill part, click the search button (generic list → LLM pick) or press Enter
   landing URL contains the part number → save it as part_url_template (exists: part_url_template())
3. signed out on the way → SessionExpired (pipeline re-logs once)
```

### 3d. Read the results

```
jqGrid on the page? → grid.py records (exists)
else → main content text (tables kept as rows: "cell | cell | cell")
  → Extractor.extract_offers (exists: schema + every value checked against the page)
  → "login_page" → SessionExpired; not_sold_phrases → NOT_SOLD
```
Version 1 reads the results page only. Clicking into each result's detail page, paging through
many result pages and bulk search stay vendor-specific (an adapter can add them later).

### 3e. Logout
Saved logout link → generic `LOGOUT_SELECTORS` → confirm signed out. Same as Proponent.

## 4. LLM calls: limits and safety

| Rule | How |
|---|---|
| Password never reaches the LLM | Only element metadata is sent; input values are never read into prompts |
| LLM never acts | It returns indexes / a page label; code validates and does the action |
| Credentials only on the vendor's sites | `allowed_hosts` checked right before typing (exists today) |
| CAPTCHA / lockout stops the run | Word check runs before any LLM call |
| Page text can't steer the run | Prompts say page content is data; answers are only indexes/labels from a fixed schema |
| Cost stays small | Each discovery answer is saved to `selectors/<vendor>`; max 5 discovery calls per account per run; normal runs make 0 |

New in `sourcing/extract.py`: `pick_login_fields`, `pick_element` (login link / search button /
search box), `classify_login_page`; each with a JSON schema, next to the existing `pick_search_input`.

## 5. Onboarding a vendor (`scripts/onboard_vendor.py`)

```bash
python scripts/manage_vendors.py add wencor --website https://... --hosts shop.wencor.com
python scripts/check_access.py --vendor wencor          # secret readable?
python scripts/onboard_vendor.py wencor --part <known part> [--part <another>] [--browser local] [--save]
```

The onboarding run logs in through the generic path, searches 1-2 parts the buyer knows, logs out, and prints:
- each step and which method found each field (saved / generic list / LLM)
- the learned selectors and results address
- the offers per part
It saves the selectors only with `--save`. It stops on any CAPTCHA, block or 2FA it can't answer.
It ends with a **TO CHECK** list (added 2026-10-16 after run 11): a part "found" with only empty
lines (the vendor's not-sold wording is probably missing from `not_sold_phrases`), in-stock lines
without a condition (`default_condition`, once the client confirms), elements the LLM found, and
no direct results address learned.
A person compares the offers with the portal, then runs `manage_vendors.py enable wencor`.
The run is saved under `test_runs/` like every real run.

## 6. Build order

| Phase | Work | Done when |
|---|---|---|
| 0 | Client: list of next vendors, their login URLs, secrets created | at least one vendor + secret |
| 1 ✅ | Move shared helpers to `vendors/web.py` | done 2026-10-15: 168 tests green, lists identical, 2-part live run gave the same offers as run 09 |
| 2 ✅ | Vendor registry in the store + `VendorRegistry` + `manage_vendors.py` | done 2026-10-15: rows resolve by id/alias, disabled/unsupported fail with the reason, 12 tests |
| 3 ✅ | GenericAdapter login + LLM field picker + page classifier | done 2026-10-15: 16 mock-page tests (1-step, 2-step, login link by rule and by LLM, TOTP, SMS, bad password, CAPTCHA, unknown fields, refused LLM answers, off-site URLs, newsletter box, call cap, AuthManager path) |
| 4 ✅ | Generic search + results reading | done 2026-10-15: 12 mock tests (search box by rule / by LLM, learned and record address, bad address fallback, button when Enter does nothing, results drawn in place, no results, not sold, signed out, refused LLM picks) + 1 whole pipeline run of a record-only vendor |
| 5 ✅ | `onboard_vendor.py` ✅ (2026-10-15: `sourcing/onboarding.py` + script, reports how each element was found, `--fresh` / `--secret` / `--save`, 5 mock tests); live run ✅ test run 11: fresh onboarding found every element without the LLM; 12-part sheet gave the same statuses and offers as the Proponent adapter on 11/12 parts (12th: a "not supported" variant line, generic is the more accurate). Fixed on the way: details expanded before reading (condition), grid records saved with `--dump-pages` | same offers as the ProponentAdapter run 09 |
| 6 | First real new vendor through onboarding | offers checked by a person, vendor enabled |

Phases 1–4 need no portal access. Phase 5 needs only the Proponent login we already use.

## 7. What the generic adapter will not handle (needs a person or vendor code)

- Passkeys (plan step 7), SMS / email / push 2FA (needs the Live View handoff, plan step 5)
- CAPTCHA / bot checks: the run stops
- Single sign-on through another company's site (Microsoft, Okta): works only if those hosts
  are added to `allowed_hosts` by an admin. Never automatic.
- Login forms inside iframes / shadow DOM: v1 looks at the main page only; add frame support
  if a real vendor needs it
- Search flows with filters, wizards or one product page per result; bulk search; many result
  pages: vendor-specific adapter code, the same way Proponent has them

## Open questions

1. Which vendors come next, and do they use username + password, TOTP, or SSO?
2. Who may enable a vendor after onboarding (developer only, or a Skylink admin too)?
