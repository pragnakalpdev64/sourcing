# Run 26: Textron Aviation, 19 parts, first signed-in Multiple Part Search (2026-11-12 19:55–20:06 IST)

Started by the sheet's Apps Script through `/trigger` (local API, `TRIGGER_RUN_LOCAL=1`,
`--browser local --headed --dump-pages`), Google test sheet `…aF7CVqak` rows 67–85, LLM opus /
medium. The worker's log went to the API's terminal and isn't saved; this folder has the text
the LLM read (`*-multiple-read.txt`) and what the store recorded.

- 20:01 2FA (email) by the team member in the shown window; 20:02 signed in.
- One Multiple Part Search, 19 parts, all 19 blocks read; 19 rows DONE by 20:05:51, RFQ tab
  written. No "Quantity typed" lines: the signed-in form showed no quantity box the rules know.
- RFQ checked against the page: prices = "Your Price" (not the list price), stock from
  "<n> IN STOCK", warehouses from "Ships from Wichita" / "Ships from Columbus, GA" ->
  "• STK WICHITA / ◦ WICHITA: 160 ON HAND" (correct, written by the LLM). Quantity Requested =
  the stock: the client's rule of 2026-10-23, not a bug.
- 20:05:57 logout confirmed. 20:06:00 a second pass started on Textron (reason not in the saved
  data); its browser closed and the logout hold then waited with no end ("Connection closed
  while reading from the driver" every 15s).

Changed after this run: the hold ends when the browser is gone; the program sets warehouse,
lead time ("Estimated availability <date>" / "In stock"), no price for 0 / "PLEASE CONTACT US
FOR ASSISTANCE WITH PRICING", notes (multiples of N, shelf life, hazardous, extended inactivity);
"NO LONGER AVAILABLE" -> NOT_SOLD with the options listed (M83461-1-122, C3317-230, C352TS were
"No Quote" lines); 2FA: Email chosen and Send Code clicked by the program, the person types only
the code; the form's row inputs are logged when no quantity box is recognised.
