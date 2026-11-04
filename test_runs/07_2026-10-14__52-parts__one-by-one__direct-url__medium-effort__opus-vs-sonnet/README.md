# 07 · 2026-10-14 · 52 parts · one by one · direct results URL · medium effort · opus vs sonnet

52 parts in the Google test sheet (rows 2–53), searched one at a time by opening
`PartSearchResults.aspx?id=<part>` directly, `LLM_EFFORT=medium`. Same parts as run 08 (bulk).

| Model | Result | LLM cost | Per part | Search per part (avg) | Whole run |
|---|---|---|---|---|---|
| opus (`claude-opus-5-5`) | 50 done, 2 not found | $1.5018 | $0.0289 | 19.1s | 1158s (22.3s/part) |
| sonnet (`claude-sonnet-5-5`) | 52 done | $0.6785 | $0.0130 | 16.0s | 973s (18.7s/part) |

## Differences between the models

- **Row 30, 5E2675-1, opus NOT_FOUND: false.** The results grid was still empty when the page
  was read (`vendor_pages/opus/…-single-5E2675-1.txt` has no records), so opus correctly
  answered "no results". Sonnet, and both run-08 models, found it (in stock, $14,338.26, qty 2). This is a page-loading
  timing problem, not a model error.
- **Row 46, 0684-12-28: sonnet is wrong.** The portal says "This part number is not recognized
  in Proponent system … restricted for sale". Opus marks it NOT_FOUND; sonnet marks it DONE
  with one empty offer (no price, qty or condition).
- Rows 2, 23, 28, 33: small stock-quantity differences (e.g. 151632 vs 150132). They follow the
  time of the run, not the model, so they look like live stock changes.
- All other rows: identical offers.

## Files

- `*.log`: full run logs; `Row N offer:` lines hold each extracted offer.
- `*__per-part-report.txt|csv`: `scripts/run_report.py` per part.
- `vendor_pages/opus/`, `vendor_pages/sonnet/`: the page/grid text each model was given
  (`--dump-pages`). Git-ignored.
