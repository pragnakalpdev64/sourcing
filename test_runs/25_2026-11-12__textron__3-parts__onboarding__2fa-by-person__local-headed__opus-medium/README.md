# Run 25: Textron Aviation, first signed-in run (onboarding, 2026-11-12 18:56–19:00 IST)

`scripts/onboard_vendor.py textron_aviation --browser local --headed --live-view --fresh`
with 3 parts, `DEBUG_DUMP_PAGES=1`, LLM anthropic opus / medium. Login from the secret
`sourcing-vendor-textron-aviation` (added the same day on `/credentials/new`). Nothing written
to a sheet.

- Login: email + password typed key by key, Log In enabled by reCAPTCHA v3 (no checkbox),
  then the portal's email 2FA page. The program didn't find the code box itself (rules + LLM:
  none); not needed: the team member typed the code in the shown window, the Live View wait
  saw `/Dashboard` 200 after ~2 min and carried on (login 196s incl. the person).
- A-7079B-24: Your Price $42.01 (list $70.01, 40% off; signed out the page's data said 45.51),
  160 IN STOCK, Ships from Wichita -> 42.01 USD, qty 160, NE, In stock. 15.5s
- MS20470AD4-4: Your Price $75.65, 0 IN STOCK, "Order today with estimated availability
  12/27/2026." -> 75.65 USD, Out of stock, NE, lead "Estimated availability 12/27/2026". 10.5s
- NOSUCHPART123: "Part Not Found."; dashboard check 200 first, so a real not found. 5.4s
- Logout: no Log Off link visible (likely in the account menu), the page's own logout form
  submitted, `/Dashboard` -> login page: signed out.
- LLM $0.1043 (3 calls, 1 for the 2FA code box); whole run 232.6s.

Changed after this run: the program reads "<n> IN STOCK" (0 = out of stock), "Ships from" and the
NOTES "SOLD BY THE POUND" / "PRICE VALID UNTIL STOCK DEPLETED" itself; the prompt names the
signed-in wordings (List Price never the price). The onboarding note "no direct results address
learned" doesn't apply: Textron opens each part by its PartsDetail address by design.
Not tried yet: the Multiple Part Search signed in (its quantity boxes), Browserbase (402, free
minutes used up).

Files: `onboarding.log`, `report.json`, `*-read.txt` (the text the LLM read per part).
