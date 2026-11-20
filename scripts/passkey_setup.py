"""One-time setup of the program's own passkey for a site that signs in with passkeys
only (Aviall / Boeing, 2026-11-20: the account offers "Passkey" and nothing else).

Why here and not in the cloud browser: a passkey on a person's phone only answers a
browser near that phone (Bluetooth), so it can never answer Browserbase. This script
opens the installed Google Chrome on the computer the person sits at:

  1. Chrome opens the login page. The person signs in THE NORMAL WAY (their own
     passkey: phone QR, Windows Hello, Touch ID, security key). Nothing of ours is
     switched on yet, so their passkey works.
  2. Once signed in, they press Enter. Only now the program's passkey device is
     switched on, in every tab (the settings page may open a new one).
  3. In the site's account settings they choose "Add a passkey" / "Add this device".
     The site creates the passkey on the program's device; the script saves it:
       --secret <vendor>  into that vendor's login secret in Secret Manager ("passkeys"),
                          as runs read it (the secret must exist; a new version is added)
       --file <path>      a local file instead (our own test account only)
  4. They log out in Chrome and press Enter; Chrome closes.

Every later run (Browserbase) loads that passkey and signs in by itself
(sourcing/auth/passkey.py). The person's own passkey is not touched. Deleting the
program's passkey in the site's settings ends its access.

  # test on our own Notion account first (local file):
  .venv/bin/python scripts/passkey_setup.py --url https://www.notion.so/login --file .local_passkey_setup_test.json
  # the real one (client approved), straight into Secret Manager:
  INFRA_SECRETS=gsm .venv/bin/python scripts/passkey_setup.py --url https://shop.boeing.com/aviation-supply --secret aviall
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sourcing.auth.passkey import VirtualPasskeys, describe, merge  # noqa: E402
from sourcing.models import SHARED  # noqa: E402

logger = logging.getLogger("passkey_setup")
ADD_WAIT_SECONDS = 15 * 60


async def wait_for_person(prompt: str, signal: Path) -> None:
    """Enter in a terminal; started without one, the file `signal` appearing."""
    print(f"\n{prompt}", flush=True)
    if sys.stdin.isatty():
        await asyncio.to_thread(sys.stdin.readline)
        return
    print(f"(no terminal: create the file {signal} to go on)", flush=True)
    while not signal.exists():
        await asyncio.sleep(1)
    signal.unlink()


async def save(args, creds: list[dict]) -> None:
    if args.secret:
        from sourcing.config import get_settings
        from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides
        s = get_settings()
        provider = SecretManagerProvider(s.gcp_project, parse_overrides(s.vendor_secret_overrides))
        cred = await provider.resolve(args.secret, "")
        await provider.update(args.secret, SHARED, {"passkeys": merge(list(cred.passkeys), creds)},
                              must_exist=True)
        print(f"Saved to the login secret {cred.secret_key} (new version).", flush=True)
        return
    args.file.write_text(json.dumps({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "credentials": creds}, indent=1))
    os.chmod(args.file, 0o600)
    print(f"Saved to {args.file} (only you can read it; never commit it).", flush=True)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="the site's login page")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--secret", help="vendor id: save into its login secret")
    where.add_argument("--file", type=Path, help="save to a local file (test accounts only)")
    p.add_argument("--chromium", action="store_true",
                   help="Playwright's own Chromium instead of the installed Google Chrome")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal = Path(str(args.file or f".passkey_setup_{args.secret}") + ".next")

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    # The installed Chrome: its passkey prompts (phone QR, Windows Hello, Touch ID,
    # security key) are what the person's own sign-in needs. Playwright's Chromium
    # may lack some of them.
    browser = await pw.chromium.launch(headless=False,
                                       channel=None if args.chromium else "chrome")
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(args.url)
        await wait_for_person(
            "1) In Chrome: sign in the NORMAL way, with your own passkey (phone, Windows "
            "Hello, ...).\n   When you are signed in, press Enter here.", signal)

        # From now on every tab gets the program's passkey device.
        devices: list[VirtualPasskeys] = []

        async def attach(tab) -> None:
            try:
                devices.append(await VirtualPasskeys.attach(tab))
            except Exception as exc:
                logger.warning("Passkey device not switched on in a tab: %r", exc)

        for tab in context.pages:
            await attach(tab)
        context.on("page", lambda tab: asyncio.ensure_future(attach(tab)))
        print("\n2) The program's passkey device is on. In the site's account settings choose "
              "'Add a passkey' / 'Add this device'.\n   No phone or Windows prompt should "
              f"appear for it. Waiting up to {ADD_WAIT_SECONDS // 60} min.", flush=True)

        made: list[dict] = []
        deadline = time.time() + ADD_WAIT_SECONDS
        while not made and time.time() < deadline:
            await asyncio.sleep(2)
            for device in list(devices):
                try:
                    made += await device.credentials()
                except Exception:
                    pass          # its tab was closed
        if made:
            print("\nPasskey created by the site: " + ", ".join(describe(c) for c in made),
                  flush=True)
            await save(args, made)
        else:
            print("\nNo passkey was created in time: nothing saved.", flush=True)
        await wait_for_person("3) Log out of the site in Chrome, then press Enter to close it.",
                              signal)
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
