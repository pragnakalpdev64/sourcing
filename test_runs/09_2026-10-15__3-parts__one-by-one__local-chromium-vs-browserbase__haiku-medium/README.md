# 09 · 2026-10-15 · 3 parts · one by one · local Chromium vs Browserbase · haiku / medium

The client asked to compare a browser running inside our own container with Browserbase.
Same 3 parts (first 3 rows of `samples/requests_20-parts_proponent.xlsx`), run one after the
other from a laptop at 02:50 US Eastern: `--browser local` (headless Chromium 156 started by
Playwright 1.64) and `--browser browserbase`.

| Browser | Result | Login | Search per part | Whole run | LLM cost |
|---|---|---|---|---|---|
| local Chromium | 3 done | 28s (cookie popup wait ~15s) | 7.2s / 11.8s / 10.2s | 65s | $0.0026 |
| Browserbase | 3 done | 28s | 17.1s / 17.8s / 19.4s | 105s | $0.0025 |

- Offers are identical for all 3 parts (price, qty, condition, lead time).
- No CAPTCHA or bot check with either browser.
- The local browser ran from an office connection, not from Cloud Run. Cloud Run sends traffic
  from Google data-centre addresses, so a bot check there is still possible and has to be
  tested after deploy.
- Browserbase had session recording on (`BROWSERBASE_RECORD_SESSIONS=1` in `.env`).
