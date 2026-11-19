# SourcingAI: Google sign-in (IAP) steps

For Skylink's Google Cloud administrator · 17 November 2026

These steps let the SourcingAI add-on reach the SourcingAI service on **sourcing-ai-dev**
while Google sign-in (IAP) stays on. Nothing is changed by us; every step below is yours to
run, and each one can be undone.

The Google Workspace Admin console part (test group and add-on install) is in the separate
document *SourcingAI_Admin_Console_Steps*.

## Your questions

**Which Cloud project holds the add-on listing?**
`sourcing-skylink-addon` (project number 1011595872116), in Skylink's organization. It
holds the add-on's Marketplace listing, its Google sign-in client, and the Firestore
database that passes rows and results between the add-on and the service. No billing and
no secrets are in it. It is not `skylink-material-planning`.

**Which group is the test group, and who is in it?**
`SourcingAI testers`, `sourcing-addon-test@skylinkintl.com` (created in the Admin console
steps). Members: <MEMBERS>.

The group is a plain Google Workspace group, used here only as a list of names: no
mailing list or shared inbox is needed. It does two jobs:
- the add-on is installed for the group's members only;
- IAP lets the group's members through.

Adding or removing a tester then means changing the group once; the add-on install and
IAP follow. The group is a convenience, not a requirement: see
[Without a group](#without-a-group).

**What exactly changes in IAP, and on which service?**
Two things, both on the Cloud Run service **sourcing-ai-dev** (project
`skylink-material-planning`, region `us-central1`):
1. The test group is added to who may pass Google sign-in.
2. The add-on's Google sign-in client ID is added to the list of clients IAP accepts from
   scripts (IAP's "programmatic clients").

IAP stays on. Nobody who has access today loses it. Nothing else on the service or the
project changes.

## Summary

| # | What | Where | Why |
|---|---|---|---|
| 1 | Allow the test group through IAP | Cloud Run > sourcing-ai-dev > Security | Testers' add-on calls are refused without it |
| 2 | Allow the add-on's client ID | Cloud Shell (no console screen for it) | IAP refuses calls from a script unless its client is listed |

Roles needed by the person running the steps: IAP Policy Admin (step 1) and IAP Settings
Admin (step 2), or Owner.

## Step 1: Allow the test group through IAP

1. Console > **Cloud Run** > **sourcing-ai-dev** > **Security** tab.
2. Under **Identity-Aware Proxy (IAP)**, click **Edit policy**.
3. Add principal `sourcing-addon-test@skylinkintl.com`. Leave the access level empty.
4. Click **Save**.

Same with gcloud:
```bash
gcloud iap web add-iam-policy-binding --project=skylink-material-planning \
  --resource-type=cloud-run --region=us-central1 --service=sourcing-ai-dev \
  --member=group:sourcing-addon-test@skylinkintl.com \
  --role=roles/iap.httpsResourceAccessor
```

**Check:** `gcloud iap web get-iam-policy --project=skylink-material-planning --resource-type=cloud-run --region=us-central1 --service=sourcing-ai-dev`
lists the group with `roles/iap.httpsResourceAccessor`.

**Undo:** Edit policy > remove the group > Save (or `remove-iam-policy-binding` with the
same flags).

## Step 2: Allow the add-on's client ID

The add-on proves who the person is with a Google ID token issued for its own sign-in
client. IAP accepts such tokens only from clients on this list. In our test, a script whose
client was not listed was refused with 401.

Run in **Cloud Shell** (console, top right `>_`), in project `skylink-material-planning`:

```bash
# 1. Read the current settings first
gcloud iap settings get --project=skylink-material-planning \
  --resource-type=cloud-run --region=us-central1 --service=sourcing-ai-dev
```

If the output already has `programmatic_clients`, keep those IDs in the list below:
`set` replaces the whole list.

```bash
# 2. Write the list (existing IDs + the add-on's client ID)
cat > iap_settings.yaml <<EOF
access_settings:
  oauth_settings:
    programmatic_clients:
    - 1011595872116-n7pglcfpupq3o51ju3jcj00vd5q979em.apps.googleusercontent.com
EOF

gcloud iap settings set iap_settings.yaml --project=skylink-material-planning \
  --resource-type=cloud-run --region=us-central1 --service=sourcing-ai-dev

# 3. Check
gcloud iap settings get --project=skylink-material-planning \
  --resource-type=cloud-run --region=us-central1 --service=sourcing-ai-dev
```

**Check:** the output lists the client ID under `programmatic_clients`.

**Undo:** run `set` again with the list as it was in step 1.

Google requires the listed client to belong to a project in an organization.
`sourcing-skylink-addon` is in Skylink's organization, so this is met.

## Without a group

If you'd rather not create a group, the testers can be named one by one instead.

**Add-on:** no Admin install. Each tester installs SourcingAI themselves: in Google Sheets,
**Extensions** > **Add-ons** > **Get add-ons** > search **SourcingAI** > **Install**. This
works if your Marketplace settings let users install apps (Admin console > Apps >
Google Workspace Marketplace apps > Settings). If they allow only allowlisted apps, add
SourcingAI there once: Apps list > **Allowlist** > **Add app to allowlist**.

**IAP Step 1:** add each tester instead of the group, for example
`chaitali@skylinkintl.com` in **Edit policy**, or with gcloud
`--member=user:chaitali@skylinkintl.com`. Run it once per tester.

**IAP Step 2:** unchanged.

The automatic start works the same way: it needs the published add-on to be installed,
whether by an admin or by the person.

Other ways, for comparison:

| Way | Add-on install | IAP | Note |
|---|---|---|---|
| Group (main steps above) | Admin install for the group | the group | one list for both |
| Named testers (this section) | each tester installs it | each tester | IAP changes for every new tester |
| Existing organizational unit | Admin install for the unit | each person (IAP can't take a unit) | everyone in the unit gets the add-on |
| Whole company (later, at go-live) | Admin install for everyone | `domain:skylinkintl.com` | too wide for a test |

In every case, the SourcingAI service itself accepts only `@skylinkintl.com` users.

## When

Steps 1 and 2 can be done now. They take effect when our code is on sourcing-ai-dev. We'll
then make the first test call from the add-on and confirm it gets through.

If the first call is refused, nothing breaks: the add-on shows "Google sign-in (IAP) did
not let the call through", and each step can be undone as above.

## What we checked beforehand

We ran the same IAP setup on a test Cloud Run service in our own project:
- Google sign-in in the browser let through only the people on the access list.
- A script's call was refused until its client was allowed.
- The IAP command and settings format above were accepted by Google.
- The SourcingAI service's own check correctly read the person's email from IAP's signed
  header, and refused it when the header was for another service or had been changed.

One step could not be finished there: our test project is not in an organization, and
Google refused to list a client from such a project. Skylink's projects are in an
organization, so this restriction does not apply to the steps above.
