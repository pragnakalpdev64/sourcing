# Run 28: first live search through the Sheets add-on relay (2026-11-16)

Run id `local-20261116-123403-8b1e__GmGxPaAg`, test sheet `1TVSs…`, started from the add-on's
test deployment (Search new rows now), worker on this machine (`TRIGGER_RUN_LOCAL=1`).
**The service account had no access to the sheet**: the add-on sent the copy, the worker's
writes waited in Firestore (`sourcing-skylink-addon`), the add-on wrote them (`SHEET_ACCESS=relay`).

Read from the store afterwards (no API log saved):

- 13 rows, all DONE; 5 different parts (8 rows repeated a part: `same_search_as_row`, no extra search)
- Offers per part: 4066-0014: 1, 41391400: 1, 416F25-4: 3, 620-1-200: 2, 41004509-1: 1
- One bulk search, one LLM call: 13,567 in / 3,177 out tokens, **$0.1178** (opus, medium)
- Search 94.7 s; whole run 190 s (start to finish)
- Pending relay writes after the run: **0** (everything written into the sheet by the add-on)
- Requested By empty: test deployments don't run the on-edit trigger that fills it
- Values not yet compared with the Proponent site
