"""Search parts on ILS (ILSmart) through its SOAP web service, with the login kept safe.

ILS has no token or session: every call carries UserId + Password in the SOAP
body (SDK v2.3, live WSDL 2026-10-19). We cannot change that, so this script
protects the password on our side:

  - the password comes only from Secret Manager (`sourcing-vendor-ils`), never
    from argv (visible in `ps` / shell history), .env or code;
  - HTTPS only, certificate checked, TLS 1.2 or newer, redirects refused (a
    307/308 would resend the body, password included, to another host);
  - the request body is never logged or saved; any text we print or save is
    scrubbed of the password in case ILS ever echoes it in a fault;
  - responses with a DOCTYPE/ENTITY are rejected (XML entity attacks) and
    capped in size; part numbers are checked before going into the XML.

  python scripts/ils_part_search.py 3214552-1                  # TEST MODE, sample data
  python scripts/ils_part_search.py 3214552-1 A100 --condition NE,OH --qty 2
  python scripts/ils_part_search.py --live 3214552-1 --out ils.csv   # BILLED per part

Prints one row per supplier listing (GetPartsAvailability gives no price);
--out writes them as .csv or .json.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ils_probe import (  # noqa: E402  same SOAP shapes, one parser
    ARRAYS_NS, ENDPOINT, NS, TEST_PASSWORD, TEST_USER, as_list, availability_rows,
    faults_of, print_table, to_obj,
)

SOAP_ENV = "{http://schemas.xmlsoap.org/soap/envelope/}"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # a big part has a few hundred listings, well under this
# Letters, digits and the separators seen in aviation part numbers.
PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-/#]{0,39}$")
CONDITION_RE = re.compile(r"^[A-Z]{2}$")
COLS = ["part_entered", "supplier", "supplier_id", "avl", "part_number", "alt_pn",
        "condition", "qty", "description", "contact", "email", "phone", "state"]


class IlsError(Exception):
    pass


class _Tls12Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()  # verifies the certificate and host name
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class IlsClient:
    """Holds the login in memory only; repr/str never show the password."""

    def __init__(self, user: str, password: str, timeout: float = 60):
        if not ENDPOINT.startswith("https://"):
            raise IlsError("ILS endpoint must be https")
        self._user, self._password, self.timeout = user, password, timeout
        self._http = requests.Session()
        self._http.trust_env = False  # no proxy / CA bundle picked up from the environment
        self._http.mount("https://", _Tls12Adapter())

    def __repr__(self) -> str:
        return f"IlsClient(user={self._user!r})"

    def scrub(self, text: str) -> str:
        if self._password:
            text = text.replace(self._password, "***")
            text = text.replace(escape(self._password), "***")
        return text

    def parts_availability(self, part: str, qty: int | None = None,
                           conditions: list[str] | None = None) -> list[dict]:
        if not PART_RE.match(part):
            raise IlsError(f"part number {part!r} has characters ILS part numbers don't use")
        conds = ""
        if conditions:
            items = "".join(f"<a:string>{escape(c)}</a:string>" for c in conditions)
            conds = f'<ConditionCodes xmlns:a="{ARRAYS_NS}">{items}</ConditionCodes>'
        # Field order follows the WSDL (the SDK says order matters).
        fields = (conds + f"<PartNumber>{escape(part)}</PartNumber>"
                  + f"<Password>{escape(self._password)}</Password>"
                  + (f"<Quantity>{qty}</Quantity>" if qty else "")
                  + f"<UserId>{escape(self._user)}</UserId>")
        body = self._call("GetPartsAvailability", fields)
        fatal = [f for f in faults_of(body) if f.get("Severity") == "Fatal"]
        if fatal:
            f = fatal[0]
            raise IlsError(self.scrub(f"ILS fault {f.get('Name')}: {f.get('Message')}"))
        return availability_rows(body)

    def _call(self, op: str, fields: str) -> dict:
        envelope = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    f'<{op} xmlns="{NS}">{fields}</{op}></s:Body></s:Envelope>')
        try:
            r = self._http.post(ENDPOINT, data=envelope.encode("utf-8"), timeout=self.timeout,
                                allow_redirects=False, stream=True,
                                headers={"Content-Type": "text/xml; charset=utf-8",
                                         "SOAPAction": f'"{NS}/{op}"'})
        except requests.RequestException as exc:
            # The exception text names the URL, not the body; scrub anyway.
            raise IlsError(self.scrub(f"{op}: request failed: {type(exc).__name__}: {exc}")) from None
        del envelope
        with r:
            if 300 <= r.status_code < 400:
                raise IlsError(f"{op}: ILS answered with a redirect ({r.status_code}); not followed")
            raw = r.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IlsError(f"{op}: response larger than {MAX_RESPONSE_BYTES} bytes")
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise IlsError(f"{op}: response has a DOCTYPE/ENTITY, refused")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise IlsError(self.scrub(f"{op}: HTTP {r.status_code}, not XML: "
                                      f"{raw[:200].decode('utf-8', 'replace')!r}")) from None
        soap_body = root.find(SOAP_ENV + "Body")
        if soap_body is None or not len(soap_body):
            raise IlsError(f"{op}: HTTP {r.status_code}, no SOAP body")
        fault = soap_body.find(SOAP_ENV + "Fault")
        if fault is not None:
            raise IlsError(self.scrub(f"{op}: SOAP fault {json.dumps(to_obj(fault))[:300]}"))
        obj = to_obj(soap_body[0])
        # Responses wrap the result in <Body><Faults/>...</Body> (seen 2026-10-19).
        return obj.get("Body", obj) if isinstance(obj, dict) else {}


def live_login() -> tuple[str, str]:
    from sourcing.config import get_settings
    from sourcing.credentials.gsm import SecretManagerProvider

    cred = asyncio.run(SecretManagerProvider(get_settings().gcp_project).resolve("ils", ""))
    return cred.username, cred.password


def save(rows: list[dict], path: str) -> None:
    # 0600: the file holds supplier contacts, not for other users of the machine.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        if path.endswith(".json"):
            json.dump(rows, fh, indent=1)
        else:
            w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("parts", nargs="+", help="part number(s) to search")
    ap.add_argument("--condition", type=lambda s: [c.strip().upper() for c in s.split(",") if c.strip()],
                    help="only these condition codes, e.g. NE,OH,SV")
    ap.add_argument("--qty", type=int, help="minimum quantity")
    ap.add_argument("--live", action="store_true",
                    help="Skylink's ILS login from Secret Manager (every part is BILLED)")
    ap.add_argument("--out", help="write rows to this .csv or .json file")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()

    bad = [c for c in args.condition or [] if not CONDITION_RE.match(c)]
    if bad:
        print(f"Condition codes are two letters (NE, OH, SV, AR...): {', '.join(bad)}")
        return 2
    if args.live:
        from sourcing.credentials.base import CredentialError
        try:
            user, password = live_login()
        except CredentialError as exc:
            print(f"No live ILS login: {exc}. The client creates the secret "
                  '`sourcing-vendor-ils` (JSON {"username": ..., "password": ...}).')
            return 2
        print(f"LIVE account {user}: {len(args.parts)} billed search(es).")
    else:
        user, password = TEST_USER, TEST_PASSWORD
        print("TEST MODE (TESTU01): ILS sample data, not billed.")

    client = IlsClient(user, password, timeout=args.timeout)
    del password
    rows, failed = [], 0
    for part in args.parts:
        t0 = time.monotonic()
        try:
            found = client.parts_availability(part, args.qty, args.condition)
        except IlsError as exc:
            print(f"  {part:20} ERROR {exc}")
            failed += 1
            continue
        print(f"  {part:20} {len(found):3} listing(s) from "
              f"{len({r['supplier_id'] for r in found})} supplier(s) "
              f"in {time.monotonic() - t0:.1f}s")
        rows += [{**r, "part_entered": r.get("part_entered") or part} for r in found]

    if rows:
        print()
        print_table(rows, ["part_entered", "supplier", "avl", "part_number", "condition",
                           "qty", "email"])
    if args.out:
        save(rows, args.out)
        print(f"\n{len(rows)} row(s) -> {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
