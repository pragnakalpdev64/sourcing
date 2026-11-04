# SourcingAI: Cloud Run Prototype Plan

**Goal:** move the working Hermes/MCP prototype onto Google Cloud (Cloud Run) and test the full flow end to end with **one vendor (Proponent)**: sheet entry → automatic search → login (incl. 2FA / expired session / human handoff) → results.

**Password manager for the prototype:** Bitwarden Secrets Manager (kept behind an interface so it can be swapped for GCP Secret Manager later).

---

## 1. Client feedback and what it changes

| Client response | Impact on the prototype |
|---|---|
| Test with **one vendor** for now; later around **10 vendors** with different login systems and **passkeys** | Build and test with Proponent only. Every vendor is its own **adapter**, so new login types plug in later. Passkeys need a separate spike (section 6.6). |
| Some vendors have **multiple users**, and each team member may have **their own credentials** (varies by vendor) | Credentials, browser Contexts and sessions are keyed by **(vendor, user)**, not just vendor. |
| **Start with a Google Sheet**, but the goal is **direct entry into AvSight** | Output goes through an **output interface**: `SheetSink` now, `AvSightSink` later, with no change to the search code. |
| **GCP Secret Manager** preferred long term; **passwords and passkeys change**, so updates must be handled (same issue as InputAI) | Credentials go through a `CredentialProvider` interface. Add a **credential update flow**: wrong password → ask that user to update it through a secure page. |
| Handoff alerts in **Google Chat**, sent to the **team member using the system**, not one point person | Every request row records **who requested it**. The 2FA alert goes to **that person** in Google Chat. |
| **Browserbase is OK** if it's the best tool | Browserbase paid plan (needed for keep-alive + Live View handoff). |
| ≤10 vendors overall | A small vendor registry is enough; no generic "any website" engine needed. |

---

## 2. Prototype scope

**In scope**
- Google Sheet input with a **Requested By** column → automatic trigger on new rows
- Cloud Run Job worker + small Cloud Run API service
- Proponent adapter (reusing the current prototype's login and search code)
- Per-user credentials from Bitwarden, including TOTP seed
- Browserbase sessions with **saved Contexts per (vendor, user)**
- Expired-session detection + one re-login + retry
- Human handoff to the **requester** via a **Google Chat** message with a secure Live View link
- Credential update flow (wrong password → requester updates it)
- Results written to a results sheet (`SheetSink`)
- Cost and time logging per part (Claude)

**Out of scope (later)**
- Other vendors, passkey vendors (only the spike in 6.6)
- Direct AvSight entry (`AvSightSink`)
- GCP Secret Manager migration
- Integration with the existing Chat door (`gateway/`) and the `mie-agent` repo

---

## 3. Where everything runs

| Component | Runs on | Purpose |
|---|---|---|
| Input sheet (`Requests` tab) | Google Sheets | Team members add: Vendor, Part Number, Qty, Condition, **Requested By** |
| Sheet trigger | Apps Script (installable `onChange`) | Marks new rows `NEW`, fills in **Requested By**, calls `/trigger` |
| API service (`sourcing-api`) | **Cloud Run service** | `/trigger`, `/hitl/view`, `/hitl/done`, `/credentials/update` |
| Worker (`sourcing-worker`) | **Cloud Run Job** | Processes `NEW` rows |
| Sweep + morning warm-up | **Cloud Scheduler** | Catches missed rows; checks logins before working hours |
| State | **Firestore** | Jobs, rows, account auth state, learned selectors, audit |
| Credentials | **Bitwarden Secrets Manager** (prototype) | Username, password and TOTP seed per (vendor, user) |
| Infra secrets | GCP Secret Manager | Bitwarden token, Browserbase key, Claude key, Chat webhook, trigger secret |
| Browser | **Browserbase** | Remote sessions, Contexts, Live View, keep-alive |
| LLM | Claude API | Page reading + field extraction |
| Notifications | **Google Chat** | 2FA handoff, credential update request, job summary |
| Output | Google Sheets (`Results` tab) | Review → AvSight import (direct entry later) |
| Build / deploy | Cloud Build → Artifact Registry → Cloud Run | One container image for both service and job |

```
Team member adds row (Requested By = their email)
        │
        ▼
Apps Script onChange ──► sourcing-api /trigger ──► starts Cloud Run Job (if not running)
Cloud Scheduler sweep ──►        ▲
                                 │
                       sourcing-worker (Cloud Run Job)
                         group NEW rows by (vendor, requester)
                         → Auth Manager → search → extract → sink
                                 │
    ┌───────────────┬────────────┼──────────────┬──────────────┬──────────────┐
    ▼               ▼            ▼              ▼              ▼              ▼
 Bitwarden SM   Browserbase   Firestore      Claude API    Google Chat    Results sheet
 (creds+TOTP)   (Contexts,    (state)        (extract)     (alerts to     (SheetSink)
                Live View)                                   requester)
```

---

## 4. Data model

### 4.1 Vendor account = (vendor, user)
A team member may have their own login for a vendor. Some vendors may only have a shared login.

**Credential lookup order for a request:**
1. The requester's own account for that vendor
2. Otherwise, the vendor's **shared** account (if one exists)
3. Otherwise, the row becomes `NO_CREDENTIALS` and the requester is told in Chat

### 4.2 Bitwarden secret naming
One secret per account, in project `sourcing-vendors`:

| Secret key | Value (JSON) |
|---|---|
| `proponent/jane@company.com` | `{"username": "...", "password": "...", "totp_secret": "BASE32..."}` |
| `proponent/_shared` | same shape |

The current code looks secrets up by **company name only**. Change it to `(vendor_id, user_email)` with `_shared` as the fallback.

### 4.3 Firestore collections

| Collection | Key | Main fields |
|---|---|---|
| `vendors` | `proponent` | `display_name`, `login_url`, `probe_url`, `mfa_method`, `adapter`, `enabled` |
| `accounts` | `proponent__jane@company.com` | `vendor_id`, `user_email` / `_shared`, `secret_key`, `context_id`, `state`, `last_validated_at`, `last_login_at`, `observed_session_ttl`, `consecutive_login_failures`, `lease_holder`, `lease_expires_at`, `hitl_session_id`, `hitl_connect_url`, `hitl_expires_at` |
| `jobs` | `run_id` | `started_at`, `finished_at`, `status`, `rows_total`, `rows_done`, `cost` |
| `rows` | `sheet_row_id` | `rfq_id`, `vendor_id`, `part_number`, `requested_by`, `account_id`, `status`, `run_id`, `result`, `error` |
| `selectors` | `proponent` | Learned selectors (replaces `learned_sites.json`) |
| `audit` | auto | Who completed a login, credential updates, job runs |

**Account states:** `ACTIVE` · `NEEDS_LOGIN` · `AWAITING_HUMAN` · `NEEDS_CREDENTIAL_UPDATE` · `NEEDS_ATTENTION` · `SUSPENDED`

**Row statuses:** `NEW` → `RUNNING` → `DONE` / `NOT_FOUND` / `WAITING_AUTH` / `NO_CREDENTIALS` / `NEEDS_ATTENTION` / `FAILED`

---

## 5. Sheet layout

**`Requests` tab**

| RFQ ID | Vendor | Part Number | Qty Needed | Condition Wanted | Requested By | Status | Run ID | Notes |
|---|---|---|---|---|---|---|---|---|

- **Requested By** is filled in automatically by Apps Script (`Session.getActiveUser().getEmail()`, which works for users in the same Google Workspace domain). If it comes back empty, the user types their email.

**`Results` tab**

| RFQ ID | Vendor | Part Number | Price | Currency | Qty Available | Condition | Lead Time | Source URL | Fetched At | Requested By | Flag | Approved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

The columns will be aligned to the AvSight import format once we receive it.

---

## 6. Login, 2FA and session handling

### 6.1 Login ladder (per account)
| Step | Action |
|---|---|
| 0 | Reuse the saved Browserbase Context for **(vendor, requester)** → auth probe |
| 1 | Automatic login: username and password from Bitwarden, TOTP via `pyotp` |
| 2 | Human handoff **to the requester** in Google Chat (Live View link) |
| 3 | Stop safely: wrong password → `NEEDS_CREDENTIAL_UPDATE`; lockout or block → `SUSPENDED` |

- At most 2 automatic login attempts per account, then stop.
- One lease per account (one session per Context at a time). Different accounts and vendors run in parallel.

### 6.2 Expired session
- An auth probe for each vendor adapter (401/403, redirect to the login page, missing logged-in marker).
- Watchers during the run (responses, navigation, "session expired" text).
- On expiry: re-login once, then retry the row. `NOT_FOUND` only if the auth probe passes.
- Morning warm-up: probe all active accounts.

### 6.3 Human handoff to the requester (Google Chat)
1. The worker opens a Browserbase session (`keep_alive=True`, Context `persist=True`), and code fills in username and password.
2. Gets the Live View URL (`sessions.debug`). Account → `AWAITING_HUMAN`, rows → `WAITING_AUTH`. The worker exits.
3. A Google Chat card goes to the **requester**:
   > 🔐 **Proponent needs your 2FA code** (8 parts waiting)
   > [Open secure login] [I'm done]
4. **Open secure login** opens `sourcing-api /hitl/view`, which checks the viewer's Google identity matches the requester, then embeds the Live View.
5. **I'm done** opens `/hitl/done`, which reconnects, runs the auth probe, releases the session (the Context is saved), sets the account `ACTIVE`, sets the rows back to `NEW`, and starts the job.
6. If nothing happens within 15 minutes: release the session, mark rows `NEEDS_ATTENTION`, and send a reminder to the requester.

**Google Chat delivery for the prototype:** an **incoming webhook** to a test space, with the requester **@mentioned**. Webhooks support card buttons that open links.
**Production:** a Chat app (or the existing Chat door) sending a **direct message** to the requester.

### 6.4 Credential update flow (passwords change)
1. Login fails with "invalid password", so the account becomes `NEEDS_CREDENTIAL_UPDATE` and the rows `NO_CREDENTIALS`.
2. A Google Chat message goes to the account owner: *"Your Proponent password seems to have changed. [Update it securely]"*
3. `/credentials/update` (Google sign-in, owner only) shows a form, and the **new value is written straight to Bitwarden** through the SDK (the machine account needs write access to this project).
4. The account goes back to `NEEDS_LOGIN`, and the rows are requeued.

Passwords never appear in chat, the sheet or logs. This is the same flow that will later write to GCP Secret Manager.

### 6.5 Security rules
- The password is filled by code and never shown to the user.
- The Live View link only works through `/hitl/view` for the right person, and expires after 15 minutes.
- Login sessions are not recorded.
- Contexts are treated as secrets.
- Every login, handoff and credential update is written to `audit`.

### 6.6 Passkey spike (research only)
Some vendors use passkeys. A passkey on a team member's phone **can't be used from a remote browser** (cross-device passkey sign-in needs the phone to be physically near that browser). Options to test:
1. The vendor offers a password + OTP fallback → use the normal ladder.
2. Register a **dedicated passkey for automation** with Chrome's virtual WebAuthn authenticator (CDP `WebAuthn` domain) during a one-time handoff, and store its private key as a secret.
3. Rely on long-lived saved Contexts, with a manual login when the Context expires.

The outcome is a short write-up of which option works for which vendor. No production code.

---

## 7. Output interface

```python
class ResultSink(Protocol):
    async def write(self, rows: list[ResultRow]) -> list[WriteOutcome]: ...

class SheetSink(ResultSink): ...      # prototype: Results tab
class AvSightSink(ResultSink): ...    # later: Salesforce API into the AvSight object(s)
```

The worker only calls `sink.write()`. Switching to direct AvSight entry later is a config change plus the new sink. Your existing Salesforce code (`scripts/salesforce_products.py`) is the starting point for `AvSightSink`.

---

## 8. Reusing the current code

| Current file | New location | Change |
|---|---|---|
| `src/browser_backend.py` | `sourcing/browser/` + `sourcing/auth/` | Browserbase only. Split login logic from session management. Add Contexts, keep-alive, Live View. Selectors in Firestore. |
| `src/bitwarden_credentials.py` | `sourcing/credentials/bitwarden.py` | Key by `(vendor, user)` with `_shared` fallback. Add `update_secret()`. |
| `src/credential_provider.py` | `sourcing/credentials/base.py` | `CredentialProvider` interface (Bitwarden now, GCP later) |
| `src/mcp_server.py` (`search_part`, `_try_auto_totp`) | `sourcing/vendors/proponent.py`, `sourcing/auth/manager.py` | Add qty and condition. Auth probe. Always try TOTP if a seed exists. |
| `src/batch_runner.py` | `sourcing/pipeline.py` | Group by (vendor, requester), leases, row statuses, retries |
| `src/excel_jobs.py` | `sourcing/sheets.py` | Google Sheets API instead of Excel |
| `src/config.py` | `sourcing/config.py` | Env + Secret Manager. Vendor registry in Firestore. |
| `scripts/salesforce_products.py` | `sourcing/sinks/avsight.py` (later) | Basis for direct AvSight entry |
| Hermes, MCP layer | Removed | Replaced by the sheet trigger + Cloud Run |

---

## 9. Repository layout

```
sourcing-proto/
├─ sourcing/
│  ├─ config.py
│  ├─ models.py               # Row, Account, ResultRow, states
│  ├─ store.py                # Firestore access (rows, accounts, leases, audit)
│  ├─ sheets.py               # read NEW rows, update status, write results
│  ├─ pipeline.py             # group by (vendor, requester) → lease → search → retry
│  ├─ extract.py              # Claude extraction, strict schema, cost tracking
│  ├─ notify/chat.py          # Google Chat cards (webhook now, Chat app later)
│  ├─ credentials/
│  │  ├─ base.py              # CredentialProvider interface
│  │  └─ bitwarden.py         # get / update per (vendor, user)
│  ├─ browser/browserbase.py  # sessions, Contexts, keep-alive, Live View, release
│  ├─ auth/
│  │  ├─ manager.py           # login ladder, account state machine
│  │  └─ hitl.py              # start handoff, complete, timeout
│  ├─ vendors/
│  │  ├─ base.py              # VendorAdapter: login, is_authenticated, search, extract
│  │  └─ proponent.py
│  └─ sinks/
│     ├─ base.py              # ResultSink interface
│     └─ sheet.py             # SheetSink
├─ service/
│  ├─ api.py                  # FastAPI: /trigger, /hitl/view, /hitl/done, /credentials/update
│  └─ job_main.py             # Cloud Run Job entrypoint
├─ apps_script/trigger.gs
├─ Dockerfile
├─ cloudbuild.yaml
├─ .env.example
└─ tests/
```

---

## 10. Build steps

### Step 0: GCP and accounts setup (day 1)
- [ ] GCP project + billing + **budget alert**
- [ ] Enable APIs: Cloud Run, Cloud Build, Artifact Registry, Firestore, Cloud Scheduler, Secret Manager, Sheets, Drive, Google Chat
- [ ] Service accounts: `sourcing-runtime` (job + service), `sourcing-build`
- [ ] Firestore (Native mode)
- [ ] Infra secrets into GCP Secret Manager: `BW_ACCESS_TOKEN`, `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `ANTHROPIC_API_KEY`, `CHAT_WEBHOOK_URL`, `TRIGGER_SECRET`
- [ ] Bitwarden project `sourcing-vendors`; secret `proponent/<your email>` in the new JSON shape. Machine account with read + write.
- [ ] Browserbase paid plan; confirm Contexts + keep-alive work
- [ ] Test Google Sheet (`Requests`, `Results`) shared with `sourcing-runtime`
- [ ] Google Chat test space + incoming webhook

**Done when:** all keys can be read from Cloud Run with a test script.

### Step 1: Skeleton on Cloud Run (days 2–3)
- [ ] Repo layout, Dockerfile (Python + Playwright library; no local browser needed, it connects to Browserbase over CDP)
- [ ] `cloudbuild.yaml` → build image → deploy `sourcing-api` (service) + `sourcing-worker` (job)
- [ ] `sheets.py`: read `NEW` rows, set `RUNNING`, write a dummy result

**Done when:** running the job manually turns `NEW` rows into dummy results in the sheet.

### Step 2: Proponent search on Cloud Run (days 4–6)
- [ ] `credentials/bitwarden.py` with `(vendor, user)` lookup + `_shared` fallback
- [ ] `browser/browserbase.py`: create session with Context, connect Playwright, release
- [ ] `vendors/proponent.py`: login, `is_authenticated`, search, extract (price, currency, qty, condition, lead time)
- [ ] `auth/manager.py` steps 0–1 (Context reuse, auto-login + TOTP)
- [ ] `sinks/sheet.py` + per-row statuses + cost/time logging

**Done when:** a real Proponent part goes from `NEW` to `DONE` with real data, and the second run reuses the Context (no login).

### Step 3: Automatic trigger (day 7)
- [ ] Apps Script `onChange`: set `NEW`, fill **Requested By**, POST `/trigger` with the secret
- [ ] `/trigger`: Firestore job lock → run the job via the Cloud Run Admin API
- [ ] Cloud Scheduler sweep every 10–15 min

**Done when:** adding a row starts a search within a minute; adding 5 rows quickly gives one run and no duplicates.

### Step 4: Session robustness (days 8–9)
- [ ] Auth probe + expiry watchers + re-login once + retry
- [ ] Account lease, login failure limit, `SUSPENDED`
- [ ] Morning warm-up (Scheduler)
- [ ] Measure Proponent's real session lifetime

**Done when:** clearing the Context or cookies mid-run recovers automatically, with no false `NOT_FOUND`.

### Step 5: Human handoff via Google Chat (days 10–12)
- [ ] `auth/hitl.py`: keep-alive session, Live View URL, state `AWAITING_HUMAN`
- [ ] `notify/chat.py`: card to the requester (webhook + @mention)
- [ ] `/hitl/view` (Google sign-in, requester check, embed Live View)
- [ ] `/hitl/done` (probe → release → Context saved → requeue → start job)
- [ ] 15-minute timeout handled by the sweep

**Done when:** a team member other than the developer completes a forced handoff from Google Chat, and the next run skips login.

### Step 6: Credential update flow (days 13–14)
- [ ] Detect "invalid password" → `NEEDS_CREDENTIAL_UPDATE`
- [ ] Chat message to the owner → `/credentials/update` form → Bitwarden `update_secret()`
- [ ] Requeue rows after the update

**Done when:** changing the Proponent password in Bitwarden to a wrong value, then fixing it through the form, recovers without developer help.

### Step 7: Passkey spike + report (days 15–16)
- [ ] Try the options in 6.6 on a test site or a vendor that uses passkeys
- [ ] Write up: cost and time per part, Context reuse rate, session lifetime, handoff experience, problems found

---

## 11. Test checklist

| # | Test | Expected |
|---|---|---|
| 1 | Add 1 row | Searched automatically, result in the sheet |
| 2 | Add 10 rows quickly | One job run, no duplicates |
| 3 | Second run next day | Context reused, no login, no 2FA |
| 4 | Delete Context or cookies mid-run | Re-login once, rows still `DONE` |
| 5 | Requester has no Proponent account | `_shared` used, or `NO_CREDENTIALS` + Chat message |
| 6 | Forced 2FA handoff | Chat card to the **requester only**; login completes; job resumes |
| 7 | Handoff ignored | 15-minute timeout → `NEEDS_ATTENTION` + reminder |
| 8 | Wrong password | No more than 2 attempts; credential update request; recovery after update |
| 9 | Two users, same vendor, same time | Separate Contexts and leases; both succeed |
| 10 | Part that doesn't exist | `NOT_FOUND` only after the auth probe passes |

---

## 12. Still to confirm with the client

1. **Vendor list:** the ~10 vendors with their login type (password, TOTP, SMS, email, passkey) and whether each has per-user or shared accounts. Boeing has been added to the list. Check whether Boeing offers an API.
2. **No personal account:** if the requester has no login for a vendor, can we use a shared account, or should the row be skipped?
3. **Passkey vendors:** which vendors require passkeys, and do they offer a password + OTP fallback?
4. **AvSight direct entry:** which AvSight object(s) should pricing go into, plus a sample record, and Salesforce API access (for `AvSightSink`).
5. **Google Chat:** can we create a Chat app in their Google Workspace for direct messages, or should we use the existing Chat door (`gateway/`)?
6. **Credential updates:** who may update a shared account's password, and should updates also be done in LastPass?
7. **Second test vendor:** Wencor, Boeing or DASI credentials once the Proponent flow passes.
