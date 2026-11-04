# Vendor login update page (plan step 6)

Status 2026-10-23: **built in code** (share copy), tested with fakes only. Not deployed; needs the
client's OAuth client and the answers in "Still open" below before real use.
2026-10-28: admins can also **add a login** (creates its secret) and **choose who may change a
shared login** on the page; see "Adding a login" below. Tested locally 2026-10-28 against the real
Secret Manager (`skylink-material-planning`, `DEV_SIGNIN_EMAIL` + the developer's gcloud login):
the page created `sourcing-vendor-alne-check`, seen with `gcloud secrets versions list`. The Google
consent step (`/auth/grant`) and the deployed service account are not tested yet.

## Why

Skylink team members are not technical and won't use the GCP console or `gcloud` (client rule 6), but
vendor passwords change about monthly. They need a simple page to update a login; the password
must still end up only in Secret Manager.

## Decided (2026-10-23)

- **Sign-in with Google OAuth inside the app** (OpenID Connect, authorization-code flow), not IAP.
  Only Skylink Google accounts (`ALLOWED_EMAIL_DOMAINS`, checked on the verified ID token).
- After sign-in the team member **picks a vendor**, then **one of the logins they may change** for
  it (their own, or a shared login they were given), then types the new password.
- Who may change which login:
  - their **own personal login** (`sourcing-vendor-<vendor>__<their-email>`): always;
  - a **shared login**: only people listed as its editors, plus admins;
  - **admins** (`CREDENTIAL_ADMINS`): every login.
- Changing a password **only adds a new version** to an existing secret, with our service
  account, and never shows a password.
- (2026-10-28) A new login's secret is created **by an admin, with the admin's own Google
  access**, on `/credentials/new`. Our service account still can't create secrets, so "new
  secrets are created by the client" still holds: the client's admin does it, on our page.

## Flow

```
/credentials ──(not signed in)──► /auth/login ──► Google ──► /auth/callback
     │                                                      (ID token checked: verified email,
     │                                                       allowed domain) → session cookie
     ▼
 1. pick vendor          (only vendors with at least one login this person may change)
 2. pick login           (Shared team login / own login / another person's, with status + last change)
 3. /credentials/update  new password ×2, optional username and 2FA key (blank = keep)
     │ POST (CSRF token, permission checked again)
     ▼
 Secret Manager: new version of the login's secret (old JSON merged, so url / totp_secret stay)
 accounts/<vendor>__<owner>: password_changed_at/by; NEEDS_CREDENTIAL_UPDATE → NEEDS_LOGIN
 audit: credential_updated (who, which login, which fields; never values)
```

A wrong password during a run (account → `NEEDS_CREDENTIAL_UPDATE`, rows `NO_CREDENTIALS`) now
puts a link to that login's update page in the Chat message (when `PUBLIC_BASE_URL` is set).

## Adding a login (2026-10-28)

```
/credentials (admin) ─► "Add a login" ─► /credentials/new
     no Cloud access yet ─► "Continue with Google" ─► /auth/grant ─► Google consent
         (scope cloud-platform, login_hint = the signed-in admin, no refresh token)
     ─► /auth/callback: same account as the session? scope given? ─► encrypted cookie sai_gcp
         (Path=/credentials/new, 10 min, tied to the session id)
     ─► form: vendor, shared or a team member's own, username, password ×2, 2FA key,
         login page, editors (shared only)
     ─► POST: Secret Manager create_secret + add_secret_version AS THE ADMIN
         secret already has a value → left alone, only listed; exists without one → filled
     ─► accounts/<vendor>__<owner> (editors), audit credential_created
```

Two locks on who may create a secret:
1. **Our page**: only `CREDENTIAL_ADMINS` see `/credentials/new` and `/credentials/editors`
   (checked on GET and again on POST, CSRF token on every POST).
2. **Google IAM**: the call runs with the admin's own token, so it works only if their Google
   account may create secrets in the project (`secretmanager.secrets.create`, e.g. *Secret
   Manager Admin*). A bug in our page can't let anyone else create a secret, and our service
   account never gets that right.

The token: Secret Manager has no scope narrower than `cloud-platform`, so it is kept as little
as possible: encrypted (Fernet, key from `SESSION_SECRET`), HttpOnly, sent only to
`/credentials/new`, 10 minutes, dropped on sign-out, never logged or stored server side; the
code only creates `sourcing-vendor-*` secrets with it. Locally with `DEV_SIGNIN_EMAIL` the
developer's own `gcloud auth application-default` login is used instead.

Who may change which login (`/credentials/editors`, admins): a shared login's editors are a list
of Skylink emails (other domains refused); a personal login is only its owner's and the admins'.
Every change is audited (`login_editors_changed`, added/removed). `manage_logins.py grant/revoke`
does the same from the command line.

## Finding a login made with gcloud (2026-10-28)

We can't list secrets (listing is a project-wide right and would show every Skylink secret's
name), but we can read any `sourcing-*` secret by name, and the name follows from vendor + owner.
`/credentials/find` (everyone signed in): type the vendor; we look for
`sourcing-vendor-<vendor>` and `sourcing-vendor-<vendor>__<your-email>`; each one found is listed
(audit `login_found`), so its password can be changed before the worker ever used it. Finding a
shared login gives no right to change it (editors/admins only). Admins may also look for one
team member's own login. Nothing from the secret is shown. `manage_logins.py add <vendor>` now
accepts a vendor with no record when its secret exists (e.g. `ils`).
Automatic listing (client routes Secret Manager "CreateSecret" audit logs to us) is a question for
the client.

## Pieces

| Piece | Where |
|---|---|
| Google sign-in, signed session cookie, CSRF tokens | `service/web_auth.py` |
| Pages: vendor list, login list, update form, save | `service/credentials_ui.py` (router in `service/api.py`) |
| Who may change what | `sourcing/credentials/access.py` |
| Login register (= account records) | store `accounts/<vendor>__<owner>`: `vendor_id`, `user_email`, `secret_key`, `editors`, `password_changed_at/by`; `Store.list_accounts()` |
| Add logins to the register / give editors | `scripts/manage_logins.py` |
| Secret write without creating a secret | `CredentialProvider.update(..., must_exist=True)` |
| Add a login (admin's own Google access) | `/credentials/new`, `/auth/grant`, `web_auth.gcp_credentials`, `gsm.create_login_secret` |
| Who may change a shared login | `/credentials/editors` (admins) |
| Chat link after a wrong password | `Pipeline` (`Deps.public_base_url`) |
| Settings | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`, `ALLOWED_EMAIL_DOMAINS`, `CREDENTIAL_ADMINS`, `PUBLIC_BASE_URL`; local only: `DEV_SIGNIN_EMAIL` |

The register: our account can't list secrets, so the account records are the list of logins. The
worker adds a record the first time it uses a login; `manage_logins.py add` adds one before that
(e.g. a new shared login, or to give editors).

## Security

- Session cookie: HMAC-signed (`SESSION_SECRET`), HttpOnly, Secure on https, SameSite=Lax, 8 h.
- OAuth `state` checked against a signed short-lived cookie; `next` only accepts local paths.
- ID token verified with Google's keys (`google.oauth2.id_token`), audience = our client id,
  `email_verified` required, domain from the email (and `hd`) checked.
- Every POST carries a CSRF token tied to the session; permission is checked again on save.
- The password is never shown back, logged, stored in Firestore or sent in Chat. Pages send
  `Cache-Control: no-store`.
- Our service account can only add versions to `sourcing-vendor-*` (infra/setup_gcp.sh). Secret
  Manager's Admin Activity log records each new version (as our service account); our `audit`
  record says which person did it.
- `DEV_SIGNIN_EMAIL` (sign in without Google, for local testing) is refused on Cloud Run.

## Client setup (before deploy)

1. In `skylink-material-planning`: OAuth consent screen **Internal**, an OAuth client of type
   "Web application" with redirect URI `<PUBLIC_BASE_URL>/auth/callback`. Internal needs the
   project to be in Skylink's Workspace organization.
2. Client id → `GOOGLE_OAUTH_CLIENT_ID` (env var); client secret → secret
   `sourcing-google-oauth-client-secret`; `sourcing-session-secret` is generated by
   `infra/setup_gcp.sh`.
3. Admin emails → `CREDENTIAL_ADMINS`; editors of each shared login on `/credentials/editors`
   (or `manage_logins.py grant`).
4. For adding logins on the page: add the scope `.../auth/cloud-platform` to the consent screen,
   and give each admin's Google account the right to create secrets in the project (e.g.
   *Secret Manager Admin*). Admins without it can still change passwords and editors.

## Still open (questions for the client)

1. Who are the admins (emails)? Who may change each **shared** login?
2. Is `skylink-material-planning` in Skylink's Workspace organization (Internal consent screen)?
3. Which email domain(s) may sign in?
4. Which vendors force a password change, and how often? (for a later reminder)
5. Disable old secret versions after the new password worked? (needs `secretVersionManager`)
6. Vendor names: one fixed name per vendor in every sheet row (the secret name is made from it).

## Next (not built)

- `NO_CREDENTIALS` rows go back to `NEW` by hand (the page says so); automatic re-queue later.
- Monthly reminder in Chat from `password_changed_at`.
- "Test login" button (a real login, so only for vendors that tolerate a failed one).
- Sign-in check on `/hitl/view` and the code form can reuse `web_auth.py`.

## Checked against Google's docs (2026-10-21)

| Need | Google API / setting | Permission | Our account has it |
|---|---|---|---|
| Worker reads a login | `secrets.versions.access` on `versions/latest` | `secretAccessor` | yes, `sourcing-*` |
| Team member changes a password | `secrets.addVersion` (payload ≤ 64 KiB) | `secretVersionAdder` | yes, `sourcing-vendor-*` |
| Admin adds a login | `secrets.create` then `addVersion` | `secrets.create` on the project | no; done with the admin's own token (2026-10-28) |
| Disable / destroy old version | `versions.disable`, `versions.destroy` | `secretVersionManager` | no (question 5) |
| Sign-in | OAuth 2.0 web client + OpenID Connect ID token | client admin creates the client | n/a |

- `addVersion` has no "only if unchanged" check, so one secret per login (as now) avoids lost changes.
- A disabled or destroyed **latest** version gives an error, so only older versions may be disabled.
- What `access latest` returns for a secret with no version is not documented; `gsm.py` maps only
  NotFound / PermissionDenied to "no login".
