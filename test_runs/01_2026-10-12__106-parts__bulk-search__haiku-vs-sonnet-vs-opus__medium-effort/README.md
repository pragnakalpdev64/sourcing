# 01 · 2026-10-12 · 106 parts · bulk search · haiku vs sonnet vs opus (medium effort)

First full-size test. Same 106 parts from the local `test.xlsx`, three runs, one per model. Bulk box with 25 parts per
search (5 bulk searches, 6–7 LLM calls), results read from the flattened page text.

| Model | Result | LLM cost | Whole run |
|---|---|---|---|
| haiku | 105 done, 1 not found | $0.0125 | 459s |
| sonnet | 105 done, 1 not found | $0.1731 | 372s |
| opus | 105 done, 1 not found | $0.3828 | 406s |

`*__results-sheet.xlsx` is the sheet after each run (`Requests` statuses + `Results` offers). 8 rows in the sheet had no
part number and were skipped. No per-part report: bulk runs record one cost per batch.
