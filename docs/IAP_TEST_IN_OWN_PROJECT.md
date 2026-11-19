# IAP test in our own Google Cloud project

Goal: prove, before the client changes anything, that the add-on's call gets through
Google sign-in (IAP) on a Cloud Run service. The test uses our own project and a tiny
echo service that shows every header it receives.

Written 2026-11-17. The steps follow Google's docs as read that day:
- "Configure IAP for Cloud Run" (docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)
- "Programmatic authentication" (docs.cloud.google.com/iap/docs/authentication-howto)
- "How to share OAuth clients" (docs.cloud.google.com/iap/docs/sharing-oauth-clients)
- `gcloud iap settings set --help` (gcloud 593: `--resource-type=cloud-run` is supported)

## What it proves and what it doesn't

Proves:
1. IAP on Cloud Run lets a signed-in, allowed person through in the browser.
2. A call from Apps Script with `ScriptApp.getIdentityToken()` in `Proxy-Authorization`
   is **refused** while the script's client ID is not on IAP's programmatic-clients allowlist.
3. The same call **goes through** once the client ID is on the allowlist.
4. Our own `Authorization` header (the add-on's access token) still reaches the service
   behind IAP.
5. IAP checks the person too: remove them from the access policy and the call gets 403.

Doesn't prove (only the first real call on sourcing-ai-dev will):
- Skylink's own organization policies.
- The published add-on's real client ID (`1011595872116-n7pg…`) and its consent.

## Cost

About $0, at most a few cents:
- Cloud Run bills only while it handles a request and scales to zero when idle. The free
  tier is about 2 million requests and 180,000 vCPU-seconds a month per billing account;
  this test makes about 10 short requests.
- IAP on Cloud Run has no charge. Paid extras, such as context-aware access levels, aren't
  used here.
- The image is pulled from Docker Hub, so there's no Artifact Registry storage and no
  Cloud Build. Cloud Shell, Apps Script and enabling the APIs are free.
- The project still needs a billing account linked, because Cloud Run won't deploy without one.
- Avoid: **Minimum instances** above 0 and **CPU always allocated**. Keep both defaults.
- Check **Billing > Reports** the next day; it should show $0.00.

## Seen on 2026-11-17 (first try, Gmail-owned project with no organization)

- C passed only after the out-of-org setup: Custom OAuth (auto-generated), consent screen
  External in Testing status, users added as test users.
- D gave the expected refusal: `401 ... Error code 900`.
- E failed twice:
  1. The script on Apps Script's default project had a Google-owned client
     (`911032974378-…`): `INVALID_ARGUMENT ... Error with client id`.
  2. A client in a Gmail-owned project:
     `Programmatic Oauth Client with client id … does not belong to an organization`.
- `service/iap.py` checked a real `x-goog-iap-jwt-assertion` from the browser call
  (audience `/projects/631296489440/locations/us-central1/services/iap-test`): it returned
  the signed-in email. It refused the same token checked against another service's
  audience, with a different plain email header, and with a changed signature (401 each).
- **Conclusion:** the client on IAP's programmatic allowlist must be in a project that
  belongs to an organization, so a Gmail project cannot finish this test. Use a project
  inside the pragnakalp.com organization; the script must be attached to it (Apps Script >
  Project Settings > Change project). The real setup meets this: `sourcing-skylink-addon`
  and `skylink-material-planning` are both in Skylink's organization (803872269278).

## What you need

- A Google account in our own workspace (`yash.patel@pragnakalp.com`), **not** a Skylink
  account, and a test project of ours **with billing linked**.
- Owner (or Cloud Run Admin + IAP Policy Admin + IAP Settings Admin) on that project.
- If the project has no organization: Google says to switch IAP on the first time from the
  Cloud Run page in the console (step B does that).

---

## A. Pick the project and enable the APIs

1. Open console.cloud.google.com as `yash.patel@pragnakalp.com` and choose the test
   project in the project picker at the top.
2. Write down the **Project number** from the dashboard. It's needed for the optional
   server check at the end.
3. Menu ☰ > **APIs & Services** > **Library**:
   - search for **Cloud Run Admin API** and click **Enable**;
   - search for **Cloud Identity-Aware Proxy API** and click **Enable**.

## B. Deploy the test service with IAP

1. Menu ☰ > **Cloud Run** > **Services** > **Deploy container**.
2. **Container image URL:** `docker.io/mendhak/http-https-echo:latest`. This public image
   replies with every header it receives.
3. **Service name:** `iap-test`. **Region:** `us-central1`.
4. **Authentication:** choose **Require authentication**, then tick
   **Identity-Aware Proxy (IAP)**.
5. Open **Container(s), Volumes, Networking, Security** and set **Container port** to `8080`.
   Leave Minimum instances at 0 and billing as request-based.
6. Click **Create** and wait for the green tick.
7. Copy the **service URL** shown at the top of the service page.

Turning IAP on from the console gives IAP's service agent the Cloud Run Invoker role
automatically (Google's docs), so there's no separate step for it.

<details>
<summary>Same step with gcloud instead (optional)</summary>

```bash
P=<project-id>; R=us-central1; S=iap-test
gcloud services enable run.googleapis.com iap.googleapis.com --project=$P
gcloud run deploy $S --project=$P --region=$R \
  --image=docker.io/mendhak/http-https-echo:latest --port=8080 \
  --no-allow-unauthenticated --iap
NUM=$(gcloud projects describe $P --format="value(projectNumber)")
gcloud run services add-iam-policy-binding $S --project=$P --region=$R \
  --member=serviceAccount:service-$NUM@gcp-sa-iap.iam.gserviceaccount.com \
  --role=roles/run.invoker
```
</details>

## C. Let yourself in and test in the browser

1. Open the service, then the **Security** tab. Under **Identity-Aware Proxy (IAP)** click
   **Edit policy**.
2. Add principal `yash.patel@pragnakalp.com` and leave the access level empty.
   Click **Save**.
3. Open the service URL in a new tab.

**Expected:** a Google sign-in page, then a JSON page whose `headers` include
`x-goog-iap-jwt-assertion`.

gcloud equivalent:
```bash
gcloud iap web add-iam-policy-binding --project=$P --resource-type=cloud-run \
  --region=$R --service=$S --member=user:yash.patel@pragnakalp.com \
  --role=roles/iap.httpsResourceAccessor
```

## D. Script test, before the allowlist

1. Go to **script.google.com** and click **New project**, signed in as `yash.patel@pragnakalp.com`.
2. Click **Project Settings** (the gear) and tick
   **Show "appsscript.json" manifest file in editor**.
3. Replace `appsscript.json` with:

```json
{
  "timeZone": "Etc/UTC",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/script.external_request"
  ]
}
```

4. Replace `Code.gs` with the code below, putting the service URL from step B in `URL`:

```javascript
const URL = 'https://<service url from B>/';

// The client ID IAP has to allow: the "aud" of the script's ID token.
function showAud() {
  let p = ScriptApp.getIdentityToken().split('.')[1];
  p += '='.repeat((4 - p.length % 4) % 4);
  const c = JSON.parse(Utilities.newBlob(Utilities.base64DecodeWebSafe(p)).getDataAsString());
  Logger.log('aud=' + c.aud + ' email=' + c.email);
}

// Same headers as the add-on's post_: ID token for IAP, access token for our own check.
function callIap() {
  const r = UrlFetchApp.fetch(URL, {
    headers: {
      'Proxy-Authorization': 'Bearer ' + ScriptApp.getIdentityToken(),
      'Authorization': 'Bearer ' + ScriptApp.getOAuthToken()
    },
    followRedirects: false,
    muteHttpExceptions: true
  });
  Logger.log(r.getResponseCode() + ' ' + r.getContentText().slice(0, 4000));
}
```

5. Run **`showAud`** and allow access. In the **Execution log**, copy the `aud=…` value.
   That's the script's client ID.
6. Run **`callIap`**.

**Expected: refused** (302 to sign-in, or 401). With the default Google-managed OAuth
client, IAP accepts no programmatic tokens until a client ID is on the allowlist.

## E. Put the client ID on the allowlist (Cloud Shell)

The allowlist has no console screen; Google's docs give only gcloud or the API for it.

1. In the Cloud console, click **Activate Cloud Shell** (`>_`, top right).
2. Paste the commands below, putting your project ID on the first line and the `aud` from
   step D in the file:

```bash
P=<project-id>
# Read first: `set` replaces the whole list, so keep any IDs already there.
gcloud iap settings get --project=$P --resource-type=cloud-run --region=us-central1 --service=iap-test

cat > iap_settings.yaml <<EOF
access_settings:
  oauth_settings:
    programmatic_clients: ["<aud from D>"]
EOF

gcloud iap settings set iap_settings.yaml --project=$P --resource-type=cloud-run --region=us-central1 --service=iap-test

# Check it took
gcloud iap settings get --project=$P --resource-type=cloud-run --region=us-central1 --service=iap-test
```

If the first `get` already shows a `programmatic_clients` list, copy those IDs into the
file as well.

### E, browser only (API Explorer, no gcloud)

Use this when gcloud or Cloud Shell can't be used. For example, on 2026-11-17 gcloud
was refused with `USER_PROJECT_DENIED`: the account lacked `serviceusage.services.use`
on the test project.

Resource name (the format comes from gcloud's own code,
`api_lib/iap/util.py`, `CloudRun._IapWebId` = `cloud_run-<region>`):

```
projects/<project number>/iap_web/cloud_run-us-central1/services/iap-test
```

1. **Read** the current settings: open
   https://cloud.google.com/iap/docs/reference/rest/v1/TopLevel/getIapSettings
   and use **Try this method** on the right.
   - `name`: the resource name above.
   - Click **Execute** and sign in as `yash.patel@pragnakalp.com`.
   - Expected: 200 with the current settings. Note any `programmaticClients` already there.
2. **Write**: open
   https://cloud.google.com/iap/docs/reference/rest/v1/TopLevel/updateIapSettings
   and use **Try this method**.
   - `iapSettings.name`: the resource name above.
   - `updateMask`: `iapSettings.accessSettings.oauthSettings.programmaticClients`
     (from Google's share-clients page; if it's refused as an invalid mask, try
     `accessSettings.oauthSettings.programmaticClients`).
   - Request body:
     ```json
     {
       "name": "projects/<project number>/iap_web/cloud_run-us-central1/services/iap-test",
       "accessSettings": {
         "oauthSettings": { "programmaticClients": ["<aud from D>"] }
       }
     }
     ```
     Add any IDs from step 1 to the list, because this replaces the whole list.
   - Click **Execute**. Expected: 200, with `programmaticClients` in the reply.
3. Run **getIapSettings** again to confirm.

A 403 here means the account lacks **IAP Settings Admin** on the project.

## F. Script test, after the allowlist

1. Wait a minute or two, then run **`callIap`** again.

**Expected: `200`**, and the logged JSON shows:

| Header | Means |
|---|---|
| `x-goog-iap-jwt-assertion` | IAP let the call in and signed it |
| `authorization: Bearer ya29…` | our add-on's own header still arrives behind IAP |
| `x-goog-authenticated-user-email` | your email |

2. **Extra check:** Cloud Run > `iap-test` > **Security** > **Edit policy**. Remove yourself
   and click **Save**, then run `callIap`. **Expected: 403.** Add yourself back afterwards.

3. **Optional server check:** run the `x-goog-iap-jwt-assertion` value through our own
   `service/iap.py`, from the project folder:

```bash
.venv/bin/python -c "
from service.iap import verified_email
print(verified_email({'x-goog-iap-jwt-assertion': '<value>'},
      '/projects/<project number>/locations/us-central1/services/iap-test'))"
```

**Expected:** your email is printed. The assertion lasts about 10 minutes, so run this
soon after `callIap`.

## G. Clean up

1. Cloud Run > **Services** > tick `iap-test` > **Delete**.
2. Optional: delete the Apps Script test project at script.google.com.

The execution logs hold your own sign-in tokens. They expire in about an hour, but don't
paste them anywhere public.

---

## Results

| Check | Expected | Seen |
|---|---|---|
| C: browser, signed in | sign-in, then JSON with `x-goog-iap-jwt-assertion` | |
| D: `callIap` before the allowlist | 302 or 401 | |
| F: `callIap` after the allowlist | 200 | |
| F: `authorization` header reaches the service | present | |
| F: user removed from the policy | 403 | |
| F: `service/iap.py` check | prints the email | |

## If F passes: what the client does on sourcing-ai-dev

Project `skylink-material-planning`, region `us-central1`, service `sourcing-ai-dev`:

1. Cloud Run > `sourcing-ai-dev` > **Security** > **Edit policy**: add the tester group
   (`sourcing-addon-test@skylinkintl.com`). This is the same role,
   `roles/iap.httpsResourceAccessor`.
2. Cloud Shell: `gcloud iap settings get … --service=sourcing-ai-dev` first. Then `set`
   with the existing IDs **plus** the add-on's client ID
   `1011595872116-n7pglcfpupq3o51ju3jcj00vd5q979em.apps.googleusercontent.com`.

Nothing else on the service changes: IAP stays on, and the same people stay allowed.
