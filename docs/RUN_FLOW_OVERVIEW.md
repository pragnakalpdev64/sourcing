# SourcingAI: what happens in one run

This page follows a single run from the moment it starts to the moment results appear in the
sheet. Each step names the file that does the work.

Start command: `python -m service.job_main`. On Cloud Run, Cloud Scheduler starts this job
every 15 minutes.

---

## The flow at a glance

```
 ┌──────────────────────────────────────────────┐
 │ 1. START                                     │  service/job_main.py
 │    Read settings, make sure only one run     │  sourcing/config.py
 │    happens at a time                         │  sourcing/store.py
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 2. PREPARE TOOLS                             │
 │    • Vendor login store (GCP Secret Manager) │  sourcing/credentials/gsm.py
 │    • Browser (Browserbase or local Chrome)   │  sourcing/browser/
 │    • AI model (Claude)                       │  sourcing/llm/
 │    • Google Chat messages                    │  sourcing/notify/chat.py
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 3. READ THE SHEET                            │  sourcing/sheets.py
 │    Pick up rows with Status empty or NEW     │
 │    and mark them RUNNING                     │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 4. GROUP THE WORK                            │  sourcing/pipeline.py
 │    Group rows by vendor and requester, and   │
 │    find which login each group uses          │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 5. SIGN IN TO THE VENDOR PORTAL              │  sourcing/auth/manager.py
 │    Reuse the saved session if it is still    │  sourcing/vendors/proponent.py
 │    signed in, otherwise log in               │
 │    ⚠ bot check / 2FA / wrong pwd → stop      │
 │      and alert, never get around it          │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 6. SEARCH THE PARTS                          │  sourcing/vendors/proponent.py
 │    Bulk search (25 parts at a time) and      │  sourcing/vendors/grid.py
 │    read the portal's results table           │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 7. AI READS THE RESULTS                      │  sourcing/extract.py
 │    Claude turns each results page into:      │
 │    price, quantity, condition, lead time,    │
 │    warehouse                                 │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 8. CHECK EVERY VALUE AGAINST THE PAGE        │  sourcing/extract.py
 │    Values not found on the page are removed  │  sourcing/conditions.py
 │    and flagged for the team member           │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 9. WRITE RESULTS TO THE SHEET                │  sourcing/sinks/sheet.py
 │    One row per offer in the "Results" tab    │
 │    Request Status → DONE / NOT_FOUND /       │
 │    NOT_SOLD / FAILED / ...                   │
 └──────────────────────┬───────────────────────┘
                        ▼
 ┌──────────────────────────────────────────────┐
 │ 10. FINISH                                   │  sourcing/pipeline.py
 │    Log out, close the browser, save time     │  sourcing/store.py
 │    and cost per part, send a summary to      │  sourcing/notify/chat.py
 │    Google Chat                               │
 └──────────────────────────────────────────────┘
```

---

## Each step in plain words

### 1. Start
The job reads its settings: which sheet, which AI model, which browser. It then takes a lock, so
two runs never work on the same rows at the same time.

### 2. Prepare tools
The job prepares everything it needs once, before any search:
- **Vendor logins** come from Skylink's **GCP Secret Manager**. They are never stored in code or
  files.
- A **browser** is opened in the cloud (Browserbase). It remembers the previous session, so we
  don't have to log in every time.
- The **AI model** (Claude) is connected.
- **Google Chat** is set up for alerts and the run summary.

### 3. Read the sheet
Every row with a part number and an empty or `NEW` Status is picked up and marked `RUNNING`.
This lets team members see that the work has started.

### 4. Group the work
Rows are grouped by vendor and by team member. Each group uses the right login: the team member's personal
login if they have one, otherwise the shared company login. Groups that share one login wait
their turn instead of signing in twice at once.

### 5. Sign in to the vendor portal
If the saved session is still signed in, it is reused. Otherwise the job logs in with the stored
username and password.

In some cases the job stops for that login and sends a Chat alert instead of retrying:
- the portal shows a **bot check** (a CAPTCHA is tried again for now, 2026-11-03)
- the portal asks for **2FA** that the job can't answer
- the **password is wrong**

### 6. Search the parts
Parts are searched in bulk, 25 at a time, which is about 3 times faster than searching one by
one. The job reads the portal's results table directly instead of the whole page. Parts that
come back empty or with an error are searched once more at the end.

### 7. AI reads the results
Claude reads the results and fills a fixed form for each offer: part number, price, currency,
quantity available, condition, lead time and warehouses. It is told to copy only what the page
shows.

### 8. Check every value against the page
This is the safety step. **Every number the AI returns is checked against the real page.** If a
value is not on the page, it is removed and the row is flagged (for example
`PRICE_NOT_ON_PAGE`). As a result, an invented value can never reach the sheet.

### 9. Write results to the sheet
Each offer becomes one row in the **Results** tab. The original request row gets its final
Status:

| Status | Meaning |
|---|---|
| `DONE` | Offers found and written |
| `NOT_FOUND` | The portal has no result for this part |
| `NOT_SOLD` | The portal says it doesn't sell this part (its own words are copied) |
| `NO_CREDENTIALS` | No login for this vendor or team member, or the password is wrong |
| `NEEDS_ATTENTION` | A person is needed (for example 2FA) |
| `FAILED` | Something went wrong (the reason is in Notes) |

The `Flag` column gives the team member extra information, for example `QTY_SHORT` (not enough
stock), `CONDITION_DIFFERS` or `SUBSTITUTE`.

### 10. Finish
The job logs out of the portal, closes the browser, and saves the time and AI cost of each part.
It then sends one summary message to Google Chat.

---

## Key points for Skylink

- **Passwords stay in Skylink's GCP Secret Manager.** They are never sent to the AI.
- **The AI never has the last word.** Every value is checked against the portal page.
- **Speed and cost:** about 20 seconds per part when searching one by one, and faster in bulk.
  The AI cost per part is about $0.001 with the cheapest model and about $0.015 with the
  standard model.
