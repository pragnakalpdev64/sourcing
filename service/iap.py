"""Who IAP let through, for the service behind Google sign-in (client, 2026-11-13:
"Google sign-in stays on. Show me the script gets through it").

On sourcing-ai-dev, IAP stands in front of the service. The add-on gets through it
with the user's Google ID token (ScriptApp.getIdentityToken, in Proxy-Authorization
so its access token stays in Authorization for our own add-on check). IAP then
signs every request it lets through with X-Goog-IAP-JWT-Assertion (ES256, Google's
IAP keys, audience = this service).

The email is taken only from that signed token, after its signature, issuer,
audience and expiry are checked; never from the plain X-Goog-Authenticated-User-Email
header, which anyone reaching the service another way could send. Same rule as the
login update page's iap.py in mie-agent (sourcing_credentials_web/iap.py).

IAP_AUDIENCE: /projects/<project number>/locations/<region>/services/<service>
(IAP on Cloud Run itself) or /projects/<project number>/global/backendServices/<id>
(IAP on a load balancer). Empty = no IAP in front (local runs through ngrok).
"""
from __future__ import annotations

import threading
import time

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"   # {key id: PEM}, rotated by Google
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
PLAIN_EMAIL_HEADER = "x-goog-authenticated-user-email"
_KEYS_TTL = 3600
_keys: dict = {"pem": None, "at": 0.0}
_keys_lock = threading.Lock()


class IapRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _fetch_keys() -> dict:
    import httpx

    resp = httpx.get(IAP_KEYS_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _iap_keys(fresh: bool = False) -> dict:
    with _keys_lock:
        now = time.monotonic()
        if fresh or _keys["pem"] is None or now - _keys["at"] > _KEYS_TTL:
            _keys["pem"], _keys["at"] = _fetch_keys(), now
        return _keys["pem"]


def _decode(token: str, audience: str, keys: dict) -> dict:
    from google.auth import jwt

    return jwt.decode(token, certs=keys, audience=audience, clock_skew_in_seconds=30)


def verified_email(headers, audience: str) -> str:
    """The email IAP signed for this request, lowercased; else IapRefused.
    Blocking (may fetch Google's keys): call it off the event loop."""
    token = headers.get(ASSERTION_HEADER, "")
    if not token:
        raise IapRefused(401, "request did not come through Google sign-in (IAP)")
    try:
        keys = _iap_keys()
    except Exception:
        raise IapRefused(503, "IAP keys not available; try again in a minute")
    try:
        claims = _decode(token, audience, keys)
    except Exception:
        # A new key id after Google rotated them: fetch once more before refusing.
        try:
            claims = _decode(token, audience, _iap_keys(fresh=True))
        except Exception:
            raise IapRefused(401, "IAP sign-in could not be checked")
    if claims.get("iss") != IAP_ISSUER:
        raise IapRefused(401, "IAP sign-in could not be checked")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise IapRefused(401, "IAP sign-in carries no email")
    plain = headers.get(PLAIN_EMAIL_HEADER, "")
    if plain and plain.rpartition(":")[2].strip().lower() != email:
        raise IapRefused(401, "IAP sign-in could not be checked")
    return email
