"""Onboarding run for a vendor: log in, check the sign-in, search 1-3 known
parts, log out, and report the offers and how each page element was found
(docs/GENERIC_VENDOR_PLAN.md, phase 5). Nothing is written to the sheet.

  python scripts/onboard_vendor.py wencor --part 35010-1 [--part MS20995C32]
      [--browser local] [--headed] [--fresh] [--secret sourcing-vendor-test] [--save]
      [--report test_runs/NN_.../onboarding.json]
  python scripts/onboard_vendor.py notion --login-only --login-file .local_login_notion.json \
      --browser local --headed --notify you@co.com

  --fresh   ignore selectors saved by earlier runs (shows what the adapter finds
            on its own, as on a vendor it has never seen)
  --secret  read the login from this secret instead of sourcing-vendor-<vendor>
  --save    save the learned selectors to the store (otherwise nothing is saved)
  --login-only  no part searches: check the login and its 2FA only
  --login-file  read the login from a local JSON file (same keys as a secret), for
            a test account outside Secret Manager. Never for a Skylink vendor
            login: those stay in Secret Manager (client rule). Keep the file out of
            git (.local_login*.json is ignored).
  --headed  show the local browser window (LOCAL_BROWSER_HEADLESS=0)
  --live-view  local browser: the 2FA message links to a DevTools live view of
            the page (127.0.0.1, this machine only; LOCAL_BROWSER_LIVE_VIEW=1)
  --notify  who the Chat message asking for a 2FA code mentions (repeatable)

A 2FA code the code can't make (email, SMS) is asked over Google Chat when
PUBLIC_BASE_URL is set and the API is running (sourcing/auth/hitl.py); the
run waits for it with the browser open.

The vendor can still be disabled: onboarding is how it gets checked. A person
compares the offers with the portal, then: manage_vendors.py enable <vendor>.
Live portal rule: few searches, outside the team's working hours.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.manage_vendors import check_record  # noqa: E402
from sourcing.config import get_settings, quiet_http_logs  # noqa: E402
from sourcing.credentials.base import SHARED, CredentialError, secret_key  # noqa: E402
from sourcing.models import slugify  # noqa: E402
from sourcing.notify.chat import ChatNotifier  # noqa: E402
from sourcing.onboarding import MAX_PARTS, onboard, report_text  # noqa: E402
from sourcing.store import make_store  # noqa: E402
from sourcing.vendors import adapter_class  # noqa: E402
from sourcing.vendors.registry import record_from_secret  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("vendor_id")
    parser.add_argument("--part", action="append", default=[], metavar="PART",
                        help=f"a part number a team member knows (1 to {MAX_PARTS})")
    parser.add_argument("--login-only", action="store_true",
                        help="no searches: check the login and its 2FA only")
    parser.add_argument("--login-file", metavar="FILE",
                        help="local JSON login for a test account (not a vendor login)")
    parser.add_argument("--headed", action="store_true", help="show the local browser")
    parser.add_argument("--live-view", action="store_true",
                        help="local browser: 2FA handoff link to a DevTools live view "
                             "(127.0.0.1, this machine only)")
    parser.add_argument("--notify", action="append", default=[], metavar="EMAIL",
                        help="who a 2FA code request in Chat mentions")
    parser.add_argument("--browser", choices=["local", "browserbase"],
                        help="browser backend (default: BROWSER_BACKEND)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore selectors saved by earlier runs")
    parser.add_argument("--secret", help="read the login from this secret instead")
    parser.add_argument("--save", action="store_true", help="save the learned selectors")
    parser.add_argument("--report", metavar="FILE", help="also write the report as JSON")
    return parser


async def main_async(args) -> int:
    from service.job_main import build_browser, build_person_handoff
    from sourcing.credentials.base import MemoryCredentialProvider
    from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides
    from sourcing.extract import Extractor
    from sourcing.llm import build_llm_client

    if args.browser:
        os.environ["BROWSER_BACKEND"] = args.browser
    if args.headed:
        os.environ["LOCAL_BROWSER_HEADLESS"] = "0"
    if args.live_view:
        os.environ["LOCAL_BROWSER_LIVE_VIEW"] = "1"
    get_settings.cache_clear()
    s = get_settings()
    store = make_store(s.store_backend, s.store_project, s.store_file)
    vendor_id = slugify(args.vendor_id)
    record = store.get_vendor(vendor_id)

    if args.login_file:
        with open(args.login_file, encoding="utf-8") as fh:
            creds = MemoryCredentialProvider({secret_key(vendor_id, SHARED): json.load(fh)})
        login_from = f"local file {args.login_file}"
    else:
        overrides = parse_overrides(s.vendor_secret_overrides)
        if args.secret:
            overrides[secret_key(vendor_id, SHARED)] = args.secret
        creds = SecretManagerProvider(s.gcp_project, overrides)
    try:
        cred = await creds.resolve(vendor_id, "")
    except CredentialError as exc:
        print(f"No login for {vendor_id}: {exc}")
        return 2
    if not args.login_file:
        login_from = f"secret {creds.secret_name(cred.secret_key)}"

    from_secret = record is None and adapter_class(vendor_id, None) is None
    if from_secret:
        # As the worker does for a vendor with only a secret (pipeline.py).
        record, problem = record_from_secret(args.vendor_id, cred)
        if record is None:
            print(f"PROBLEM: {problem}")
            return 2
        print(f"{vendor_id}: no record; using one built from secret {cred.secret_key} "
              f"(login sites {', '.join(record['allowed_hosts'])}; saved only with --save)")
    cls = adapter_class(vendor_id, record)
    # A record built from the secret starts at the secret's url (checked above).
    problems = check_record(vendor_id, record) if record and not from_secret else []
    if cls is None or problems:
        for problem in problems or [f"{vendor_id}: no adapter can run it"]:
            print(f"PROBLEM: {problem}")
        return 2

    llm = build_llm_client(s.llm_provider, s.llm_model, s.llm_api_key, s.llm_effort)
    adapter = cls(extractor=Extractor(llm),
                  selectors={} if args.fresh else store.get_selectors(vendor_id),
                  vendor_config=record or {}, max_page_chars=s.max_page_chars)
    print(f"Onboarding {vendor_id} with {cls.__name__}: login from {login_from}, "
          f"browser {s.browser_backend}"
          + (" (window shown)" if s.browser_backend == "local" and not s.local_browser_headless
             else "") + ", "
          f"LLM {s.llm_provider} {s.llm_model} (effort {s.llm_effort}), "
          f"{'fresh (saved selectors ignored)' if args.fresh else 'with saved selectors'}",
          flush=True)

    handoff = build_person_handoff(s, store, ChatNotifier(s.chat_webhook_url))
    if not handoff:
        how = "not asked (set PUBLIC_BASE_URL to ask a person)"
    elif s.browser_backend == "browserbase":
        how = "Live View link sent in Chat; the person logs in there"
    elif s.local_browser_live_view:
        how = ("local live view link (DevTools on 127.0.0.1) sent in Chat; open it on this "
               "machine and log in there")
    else:
        how = (f"code form at {s.public_base_url}/hitl/code (the API must be running); "
               "the local browser has no Live View")
    print(f"2FA: {how}; wait {s.hitl_code_wait_seconds}s", flush=True)
    report = await onboard(vendor_id, adapter, cred, build_browser(s),
                           [] if args.login_only else args.part,
                           handoff=handoff, people=tuple(args.notify))
    print("\n" + report_text(report))

    if args.report:
        with open(args.report, "w") as f:
            json.dump(report.as_dict(), f, indent=2, default=str)
        print(f"\nSaved {args.report}")
    if args.save:
        if report.ok and report.learned:
            if from_secret:
                store.update_vendor(vendor_id, record)
                print(f"Saved the vendor record for {vendor_id}")
            store.save_selectors(vendor_id, report.learned)
            print(f"Saved {len(report.learned)} learned selector(s) for {vendor_id}")
        else:
            print("Not saved: the run didn't finish with a working login and offers")
    if report.ok and record is not None and record.get("enabled") is False:
        print(f"\nCompare the offers with the portal; if they match: "
              f"python scripts/manage_vendors.py enable {vendor_id}")
    return 0 if report.ok else 1


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()
    parser = build_parser()
    args = parser.parse_args(argv)
    if len(args.part) > MAX_PARTS:
        parser.error(f"at most {MAX_PARTS} parts: an onboarding run only checks the vendor")
    if not args.part and not args.login_only:
        parser.error("give --part (1 to 3 known parts), or --login-only")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
