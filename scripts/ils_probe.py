"""Try the ILS (ILSmart) web services that matter for SourcingAI.

ILS has a SOAP 1.1 API (ILS Web Services SDK v2.3, plus newer operations seen
in the live WSDL on 2026-10-19). This script calls the read-only operations we
might use and prints what comes back, so we can see the real data before
writing an adapter. It never sends RFQs or quotes and never changes listings.

Credentials:
  default   ILS TEST MODE (TESTU01 / WEBSERVICETEST from the SDK). Static
            sample data, not billed. Good for checking the requests work.
  --live    Skylink's account from Secret Manager (`sourcing-vendor-ils`,
            JSON {"username", "password"}). Every search is BILLED 1:1.

  python scripts/ils_probe.py                          # all sourcing ops, test mode
  python scripts/ils_probe.py --part 3214552-1 --condition NE,OH --qty 2
  python scripts/ils_probe.py --ops GetPartsAvailability,GetFmvPricing
  python scripts/ils_probe.py --group polling          # quotes / RFQs received
  python scripts/ils_probe.py --list                   # what each op is for
  python scripts/ils_probe.py --live --ops GetPartsAvailability --part X

Raw XML and parsed JSON of every response go to debug_pages/ils/ (not committed).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENDPOINT = "https://secure.ilsmart.com/services/v1/soap11"
NS = "http://namespace.ilsmart.com/v1"
ARRAYS_NS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
TEST_USER, TEST_PASSWORD = "TESTU01", "WEBSERVICETEST"

# op -> (group, billed search?, what it gives us). Field order in each request
# follows the WSDL (the SDK says order matters).
OPS = {
    "Login": ("check", False, "Checks the user id / password only."),
    "GetPartsAvailability": ("sourcing", True,
        "Main search: suppliers listing the part, qty, condition, contacts. NO price."),
    "IsPartAvailable": ("sourcing", True, "Yes/no: is the part listed anywhere on ILS."),
    "GetPartInventorySummary": ("sourcing", True, "Summary counts of listings for the part."),
    "GetPartListing": ("sourcing", False, "Skylink's OWN ILS listing of the part (has price)."),
    "GetFmvPricing": ("pricing", True, "Fair Market Value statistics (WS Fair Market Value)."),
    "GetPartStatsAndPricing": ("pricing", True,
        "Part Stats: inventory/overhaul stats, published pricing, quote history in one call."),
    "GetPartQuoteHistory": ("pricing", True, "Last 10 quotes sent through ILS for the part."),
    "GetPartPublishedPricing": ("pricing", True, "Manufacturer's published (list) price."),
    # "since last call" cursors: on a live account a call consumes the new items,
    # so another system (ERP) polling the same account would miss them.
    "GetNewPartQuotesReceived": ("polling", False,
        "Quotes received for RFQs Skylink sent (Buyer Bundle). Advances a cursor."),
    "GetNewRfqsReceived": ("polling", False,
        "RFQs customers sent Skylink (Supplier Bundle, Customer RFQ). Advances a cursor."),
}
GROUPS = {"sourcing": ["Login", "GetPartsAvailability", "IsPartAvailable",
                       "GetPartInventorySummary", "GetPartListing",
                       "GetFmvPricing", "GetPartStatsAndPricing",
                       "GetPartQuoteHistory", "GetPartPublishedPricing"],
          "polling": ["GetNewPartQuotesReceived", "GetNewRfqsReceived"]}
GROUPS["all"] = GROUPS["sourcing"] + GROUPS["polling"]


def _el(name: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    return f"<{name}>{escape(str(value))}</{name}>"


def build_body(op: str, user: str, password: str, args) -> str:
    part = args.part
    if op == "GetPartsAvailability":
        conds = ""
        if args.condition:
            items = "".join(f"<a:string>{escape(c)}</a:string>" for c in args.condition)
            conds = f'<ConditionCodes xmlns:a="{ARRAYS_NS}">{items}</ConditionCodes>'
        fields = (conds + _el("PartNumber", part) + _el("Password", password)
                  + _el("Quantity", args.qty) + _el("UserId", user))
    elif op in ("GetPartStatsAndPricing", "GetPartQuoteHistory", "GetPartPublishedPricing"):
        fields = (_el("ExactMatch", args.exact) + _el("PartNumber", part)
                  + _el("Password", password) + _el("UserId", user))
    elif op == "GetNewPartQuotesReceived":
        fields = (_el("IncludeQuotesForClosedRfqs", False) + _el("Password", password)
                  + _el("UserId", user))
    elif op == "GetNewRfqsReceived":
        fields = _el("IncludeClosed", False) + _el("Password", password) + _el("UserId", user)
    elif op == "Login":
        fields = _el("Password", password) + _el("UserId", user)
    else:  # PartNumber, Password, UserId
        fields = _el("PartNumber", part) + _el("Password", password) + _el("UserId", user)
    return ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<{op} xmlns="{NS}">{fields}</{op}></s:Body></s:Envelope>')


def to_obj(el: ET.Element):
    """XML -> plain dict/list/str, namespaces dropped, nil -> None."""
    if el.get(NIL) == "true":
        return None
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    out: dict = {}
    for k in kids:
        name = k.tag.split("}")[-1]
        val = to_obj(k)
        if name in out:
            # Repeated tag -> list (to_obj itself never returns a list).
            if not isinstance(out[name], list):
                out[name] = [out[name]]
            out[name].append(val)
        else:
            out[name] = val
    return out


def as_list(v) -> list:
    if v in (None, "", {}):
        return []
    return v if isinstance(v, list) else [v]


def faults_of(body) -> list[dict]:
    if not isinstance(body, dict):
        return []
    f = body.get("Faults") or {}
    return [x for x in as_list(f.get("Fault") if isinstance(f, dict) else f) if x]


def call(op: str, user: str, password: str, args) -> tuple[int, str, float]:
    t0 = time.monotonic()
    r = requests.post(ENDPOINT, data=build_body(op, user, password, args).encode("utf-8"),
                      headers={"Content-Type": "text/xml; charset=utf-8",
                               "SOAPAction": f'"{NS}/{op}"'},
                      timeout=args.timeout)
    return r.status_code, r.text, time.monotonic() - t0


def parse(op: str, xml: str):
    root = ET.fromstring(xml)
    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    soap_fault = body.find("{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if soap_fault is not None:
        return {"SoapFault": to_obj(soap_fault)}
    resp = body[0]
    obj = to_obj(resp)
    # Responses wrap the result in <Body><Faults/>...</Body> (seen 2026-10-19).
    return obj.get("Body", obj) if isinstance(obj, dict) else obj


def contact_of(company: dict) -> dict:
    for key in ("PartsRfqContact", "SalesContact", "MrosRfqContact", "MroRfqContact"):
        c = company.get(key) or {}
        if isinstance(c, dict) and c.get("Email"):
            phone = c.get("Phone") or {}
            return {"contact": c.get("Name") or "", "email": c.get("Email") or "",
                    "phone": (phone.get("PhoneNumber") or "") if isinstance(phone, dict) else ""}
    return {"contact": "", "email": "", "phone": ""}


def availability_rows(body: dict) -> list[dict]:
    """GetPartsAvailability -> one flat row per supplier listing (Results-tab shape)."""
    rows = []
    outer = body.get("PartListings") or {}
    for listing in as_list(outer.get("PartListings") if isinstance(outer, dict) else outer):
        company = listing.get("Company") or {}
        parts = listing.get("Parts") or {}
        for p in as_list(parts.get("PartSearchResult") or parts.get("PartsSearchResults")):
            rows.append({
                "supplier": company.get("Name") or "",
                "supplier_id": company.get("Id") or "",
                "avl": company.get("AccreditedVendorLevel") or "",
                "cage": company.get("SupplierCAGE") or "",
                "state": (company.get("CompanyAddress") or {}).get("StateProvince") or "",
                **contact_of(company),
                "part_number": p.get("PartNumber") or "",
                "alt_pn": p.get("AlternatePartNumber") or "",
                "condition": p.get("ConditionCode") or "",
                "qty": p.get("Quantity") or "",
                "description": p.get("Description") or "",
                "preferred": p.get("IsPreferredVendor") or "",
                "search_part_id": p.get("SearchPartId") or "",
                "part_entered": p.get("PartEntered") or "",
                "note": company.get("AdditionalInfo") or "",
            })
    return rows


def print_table(rows: list[dict], cols: list[str]) -> None:
    widths = {c: min(28, max(len(c), *(len(str(r.get(c, ""))) for r in rows))) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, ""))[:28].ljust(widths[c]) for c in cols))


def summarize(op: str, body) -> None:
    if isinstance(body, dict) and "SoapFault" in body:
        print(f"  SOAP fault: {json.dumps(body['SoapFault'])[:400]}")
        return
    for f in faults_of(body):
        print(f"  FAULT {f.get('Severity')} {f.get('Name')}/{f.get('SubType') or '-'}: "
              f"{f.get('Message')}")
    if op == "GetPartsAvailability":
        rows = availability_rows(body)
        print(f"  {len(rows)} listing(s) from "
              f"{len({r['supplier_id'] for r in rows})} supplier(s)")
        if rows:
            print_table(rows, ["supplier", "supplier_id", "avl", "part_number", "alt_pn",
                               "condition", "qty", "email", "search_part_id"])
        return
    rest = {k: v for k, v in body.items() if k != "Faults"} if isinstance(body, dict) else body
    text = json.dumps(rest, indent=1)
    print("  " + (text[:1500] + "\n  ... (full JSON in the saved file)"
                  if len(text) > 1500 else text).replace("\n", "\n  "))


def live_credentials() -> tuple[str, str]:
    from sourcing.config import get_settings
    from sourcing.credentials.gsm import SecretManagerProvider

    cred = asyncio.run(SecretManagerProvider(get_settings().gcp_project).resolve("ils", ""))
    return cred.username, cred.password


def sheet_rows(path: str) -> list[dict]:
    """Requests tab of a local request sheet -> [{rfq_id, part, qty, condition}]."""
    import openpyxl

    ws = openpyxl.load_workbook(path, read_only=True)["Requests"]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    out = []
    for r in rows:
        rec = dict(zip(header, r))
        part = str(rec.get("Part Number") or "").strip()
        if part:
            out.append({"rfq_id": str(rec.get("RFQ ID") or ""), "part": part,
                        "qty": str(rec.get("Qty Needed") or "").strip(),
                        "condition": str(rec.get("Condition Wanted") or "").strip().upper()})
    return out


SHEET_OPS = ["GetPartsAvailability", "GetFmvPricing"]
SHEET_COLS = ["RFQ ID", "Requested Part", "Part Entered (ILS echo)", "Supplier", "Supplier ID",
              "AVL", "Part Number", "Alt PN", "Condition", "Qty Available", "Qty Needed",
              "Description", "Contact Email", "Phone", "Search Part ID",
              "FMV", "FMV Low", "FMV High", "FMV Condition", "Fetched At"]


def run_sheet(path: str, user: str, password: str, args, stamp: str) -> int:
    """Search every part in the sheet like a run would, one billed call per part per op.

    In TEST MODE ILS answers every part with the same sample data, so this only
    proves the loop and the mapping to Results columns, not real availability.
    """
    import csv

    parts = sheet_rows(path)
    print(f"{len(parts)} part(s) from {path}; ops: {', '.join(SHEET_OPS)} "
          f"= {len(parts) * len(SHEET_OPS)} call(s)\n")
    results, summary, failed = [], [], 0
    for i, p in enumerate(parts, 1):
        a = argparse.Namespace(**{**vars(args), "part": p["part"],
                                  "qty": int(p["qty"]) if p["qty"].isdigit() else None,
                                  "condition": [p["condition"]] if p["condition"] else None})
        fetched = time.strftime("%Y-%m-%dT%H:%M:%S")
        got, secs_total, errors = {}, 0.0, []
        for op in SHEET_OPS:
            try:
                status, xml, secs = call(op, user, password, a)
                secs_total += secs
                with open(os.path.join(args.out, f"{stamp}_{i:02d}_{op}.xml"), "w",
                          encoding="utf-8") as fh:
                    fh.write(xml)
                body = parse(op, xml)
            except (requests.RequestException, ET.ParseError) as exc:
                errors.append(f"{op}: {exc!r}"[:120])
                continue
            fatal = [f for f in faults_of(body) if f.get("Severity") == "Fatal"]
            if status != 200 or "SoapFault" in body or fatal:
                msg = fatal[0].get("Message") if fatal else body.get("SoapFault", status)
                errors.append(f"{op}: {msg}"[:120])
            got[op] = body
        listings = availability_rows(got.get("GetPartsAvailability") or {})
        fmv_node = ((got.get("GetFmvPricing") or {}).get("FmvResults") or {})
        fmvs = as_list(fmv_node.get("FmvPricing") if isinstance(fmv_node, dict) else None)
        fmv = fmvs[0] if fmvs else {}
        echo = sorted({r["part_entered"] for r in listings if r.get("part_entered")})
        for r in listings:
            results.append({
                "RFQ ID": p["rfq_id"], "Requested Part": p["part"],
                "Part Entered (ILS echo)": r.get("part_entered", ""),
                "Supplier": r["supplier"], "Supplier ID": r["supplier_id"], "AVL": r["avl"],
                "Part Number": r["part_number"], "Alt PN": r["alt_pn"],
                "Condition": r["condition"], "Qty Available": r["qty"], "Qty Needed": p["qty"],
                "Description": r["description"], "Contact Email": r["email"],
                "Phone": r["phone"], "Search Part ID": r["search_part_id"],
                "FMV": fmv.get("Fmv", ""), "FMV Low": fmv.get("LowFmv", ""),
                "FMV High": fmv.get("HighFmv", ""), "FMV Condition": fmv.get("ConditionCode", ""),
                "Fetched At": fetched})
        summary.append({"#": i, "requested": p["part"], "cond": p["condition"] or "-",
                        "qty": p["qty"], "listings": len(listings),
                        "suppliers": len({r["supplier_id"] for r in listings}),
                        "ils_echo": ",".join(echo)[:20] or "-",
                        "fmv": fmv.get("Fmv", "-") or "-",
                        "fmv_part": fmv.get("PartNumber", "-") or "-",
                        "secs": f"{secs_total:.1f}", "error": "; ".join(errors) or ""})
        failed += bool(errors)
        print(f"  [{i:2}/{len(parts)}] {p['part']:20} {len(listings):3} listing(s) "
              f"{secs_total:4.1f}s {'ERR ' + errors[0] if errors else ''}", flush=True)

    print()
    print_table(summary, ["#", "requested", "cond", "qty", "listings", "suppliers",
                          "ils_echo", "fmv", "fmv_part", "secs", "error"])
    csv_path = os.path.join(args.out, f"{stamp}_sheet_results.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLS)
        w.writeheader()
        w.writerows(results)
    total = sum(float(s["secs"]) for s in summary)
    print(f"\n{len(parts) - failed}/{len(parts)} part(s) without errors, {len(results)} "
          f"Results row(s), {total:.1f}s of ILS time. Rows -> {csv_path}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--part", default="A100", help="part number (default: SDK sample A100)")
    ap.add_argument("--condition", type=lambda s: [c.strip().upper() for c in s.split(",") if c.strip()],
                    help="GetPartsAvailability condition filter, e.g. NE,OH,SV")
    ap.add_argument("--qty", type=int, help="GetPartsAvailability minimum quantity")
    ap.add_argument("--exact", action="store_true", help="Part Stats: exact part number match")
    ap.add_argument("--ops", help="comma list of operations (see --list)")
    ap.add_argument("--group", choices=sorted(GROUPS), default="sourcing")
    ap.add_argument("--live", action="store_true",
                    help="use Skylink's account from Secret Manager (BILLED searches)")
    ap.add_argument("--allow-cursor", action="store_true",
                    help="with --live, allow GetNew* polling ops (they consume new items)")
    ap.add_argument("--out", default="debug_pages/ils", help="where raw responses are saved")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--list", action="store_true", help="list operations and exit")
    ap.add_argument("--sheet", help="local request .xlsx: search every part in its Requests tab "
                                    f"with {' + '.join(SHEET_OPS)}")
    args = ap.parse_args()
    if args.sheet:
        args.ops = ",".join(SHEET_OPS)

    if args.list:
        for op, (group, billed, what) in OPS.items():
            print(f"{op:26} {group:9} {'BILLED' if billed else '      '}  {what}")
        return 0

    ops = [o.strip() for o in args.ops.split(",")] if args.ops else GROUPS[args.group]
    unknown = [o for o in ops if o not in OPS]
    if unknown:
        print(f"Unknown op(s): {', '.join(unknown)}. See --list.")
        return 2

    if args.live:
        cursor = [o for o in ops if OPS[o][0] == "polling"]
        if cursor and not args.allow_cursor:
            print(f"Refusing {', '.join(cursor)} on the live account without --allow-cursor: "
                  "they mark new quotes/RFQs as fetched for everyone using this account.")
            return 2
        from sourcing.credentials.base import CredentialError
        try:
            user, password = live_credentials()
        except CredentialError as exc:
            print(f"No live ILS login: {exc}. The client creates the secret "
                  '(JSON {"username": ..., "password": ...}).')
            return 2
        billed = [o for o in ops if OPS[o][1]]
        n_parts = len(sheet_rows(args.sheet)) if args.sheet else 1
        print(f"LIVE account {user}: {len(billed) * n_parts} billed search(es).")
    else:
        user, password = TEST_USER, TEST_PASSWORD
        print("TEST MODE (TESTU01): static sample data, not billed.")

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if args.sheet:
        return run_sheet(args.sheet, user, password, args, stamp)
    failed = 0
    for op in ops:
        print(f"\n== {op}  ({OPS[op][2]})")
        try:
            status, xml, secs = call(op, user, password, args)
        except requests.RequestException as exc:
            print(f"  request failed: {exc!r}")
            failed += 1
            continue
        base = os.path.join(args.out, f"{stamp}_{op}")
        with open(base + ".xml", "w", encoding="utf-8") as fh:
            fh.write(xml)
        print(f"  HTTP {status} in {secs:.1f}s, {len(xml)} bytes -> {base}.xml")
        try:
            body = parse(op, xml)
        except ET.ParseError as exc:
            print(f"  not XML: {exc}; first bytes: {xml[:200]!r}")
            failed += 1
            continue
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        if op == "GetPartsAvailability":
            with open(base + "_rows.json", "w", encoding="utf-8") as fh:
                json.dump(availability_rows(body), fh, indent=1)
        summarize(op, body)
        if status != 200 or (isinstance(body, dict) and "SoapFault" in body) or any(
                f.get("Severity") == "Fatal" for f in faults_of(body)):
            failed += 1
    print(f"\n{len(ops) - failed}/{len(ops)} operation(s) OK. Saved to {args.out}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
