# 08 · 2026-10-14 · 52 parts · bulk search, 25 per search · medium effort · opus vs sonnet

Same 52 parts as run 07, through Proponent's BULK SEARCH box: `--bulk 25 --set
proponent:bulk_batch_size=25` → 3 searches (25 + 25 + 2 parts), results read from the jqGrid
records. Parts a bulk search misses are searched again one by one before logout.

| Model | Result | LLM cost | Per part | Whole run |
|---|---|---|---|---|
| opus (`claude-opus-5-5`) | 51 done, 1 not found | $0.7713 | $0.0148 | 388s (7.5s/part) |
| sonnet (`claude-sonnet-5-5`) | 52 done | $0.3484 | $0.0067 | 349s (6.7s/part) |

The LLM cost is the run's bill from the log's run summary. The per-part reports split each batch's
cost across its rows and come out slightly lower ($0.7566 / $0.3414). The "search time per part" in
the reports is the whole batch's time, so it isn't comparable with run 07; use the whole-run time.

## Compared with run 07 (one by one)

- **About 3x faster and about half the LLM cost** for both models.
- **Same offers:** every row matches run 07, apart from the live stock-quantity changes, and
  run 07's false NOT_FOUND on row 30, which bulk search found.
- **Row 46, 0684-12-28** ("not recognized … restricted for sale" on the portal): bulk misses
  it, the one-by-one retry finds the same message, and as in run 07, opus says NOT_FOUND
  (correct) and sonnet says DONE with an empty offer (wrong).

## Files

- `*.log`: full run logs. The sonnet log was overwritten by an accidental re-run and was restored
  from the terminal output of the real run (run id `local-20261014-133949-9f88`).
- `*__per-part-report.txt|csv`: `scripts/run_report.py` per part.
- `vendor_pages/opus/`, `vendor_pages/sonnet/`: the grid records each model was given.
  `earlier-attempt-no-log__*` are two opus tries (18:39, 18:43) just before the logged run. Git-ignored.
