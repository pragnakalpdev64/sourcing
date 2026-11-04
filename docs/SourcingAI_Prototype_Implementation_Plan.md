# SourcingAI: Prototype Implementation Plan

**Purpose:** a prototype that searches vendor portals for part numbers added to a Google Sheet, and writes price, quantity, condition, lead time and currency back to a results sheet for human review (later imported into AvSight).

**Vendors in scope:** Proponent (first), Wencor, DASI.

---

## 1. Where everything runs

| Component | Where it runs | What it does |
|---|---|---|
| **RFQ input sheet** | Google Sheets | Users add rows: Vendor, Part Number, Qty, Condition |
| **Sheet trigger** | Google Apps Script (attached to the sheet) | Detects new rows and calls the trigger endpoint |
| **Trigger / HITL API** | **Cloud Run service** | `/trigger` starts a search job; `/hitl/view` and `/hitl/done` handle human 2FA |
| **Search worker** | **Cloud Run Job** | Reads NEW rows, logs into vendors, searches parts, writes results |
| **Safety-net sweep** | **Cloud Scheduler** (every 10–15 min) | Starts the job if any NEW rows were missed |
| **Morning warm-up** | **Cloud Scheduler** (weekdays, before working hours) | Checks every vendor login so any 2FA happens early |
| **State store** | **Firestore** | Job state, row state, vendor login state, learned selectors |
| **Credentials** | **Secret Manager / Bitwarden** | Vendor usernames, passwords, TOTP seeds |
| **Browser** | **Browserbase** (remote) | Vendor portal sessions, saved Contexts, Live View for 2FA |
| **LLM** | LLM API | Reads vendor pages and extracts fields (only where needed) |
| **Results** | Google Sheets (results tab) | Output for human review → AvSight import |
| **Code + deploy** | Git repo → **Cloud Build** → Cloud Run | Build and deploy on merge |

---

## 2. Architecture

```
User adds rows to the sheet (Status = NEW)
        │
        ▼
Apps Script trigger (on change) ──POST──►  Cloud Run service  /trigger
                                                │
Cloud Scheduler sweep (every 10–15 min) ───────►│  (starts the job only if
                                                │   one isn't already running)
                                                ▼
                                   Cloud Run Job: search worker
                                     1. Read NEW rows → mark RUNNING
                                     2. Group by vendor (vendors in parallel)
                                     3. Vendor Auth Manager → logged-in session
                                     4. Search each part → extract fields
                                     5. Write results + status per row
                                                │
            ┌───────────────┬───────────────────┼──────────────────┬───────────────┐
            ▼               ▼                   ▼                  ▼               ▼
      Browserbase     Secret Manager /      Firestore           LLM API      Google Sheet
      (sessions,      Bitwarden             (state)             (extract)    (results tab)
      Contexts,       (creds + TOTP)
      Live View)
```

---

## 3. Running the search when a new row is added

**Primary: Apps Script trigger (event-driven)**
- An **installable `onChange` trigger** on the sheet runs when rows are added or pasted.
- The script finds rows with `Status` empty or `NEW`, sets them to `NEW`, and calls the Cloud Run `/trigger` endpoint. The request is authenticated with a shared secret kept in Script Properties, or an identity token.
- `/trigger` starts the Cloud Run Job (Cloud Run Admin API `jobs.run`) **only if no job is already running** (a lock in Firestore).
- Triggers that fire in quick succession are fine: the job always processes **all** NEW rows, so one run covers several additions.

**Backup: Cloud Scheduler sweep**
- Every 10–15 minutes, Scheduler calls `/trigger`. If any NEW rows exist and no job is running, it starts one.
- This catches rows that Apps Script doesn't see, such as rows written by another program through the Sheets API, which doesn't fire sheet triggers. It also catches triggers that failed.

**No duplicate work**
- Rows move `NEW → RUNNING → DONE / NOT_FOUND / WAITING_AUTH / NEEDS_ATTENTION / FAILED`.
- The worker only picks up `NEW` rows and records its `Run ID` on each one, so a row is never searched twice.

```javascript
// Apps Script (sketch)
function onSheetChange(e) {
  const sh = SpreadsheetApp.getActive().getSheetByName('RFQ Input');
  const rows = sh.getDataRange().getValues();
  let hasNew = false;
  for (let i = 1; i < rows.length; i++) {
    const [rfq, vendor, part, , , status] = rows[i];
    if (part && !status) { sh.getRange(i + 1, 6).setValue('NEW'); hasNew = true; }
  }
  if (hasNew) {
    UrlFetchApp.fetch(PropertiesService.getScriptProperties().getProperty('TRIGGER_URL'), {
      method: 'post',
      headers: { 'X-Trigger-Secret': PropertiesService.getScriptProperties().getProperty('TRIGGER_SECRET') },
      muteHttpExceptions: true,
    });
  }
}
```

---

## 4. Sheet layout

**Input tab: `RFQ Input`**

| RFQ ID | Vendor | Part Number | Qty Needed | Condition Wanted | Status | Run ID | Notes |
|---|---|---|---|---|---|---|---|

- Vendor blank → search all three vendors.

**Results tab: `Results`**

| RFQ ID | Vendor | Part Number | Price | Currency | Qty Available | Condition | Lead Time | Source URL | Fetched At | Flag | Approved |
|---|---|---|---|---|---|---|---|---|---|---|---|

The columns will be changed to match the AvSight import format once we have it.

---

## 5. Search worker (Cloud Run Job)

```
for each vendor in NEW rows (in parallel):
    take vendor lease (Firestore)                # one session per vendor account
    session = auth_manager.get_session(vendor)   # see section 6
    for each row of that vendor:
        try:
            search part → extract fields (strict schema)
            row.status = DONE / NOT_FOUND
        except SessionExpired:
            re-authenticate once → retry the row
        except AwaitingHuman:
            remaining rows → WAITING_AUTH; stop this vendor
        save row result + status to the sheet
    release session (Context saved) and lease
```

- **Extraction:** the LLM extracts price, currency, qty available, condition and lead time into a fixed schema.
- **Selector learning:** selectors that work are saved in Firestore, so later runs need fewer LLM calls.
- **Large RFQs:** each vendor runs as its own task. Row statuses make the job resumable if it is cut off.

---

## 6. 2FA and expired-session handling

### 6.1 Vendor Auth Manager
Login is handled by its own component. The search code only asks it for a logged-in session.

Per-vendor state in Firestore: `ACTIVE`, `NEEDS_LOGIN`, `AUTO_LOGIN`, `AWAITING_HUMAN`, `NEEDS_ATTENTION`, `SUSPENDED`.

### 6.2 Login steps (in strict order)

| Step | Action | When it is used |
|---|---|---|
| 0. Reuse the Context | Browserbase session with the saved Context (`persist: true`), then an auth probe | Most runs |
| 1. Automatic login | Username and password from the vault, filled by code. TOTP generated from the vault seed (`pyotp`). | Password-only or authenticator-app vendors |
| 2. Human handoff | Notify the vendor owner with a secure Live View link. The person enters the SMS, email or push code. The Context is saved. | 2FA the code can't complete |
| 3. Stop safely | Rows become `NEEDS_ATTENTION`, the owner is alerted, no automatic retries | Wrong password, lockout, bot block |

- At most **2 automatic login attempts**, then `SUSPENDED`, to avoid locking the account.
- **One session per Context at a time** (Firestore lease).

### 6.3 Expired-session detection
- An **auth probe** for each vendor: open a page that requires login, then check for 401/403, a redirect to the login page, or a missing logged-in marker.
- **Watchers during the run:** 401/403 responses, navigation to the login URL, and "session expired" text.
- On expiry: re-authenticate once, then retry the row.
- A part is marked `NOT_FOUND` **only if the auth probe passes.**
- **Morning warm-up** (Cloud Scheduler) probes all vendors before working hours.

### 6.4 Human 2FA handoff
1. The worker opens a Browserbase session with **keep-alive + Context persist**, and code fills in the username and password.
2. It gets the Live View URL (`sessions.debug`), sets state `AWAITING_HUMAN`, and the rows become `WAITING_AUTH`.
3. A notification is sent with a link to the **authenticated `/hitl/view` page** on Cloud Run, never the raw Live View URL.
4. The worker exits, so nothing is billed while it waits.
5. The person enters the code they received, then clicks **Done** (`/hitl/done`). A Cloud Tasks poller also runs the auth probe every 15–20 seconds.
6. On success: `REQUEST_RELEASE` (this saves the Context), set state `ACTIVE`, set the `WAITING_AUTH` rows back to `NEW`, and start the job.
7. If there's no action within 15 minutes: release the session, mark rows `NEEDS_ATTENTION`, and notify the backup person.

**Security:**
- The password is never shown to the person.
- The link is short-lived and sent only to a named person.
- Login sessions are not recorded.
- Contexts are treated as secrets.
- An audit log records who completed each login.

---

## 7. Code layout

```
sourcing-proto/
├─ sourcing/                 # core package
│  ├─ auth/
│  │  ├─ manager.py          # login steps: Context → TOTP → human → stop
│  │  ├─ state.py            # vendor login state (Firestore)
│  │  └─ hitl.py             # Live View link, Done/poll, release + save
│  ├─ browser/browserbase.py # sessions, Contexts, keep-alive, connect
│  ├─ vendors/
│  │  ├─ base.py             # VendorAdapter: login, is_authenticated, search, extract
│  │  ├─ proponent.py
│  │  ├─ wencor.py
│  │  └─ dasi.py
│  ├─ secrets.py             # Secret Manager / Bitwarden
│  ├─ sheets.py              # read NEW rows, write results, statuses
│  ├─ extract.py             # LLM extraction, strict schema
│  └─ pipeline.py            # group by vendor → lease → search → retry
├─ service/
│  ├─ job_main.py            # Cloud Run Job entrypoint
│  └─ api.py                 # Cloud Run service: /trigger, /hitl/view, /hitl/done
├─ apps_script/
│  └─ trigger.gs             # sheet onChange trigger
├─ Dockerfile
├─ cloudbuild.yaml
└─ tests/
```

---

## 8. Implementation phases

| Phase | Work | Done when |
|---|---|---|
| **0. Setup** | GCP project, APIs, service account, test sheet, Browserbase account, repo skeleton, Cloud Build | A Cloud Run Job reads and writes the sheet |
| **1. Pipeline + Proponent** | Vendor adapter, Browserbase + Context, auth probe, auto-login with TOTP, extraction, row statuses | Proponent rows are processed from start to finish on Cloud Run |
| **2. Sheet trigger** | Apps Script `onChange` → `/trigger`, job lock, Scheduler sweep | Adding a row starts a search automatically, with no duplicates |
| **3. Session handling** | Expiry watchers, re-auth + retry, vendor lease, login limits, morning warm-up | Deleting cookies mid-run recovers, with no false NOT_FOUND |
| **4. Human 2FA handoff** | Keep-alive + Live View, `/hitl/view`, `/hitl/done`, poller, timeout, notification | A second person completes 2FA; the next run skips 2FA |
| **5. Wencor + DASI** | Adapters and auth probes; note each vendor's 2FA type, session lifetime and blockers | All three vendors tested |

---

## 9. Open questions

1. A sample of the input sheet: are the columns as shown above, and is the vendor always filled in?
2. The exact AvSight import format (columns, file type), with a sample file.
3. The 2FA method for each vendor account (authenticator app, SMS, email, push). Can it be switched to an authenticator app (TOTP)?
4. Who receives the 2FA code for each vendor, and who should get the handoff notification?
5. Which credential store is approved for automation (Secret Manager / Bitwarden)?
6. Do the vendors' terms allow automated logins? Does any vendor offer an API or price file?
7. Which condition codes to capture, and how to handle several results for one part?
8. Which notification channel to use for 2FA handoffs (email, Slack, chat)?
