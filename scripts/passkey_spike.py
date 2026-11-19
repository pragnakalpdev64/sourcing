"""Passkey spike (plan step 7): can the program sign in with a passkey of its own?

Tried on our own Notion account first (2026-11-18), not on the client's Boeing
(Aviall) login. Two steps:

  register  a browser with Chrome's virtual passkey device opens the site; a person
            signs in as usual (in the window, or Browserbase Live View), opens the
            account settings and adds a passkey. The device creates it; this script
            saves it to --file (private key inside: a local file for our own test
            account only, never committed; a client login's goes to Secret Manager).
  login     a fresh browser, the saved passkey loaded, the login page opened: the
            script clicks the page's passkey button (or a person does) and reports
            whether the passkey answered. The new use count is saved back.

  import    moves the passkey from --file into the vendor's login secret (a new
            version), then deletes the file.

With --secret <vendor> (e.g. notion) the passkey is read from and saved to that
vendor's shared login secret in Secret Manager (sourcing-vendor-<vendor>, key
"passkeys"), as a run would: no local file. The secret must exist already
(made on /credentials/new); a new version is added, never a new secret.

Both end the same way: the person logs out in the window, then presses Enter
(logout confirmed before a browser closes, team rule 2026-11-04).

  .venv/bin/python scripts/passkey_spike.py register --url https://www.notion.so/login
  .venv/bin/python scripts/passkey_spike.py login    --url https://www.notion.so/login
  ... --browser browserbase   (the cloud browser: Live View link printed)
  INFRA_SECRETS=gsm .venv/bin/python scripts/passkey_spike.py import --secret notion
  INFRA_SECRETS=gsm .venv/bin/python scripts/passkey_spike.py login --secret notion --browser browserbase
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sourcing.auth.passkey import VirtualPasskeys, describe, merge  # noqa: E402
from sourcing.models import SHARED  # noqa: E402

logger = logging.getLogger("passkey_spike")
DEFAULT_FILE = ".local_passkey_notion.json"
REGISTER_WAIT_SECONDS = 15 * 60
LOGIN_WAIT_SECONDS = 3 * 60
# Buttons a login page uses for a passkey (Notion and most others, 2026-11).
PASSKEY_BUTTON = re.compile(r"passkey|security key|face id|touch id|windows hello", re.I)


async def open_browser(kind: str):
    if kind == "local":
        from sourcing.browser.local import LocalBrowserClient
        client = LocalBrowserClient(headless=False)
        handle = await client.open(await client.create_context())
        return client, handle
    from sourcing.browser.browserbase import BrowserbaseClient
    from sourcing.config import get_settings
    s = get_settings()
    client = BrowserbaseClient(s.browserbase_api_key, s.browserbase_project_id,
                               record_sessions=False)
    handle = await client.open(await client.create_context())   # logs the Live View link
    return client, handle


def _provider():
    from sourcing.config import get_settings
    from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides
    s = get_settings()
    return SecretManagerProvider(s.gcp_project, parse_overrides(s.vendor_secret_overrides))


async def load_keys(args) -> list[dict]:
    if not args.secret:
        return json.loads(args.file.read_text())["credentials"]
    cred = await _provider().resolve(args.secret, "")
    print(f"Login secret {cred.secret_key}: {len(cred.passkeys)} passkey(s)", flush=True)
    return list(cred.passkeys)


async def store_keys(args, saved: list[dict], current: list[dict]) -> None:
    """A run's way: merged into the shared login secret as a new version."""
    if not args.secret:
        save(args.file, merge(saved, current))
        return
    await _provider().update(args.secret, SHARED, {"passkeys": merge(saved, current)},
                             must_exist=True)
    print(f"Saved to the login secret of {args.secret} (new version)", flush=True)


def save(path: Path, creds: list[dict]) -> None:
    path.write_text(json.dumps({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "credentials": creds}, indent=1))
    os.chmod(path, 0o600)


async def wait_for_enter(prompt: str, close_file: Path) -> None:
    """Enter in a terminal; started in the background (no terminal), the file
    `close_file` appearing instead."""
    if sys.stdin.isatty():
        print(prompt, flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        return
    print(f"\nLog out in the browser; it closes when {close_file} exists.", flush=True)
    while not close_file.exists():
        await asyncio.sleep(1)
    close_file.unlink()


async def register(args, page) -> None:
    keys = await VirtualPasskeys.attach(page)
    await page.goto(args.url)
    print("\nIn the browser: sign in as usual, open the account settings and add a "
          "passkey\n(Notion: Settings > My account > Passkeys > Add passkey). "
          f"Waiting up to {REGISTER_WAIT_SECONDS // 60} min.\n", flush=True)
    deadline = time.time() + REGISTER_WAIT_SECONDS
    while time.time() < deadline:
        creds = await keys.credentials()
        if creds:
            saved = await load_keys(args) if args.secret else []
            await store_keys(args, saved, creds)
            print(f"\nPasskey created and saved ({args.secret or args.file}): "
                  f"{', '.join(describe(c) for c in creds)}", flush=True)
            return
        await asyncio.sleep(2)
    print("\nNo passkey was created in time.", flush=True)


async def login(args, page) -> None:
    saved = await load_keys(args)
    if not saved:
        print("No saved passkey: run the register (or import) step first.", flush=True)
        return
    keys = await VirtualPasskeys.attach(page, saved)
    await page.goto(args.url)
    await page.wait_for_load_state("domcontentloaded")
    clicked = None
    for _ in range(20):                     # the button may come a moment later
        for role in ("button", "link"):
            target = page.get_by_role(role, name=PASSKEY_BUTTON)
            if await target.count() and await target.first.is_visible():
                clicked = (await target.first.inner_text()).strip()
                await target.first.click()
                break
        if clicked or keys.asserted:
            break
        await asyncio.sleep(1)
    print(f"\nPasskey button: {clicked!r}" if clicked else
          "\nNo passkey button found: click the page's passkey option in the browser.",
          flush=True)
    deadline = time.time() + LOGIN_WAIT_SECONDS
    while not keys.asserted and time.time() < deadline:
        await asyncio.sleep(1)
    if not keys.asserted:
        print("\nThe passkey was NOT used (the site never asked for it, or it refused "
              "this one).", flush=True)
        return
    await asyncio.sleep(5)                  # the site finishes the sign-in
    await store_keys(args, saved, await keys.credentials())   # new use count
    print(f"\nPasskey used. Page now: {page.url}\nCheck in the browser that you are "
          "signed in.", flush=True)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["register", "login", "import"])
    p.add_argument("--secret", default="", help="vendor id: use its login secret, not --file")
    p.add_argument("--url", default="https://www.notion.so/login")
    p.add_argument("--file", type=Path, default=Path(DEFAULT_FILE))
    p.add_argument("--browser", choices=["local", "browserbase"], default="local")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.step == "import":
        if not args.secret or not args.file.exists():
            sys.exit("import needs --secret <vendor> and an existing --file")
        current = json.loads(args.file.read_text())["credentials"]
        await store_keys(args, await load_keys(args), current)
        args.file.unlink()
        print(f"{args.file} deleted: the passkey is only in the secret now.")
        return
    if args.step == "login" and not args.secret and not args.file.exists():
        sys.exit(f"{args.file} not found: run the register step first")

    client, handle = await open_browser(args.browser)
    try:
        await (register if args.step == "register" else login)(args, handle.page)
        await wait_for_enter("\nLog out in the browser, then press Enter here to close it.",
                             args.file.with_suffix(".close"))
    finally:
        await client.close(handle)


if __name__ == "__main__":
    asyncio.run(main())
