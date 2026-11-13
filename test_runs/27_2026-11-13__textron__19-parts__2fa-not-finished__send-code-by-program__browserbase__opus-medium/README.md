# Run 27: Textron Aviation, 19 parts, 2FA not finished (2026-11-13 11:45–11:55 IST)

Local API (`TRIGGER_RUN_LOCAL=1`, `--browser browserbase --dump-pages`), same 19 rows as run 26.

- 11:45 Browserbase session (saved Context, signed out at the end of run 26), login page opened.
- 11:46:44 Log In enabled by reCAPTCHA v3, submitted -> /Account/SendCode.
- **2FA by the program, first time on the real page**: `select#SelectedProvider` -> Email, `button#btnSendCode`
  clicked -> /Account/VerifyCode?Provider=Email, code box `input#Code` found (pages saved here,
  anti-forgery tokens removed). Live View link sent; the run waited for the code.
- No code entered. 11:54 the page was back on /Account/SendCode (the code page's "I didn't receive the
  code. Take me to the previous step." link, or the step ending); the wait went on, logging the
  `/Dashboard` check every 5s, until the API was stopped with Ctrl+C at 11:55. The Browserbase session
  ended at 11:55:56 (checked through the API: COMPLETED, nothing left running). No search ran.

Changed after this run: the Live View wait ends as soon as the adapter sees the sign-in end
(`MfaRequired` from `person_finished_login`, hitl.py): Textron back on the login form after the code
step, or on Send Code for over 3 min -> Chat message, rows NEEDS_ATTENTION, logout checked, browser
session closed. The `/Dashboard` check logs only when its answer changes. SIGTERM ends a worker like
Ctrl+C (sessions released). The 2FA elements are pinned (`#btnSendCode`, `form#frmVerifyCode input#Code`).
