# New vendor checklist: what the working adapters already taught us

Read this before writing a new adapter or prompt. Every line comes from a real
run on Proponent, DASI, Aviall, Wencor, AAR, Monroe, Aircraft Spruce or Textron Aviation; the file and date are
where it was seen. Start from the closest working adapter, not from scratch:

| The new site looks like | Start from |
|---|---|
| Magento store (`catalogsearch/result/?q=`, `/customer/account/`) | `aar.py` |
| BigCommerce store (`search.php?search_query_adv=`, `login.php`, `account.php`) | `monroe.py` |
| Salesforce Experience site, grid of lots per part | `dasi.py` |
| Search bar + list + product pages, quantity rules in the page's inputs | `wencor.py` |
| Many steps, pop-ups, SSO buttons, big slow site | `aviall.py` |
| One product page per part number by address, no multi-part search | `aircraft_spruce.py` |
| Single-page app (Angular / React) whose pages load JSON from an API | `incora.py` |
| ASP.NET page with a jqGrid | `proponent.py` + `grid.py` |
| ASP.NET MVC part page per part + a multi-part form, values in hidden fields | `textron.py` |
| Anything else | `generic.py` alone first (vendor record only), then an adapter |

## 1. Look at the real pages first (no login needed for public ones)

- `curl` the search page and a product page, and open them once in local
  Chromium (Playwright): the HTML says the platform, the card markup and what
  is drawn by the server vs. by script (Monroe 2026-11-04: all on the server).
- Build the test mock from that real HTML, not from guesses (`tests/test_monroe_adapter.py`).
- Save what the run reads with `--dump-pages`; check `debug_pages/*-read.txt`.

## 2. Login

- **The login form can be in an iframe** (Monroe pop-up, 2026-11-04). The generic
  login now looks in shown frames on the vendor's site (`GenericAdapter._form_frame`).
- **Link text is matched as a substring**: "Login" matched "Forgot Login"
  (Monroe). Forgot / reset / register links are excluded in `web.LOGIN_LINK_SELECTORS`.
- **Two forms on one page** (Wencor header drop-down + page form): pin the page's
  own form in `KNOWN_SELECTORS`.
- **A newsletter box is often the page's only `input[type=email]`** (Aircraft Spruce
  2026-11-05: the User ID field is a plain text box labelled "User ID / Dealer #", so the
  login typed the email into the footer's newsletter box). Look at the login form's
  own fields and pin them.
- **Direct login page can trip a CAPTCHA** where the site's own path doesn't
  (AAR: landing page -> avatar -> "Sign In or Register"); `human_typing` helped.
- **An image CAPTCHA has no wording in the page** (Aircraft Spruce 2026-11-05: Google's
  reCAPTCHA challenge sits in its `recaptcha/api2/bframe` frame). Detect the shown frame and
  hand it to a person (`MfaRequired("captcha")` -> Live View); retrying only raises Google's
  risk score. The program never solves one.
- **Login button opens a dialog, not a page** (Aviall): wait for what comes next,
  don't wait only for navigation.
- **The page after sign-in may show nothing the rules know** (AAR home page):
  give the adapter a cheap signed-in check, e.g. the account page fetched with the
  browser's cookies (200 vs redirect to login): `aar.py`, `monroe.py` `_account_state`.
- **Know what the account page answers when signed in**: BigCommerce's
  `account.php` redirects to `account.php?action=order_status` (Monroe 2026-11-04);
  only a redirect to the login page means signed out. Read it as "can't tell" and a
  good login is tried again over the live session (that suspended Monroe).
- If the login itself read its page as signed in but the probe can't prove it, the
  worker now searches once instead of logging in again (`login_claimed`).
- Use the platform's own logout address when it has one (Magento
  `/customer/account/logout/`, BigCommerce `/login.php?action=logout`) and confirm
  it with the same check. Logout must be confirmed before the browser closes.
- **An OAuth / Azure AD B2C sign-in address is one sign-in's own** (Incora 2026-11-05: `authorize?state=...&code_challenge=...`). Saved as the login page and opened on the next run, B2C took the password and the app refused the answer ("Still on the sign-in form"). Start on the app's own page every time and never save the B2C address (`IncoraAdapter.login_url_candidates`, `_remember_login_page`). Short-lived tokens (Incora: signed out ~3.5 min after login) are refreshed by the app while its page is open: wait on the open page before opening the next one.
- **A Log In button the page enables only after Google's invisible reCAPTCHA v3** (Textron
  2026-11-12: `#btnSignIn disabled` until the score comes back; a low score shows the "I'm not a
  robot" box). Wait for the button, never click a disabled one; the box goes to a person
  (`TextronAdapter._sign_in_state`, `MfaRequired("captcha")`).
- **A page's hidden form fields can hold the values it shows** (Textron's part blocks: `[n].Uom`,
  `[n].MinOrder`, `YourPriceUS_n`, `[n].ErrorMsg`): read them as the program's own facts.
- **A site can answer a headless browser with its own 404 page** (Textron 2026-11-12, every
  address; curl with a browser's user agent got 200). Recognise that page and stop
  (`LoginBlocked`): read as "no results" it would mark every part NOT_FOUND. Never work round it
  by faking the browser.
- The login page comes from the secret's `url` (vendor config `login_url` wins).
  Don't hard-code one in the adapter unless the team says so (Monroe, 2026-11-04).

## 3. Search: direct addresses first, clicks last

- **Prefer a direct results address** over typing in a search box: Proponent
  direct URL ~20s/part vs the form; DASI `exact=true`; AAR / Monroe `?q=`.
- **Prefer a multi-part address** for bulk when the site has one (AAR, Monroe:
  parts joined by `%2C+`; DASI `&product=` repeated).
- **Search that matches words** (AAR, Monroe: 8,898 results for 3 parts): keep only
  cards whose part number is a requested one (spaces/dashes/slashes/dots aside),
  read further result pages until every part has one, search a missing part alone.
- **Open detail pages by their address, never by clicking a card** (Wencor, Monroe).
  Several at once in their own tabs, images/fonts blocked (Monroe: 2 pages in
  ~5s). A plain `page.request` fetch is faster but **misses what the page's script
  draws**: Monroe's stock number is only in its script-drawn variants table, so
  the first run (fetch only) had no quantity. Fetch is the fallback, not the way.
- **Every value a detail page can show, before writing the prompt**: stock number,
  price breaks (often a hidden pop-up: Monroe "Bulk Discounts"), MOQ, unit,
  certificates / trace (option lists), lead time, warehouses. Check it in a real
  browser after the page's scripts ran, and open what only shows on demand
  (pop-ups). Don't pass on filter choices (Monroe's Manufacturer / Revision /
  MFR Certificate drop-downs): the team saw them turn into values that weren't
  the product's (2026-11-04). Tagged By only from a label that names who tags
  or certifies the part; a spec standard (Monroe's `Industry Standards/Certification:
  MS25036`) is not a tag (team, 2026-11-05: blank unless a real tag / cert source).
- **Check the LLM's offer against facts the program reads itself** when the page
  states them in fixed wording (Monroe `page_facts` / `_finish`, 2026-11-04): MOQ,
  certificate label, stock, unit price, price-break rows are set to the page's own;
  fields the vendor never shows (trace, warehouses) are cleared; an offer matching
  no product read is dropped. The value check only proves words are *somewhere* on
  the page ("MFR Certificate" was, in a filter list), not that they belong there.
- **MOQ only from the stated minimum** when there is one; price breaks that start
  higher are discounts (`pipeline.min_order`, 2026-11-04: Monroe MOQ 100, rates
  from 500 had made it 500).
- **A detail page whose data didn't show gets one more try, alone; a part no
  search lists (not even searched alone) is reported as not found** to the
  pipeline, so it isn't searched again one by one (no ghost retries).
- Don't open what adds nothing (DASI's View dialog: ~13s each, dropped 2026-10-29).
- **A search box can POST** (Aircraft Spruce, 2026-11-05): `search.php?s=...` by GET listed
  nothing, so the box is used. Check the form's method before calling an address "direct".
- **Stock can depend on the quantity in the box** (Aircraft Spruce: no stock number, "In Stock"
  / "Partial Stock" / "No Stock" worked out by script for the Quantity box). Type the requested
  quantity and read the line again; the price too, before and after.
- **A product address can answer with another part** (Aircraft Spruce `pnpages/MS20470AD4-4.php`
  shows AN470AD-4-4): compare the page's own part number (and MFR number) with the request.
- A public site still needs a sign-in check on the search page: signed-out pages
  show public prices (Monroe `_open_results`).

## 4. Reading the page

- **A single-page app keeps what it shows in its own data** (Incora 2026-11-05, Angular): read
  its script bundle (`main.*.js`) before the first login: routes, API addresses, what it keeps
  in localStorage. Incora's Bulk Search takes its parts from localStorage and saves the whole
  result there: writing the list + opening the page is the search, and the stored result has
  every row of every table page (the paginator's "1 - 50 of N" checks it). Values only behind
  a click (the $ price pop-up) come from the same address the app asks, called from inside the
  page (`page.evaluate(fetch)`), never `page.request` (not routed in tests: it reached the real
  site once).

- Give the LLM the program's own text per card (`--- Card n: <link> ---` blocks),
  not the whole page: menus, filters and look-alike cards stay out (AAR, Monroe).
- Write what only shows on hover / in icons / in pop-ups into the card (Aviall,
  AAR `extra`), and grid rows as records (Proponent `grid.py`, DASI).
- **A list of selectors in one `querySelector` returns the first in page order**,
  not the first selector: a share `mailto:` link came before the product link
  (Monroe). Try selectors one by one.
- **Server HTML can hide what the browser shows** (Monroe's `Minimum Purchase` dl
  has `display:none` until script runs): don't drop inline-hidden parts from fetched HTML.
- **The detail section may be outside the obvious container** (Monroe
  Specifications outside `.productView`): read the main content, cut at
  "similar products".
- Wait for script-drawn lists (Wencor: 0.3s read found nothing; wait for cards or
  a "no results" line).
- Same product under two addresses = one line (Wencor item number, Monroe link
  without `?searchuuid=`).
- Quantity rules can live in input attributes, not text (Wencor `data-min`).

## 4b. Page actions: coded, then the LLM, never a quiet gap

- Every action that fills in data (a dropdown entry, a pop-up, a "show more")
  goes through `self.assist.act(...)` when its coded way fails: a saved recipe,
  then the LLM picks the clicks, and the adapter's own **check** decides whether
  the data appeared (Proponent warehouses: the row's numbers change; Monroe:
  the bulk rates are there). Give it a `scope` near the control.
- Whatever is still missing is **flagged on the line** (Proponent
  `WAREHOUSES_INCOMPLETE` + "OTHER WAREHOUSES NOT READ: ..."; Monroe
  `PRICE_BREAKS_NOT_READ`; any vendor `WAREHOUSES_SHORT` when warehouses add up
  to less than the total). Never let a gap look like complete data.
- The run's Chat summary lists the actions the code couldn't do alone: the cue
  to update the adapter.

## 5. Prompt (`<vendor>_prompt.py`)

- Extend the shared `EXTRACT_SYSTEM` with one section; the client's field rules
  stay shared. Name the page's exact wordings and map each to a field (table).
- Always cover: price vs RFQ / Request Quote (`"Not Present on website"`), `$0.00`,
  stock with/without a number, out of stock, MOQ vs "increments of", unit, what is
  never taken (header, filters, cart, footer, similar products), substitutes or not.
- **A stock status without a number** ("In Stock") is kept as `"In stock"` by the
  value check since 2026-11-04; before, it was deleted and the stock rule then
  deleted the price (every Monroe line went No Quote).
- Vendor shows no condition: set `condition_not_shown` (default `NE`, Wencor and
  Monroe), else in-stock lines go out as "No Quote".

## 6. Tests and runs

- `route.fulfill(status=302)` in a mock: the browser fetches the redirect target from the
  **real** site (not routed). Redirect by script in mocks (Aircraft Spruce tests, 2026-11-05).
- `page.route` doesn't intercept `page.request`: patch `_account_state` /
  `_fetch_html` in tests, or they hit the real site.
- A form inside a script-made `about:blank` iframe gets no `submit` event in
  Chromium: mock pop-ups sign in by a click handler.
- The worker does NOT need onboarding: rules -> saved selectors -> LLM fallback.
  Onboarding (`scripts/onboard_vendor.py`) is the safe first look: 1-3 parts, no
  sheet writes. Two failed logins SUSPEND the account; reset it before rerunning.
