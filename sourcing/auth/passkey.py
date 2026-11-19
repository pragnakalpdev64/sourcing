"""A passkey the program holds itself: Chrome's virtual WebAuthn authenticator.

Why (2026-11-18): Aviall (Boeing) now asks for a passkey after the password. A
passkey on a team member's phone or laptop can't be copied out, and a phone can't
answer a cloud browser (it must be near that computer). So the program gets its
own passkey: Chrome's virtual authenticator (DevTools protocol, WebAuthn domain)
creates it once while a person, signed in, adds a passkey in the site's account
settings; its private key is kept like a password; every run loads it into a
fresh virtual authenticator and the site's passkey request is answered at once.
Deleting that passkey in the site's settings ends the program's access.

Plan step 7 (passkey spike, plan 6.6 option 2). Tried first on our own Notion
account (scripts/passkey_spike.py), not on the client's.

The credential dict holds a private key: never logged, only its id and site.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("sourcing.passkey")

# A platform authenticator ("this device") that has verified the user and
# answers without a click: what a run needs.
AUTHENTICATOR_OPTIONS = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}
# The fields WebAuthn.addCredential takes back.
CREDENTIAL_FIELDS = ("credentialId", "isResidentCredential", "rpId", "privateKey",
                     "userHandle", "signCount")


def web_id(credential_id: str) -> str:
    """The id as the page sees it (base64url, no padding); the DevTools protocol
    gives standard base64."""
    return credential_id.replace("+", "-").replace("/", "_").rstrip("=")


def describe(cred: dict) -> str:
    """For logs: the site and the start of the id, never the key."""
    return f"{cred.get('rpId')} id {str(cred.get('credentialId', ''))[:10]}… " \
           f"(uses {cred.get('signCount', 0)})"


def merge(saved: list[dict], current: list[dict]) -> list[dict]:
    """The saved passkeys with the authenticator's current ones laid over them
    (same id: the newer use count; new id: added). Saved back to the secret."""
    out = {c["credentialId"]: dict(c) for c in saved if c.get("credentialId")}
    for c in current:
        if c.get("credentialId"):
            out[c["credentialId"]] = dict(c)
    return list(out.values())


def newest_per_account(creds: list[dict]) -> list[dict]:
    """One passkey per site and account: the authenticator refuses a second one
    for the same user ("An error occurred trying to create the credential", seen
    2026-11-18 with Notion's local- and Browserbase-made passkeys). The last in
    the list is the newest (merge() appends new ones)."""
    out: dict = {}
    for c in creds:
        out.pop((c.get("rpId"), c.get("userHandle")), None)
        out[(c.get("rpId"), c.get("userHandle"))] = c
    return list(out.values())


class VirtualPasskeys:
    """The virtual authenticator of one page (it belongs to that tab; a pop-up
    or new tab has none)."""

    def __init__(self, cdp, authenticator_id: str) -> None:
        self.cdp = cdp
        self.authenticator_id = authenticator_id
        self.added: list[str] = []        # credential ids (web_id) the site registered
        self.asserted: list[str] = []     # credential ids (web_id) used to sign in

    @classmethod
    async def attach(cls, page, credentials: Optional[list[dict]] = None) -> "VirtualPasskeys":
        cdp = await page.context.new_cdp_session(page)
        # enableUI False: no Chrome passkey dialog, the authenticator answers.
        await cdp.send("WebAuthn.enable", {"enableUI": False})
        reply = await cdp.send("WebAuthn.addVirtualAuthenticator",
                               {"options": AUTHENTICATOR_OPTIONS})
        keys = cls(cdp, reply["authenticatorId"])
        cdp.on("WebAuthn.credentialAdded", keys._on_added)
        cdp.on("WebAuthn.credentialAsserted", keys._on_asserted)
        for cred in newest_per_account(credentials or []):
            try:
                await keys.add(cred)
            except Exception as exc:          # one bad passkey must not stop the others
                logger.warning("Passkey not loaded: %s (%s)", describe(cred), exc)
        return keys

    def _on_added(self, event: dict) -> None:
        cred = event.get("credential") or {}
        self.added.append(web_id(cred.get("credentialId", "")))
        logger.info("Passkey created by the site: %s", describe(cred))

    def _on_asserted(self, event: dict) -> None:
        cred = event.get("credential") or {}
        self.asserted.append(web_id(cred.get("credentialId", "")))
        logger.info("Passkey used to sign in: %s", describe(cred))

    async def add(self, cred: dict) -> None:
        await self.cdp.send("WebAuthn.addCredential", {
            "authenticatorId": self.authenticator_id,
            "credential": {k: cred[k] for k in CREDENTIAL_FIELDS if k in cred}})
        logger.info("Passkey loaded: %s", describe(cred))

    async def credentials(self) -> list[dict]:
        """Every passkey in the authenticator, with its current use count (save
        it back after a run: some sites refuse a count that didn't go up)."""
        reply = await self.cdp.send("WebAuthn.getCredentials",
                                    {"authenticatorId": self.authenticator_id})
        return [{k: c[k] for k in CREDENTIAL_FIELDS if k in c}
                for c in reply.get("credentials", [])]
