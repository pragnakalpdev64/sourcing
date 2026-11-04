# 06 · 2026-10-13/14 · 20 parts · one by one · direct results URL · medium effort · haiku vs sonnet vs opus

Same 20 parts as run 05, but each part's results page is opened directly
(`PartSearchResults.aspx?id=<part>`) instead of through the search form, with `LLM_EFFORT=medium`.
This is the current code path.

| Model | Result | LLM cost | Per part | Search per part (avg) | Whole run |
|---|---|---|---|---|---|
| opus | 20 done | $0.7045 | $0.0352 | 19.0s | 471s |
| sonnet | 20 done | $0.3011 | $0.0150 | 21.1s | 501s |
| haiku (run 2026-10-14) | 19 done, 1 failed | $0.0191 | $0.0010 | 20.6s | 487s |

The haiku failure (row 4, AS15001-1P) is a dropped connection while writing to the Google Sheet; the search itself worked.
The sonnet report was rebuilt from its log (see the note in `../README.md`).
