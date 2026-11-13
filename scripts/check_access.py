"""Step 0 check: can this environment reach every dependency?

Prints OK/FAIL per dependency and never prints secret values.

  python scripts/check_access.py                       # locally (uses .env)
  python scripts/check_access.py --vendor proponent --user you@co.com
  python scripts/check_access.py --vendor proponent,dasi,aviall   # every vendor going live
  gcloud run jobs execute sourcing-worker --args=scripts/check_access.py --wait
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing.config import get_settings  # noqa: E402

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}", flush=True)


async def check(name, fn):
    try:
        detail = fn()
        if asyncio.iscoroutine(detail):
            detail = await detail
        report(name, True, detail or "")
    except Exception as exc:
        report(name, False, repr(exc)[:300])


async def main(vendors: list[str], user: str, send_chat: bool) -> int:
    s = get_settings()
    local = s.store_backend != "firestore"

    def env():
        names = ["GCP_PROJECT", "SHEET_ID", "LLM_PROVIDER", "LLM_MODEL", "LLM_EFFORT"]
        if s.browser_backend == "browserbase":
            names += ["BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID"]
        # Chat and /trigger aren't needed for a local job run (Chat is skipped
        # when its URL is empty), so only Cloud Run (firestore) requires them.
        optional = ["CHAT_WEBHOOK_URL", "TRIGGER_SECRET"]
        required = names if local else names + optional
        unset = lambda n: os.environ.get(n, "").strip() in ("", "unset")  # noqa: E731
        missing = [n for n in required if unset(n)]
        if missing:
            raise RuntimeError(f"missing: {', '.join(missing)}")
        not_set = [n for n in optional if unset(n)]
        return f"all set (optional locally, not set: {', '.join(not_set)})" if not_set else "all set"

    def sheet():
        from sourcing.sheets import open_sheet

        try:
            client = open_sheet(s.sheet_id, s.requests_tab, s.results_tab)
        except PermissionError as exc:
            raise RuntimeError(
                "no access to the sheet. Log in with the Sheets scope "
                "(gcloud auth application-default login --scopes=...cloud-platform,"
                "...spreadsheets) and make sure that account can EDIT the sheet "
                "(shared with it, or link sharing set to 'Anyone with the link: Editor')"
            ) from exc
        return f"{len(client.read_new_rows())} NEW rows in '{s.requests_tab}'"

    def firestore():
        from sourcing.store import FirestoreStore

        st = FirestoreStore(project=s.gcp_project)
        ref = st.db.collection("healthcheck").document("check_access")
        ref.set({"at": time.time()})
        return "read/write ok" if ref.get().exists else "write not visible"

    def vendor_record(vendor: str) -> dict:
        from sourcing.store import make_store

        return make_store(s.store_backend, s.store_project, s.store_file).get_vendor(vendor) or {}

    def vendor_registry(vendor: str):
        from sourcing.store import make_store
        from sourcing.vendors.registry import VendorRegistry

        reg = VendorRegistry(make_store(s.store_backend, s.store_project, s.store_file))
        vendor_id = reg.resolve_id(vendor)
        if reg.record(vendor_id) is None and not reg.has_adapter(vendor_id):
            return (f"{vendor}: no record yet; the worker sets it up from its secret's url "
                    "(generic adapter)")
        problem = reg.problem(vendor_id)
        if problem:
            raise RuntimeError(problem)
        from sourcing.vendors import adapter_class

        record = reg.record(vendor_id)
        # The class that will run, not the record's text: an auto-made "generic" record
        # does not override a vendor's own adapter (DASI, 2026-10-29).
        cls = adapter_class(vendor_id, record)
        return (f"{vendor} -> {reg.display_name(vendor_id)} (runs {cls.__name__ if cls else 'nothing'}"
                + (f", hosts={record.get('allowed_hosts') or 'adapter default'})" if record
                   else ", no record)"))

    async def secret_manager(vendor: str):
        from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides

        sm = SecretManagerProvider(s.gcp_project, parse_overrides(s.vendor_secret_overrides))
        cred = await sm.resolve(vendor, user)
        url_note = "url=not set"
        if cred.url:
            from sourcing.pipeline import default_adapter_factory
            from sourcing.vendors.base import check_login_url

            adapter = default_adapter_factory(None)(vendor, {}, vendor_record(vendor))
            problem = check_login_url(cred.url, adapter.hosts) if adapter else None
            url_note = f"url={cred.url}" + (f" IGNORED: {problem}" if problem else " (valid)")
        return (f"{vendor} for {user or '(shared)'} -> {sm.secret_name(cred.secret_key)} "
                f"(username set, totp={'yes' if cred.totp_secret else 'no'}, {url_note})")

    def browserbase():
        from browserbase import Browserbase

        bb = Browserbase(api_key=s.browserbase_api_key)
        bb.projects.retrieve(s.browserbase_project_id)
        return "project reachable"

    async def local_chromium():
        # On Cloud Run this proves the image has Chromium and the job has enough memory.
        from sourcing.browser.local import LocalBrowserClient

        client = LocalBrowserClient(headless=True)
        handle = await client.open(await client.create_context())
        try:
            await handle.page.set_content("<p>ok</p>")
            text = await handle.page.inner_text("p")
            if text != "ok":
                raise RuntimeError(f"page did not render: {text!r}")
            return f"Chromium {handle.browser.version} starts and renders a page"
        finally:
            await client.close(handle)

    async def llm():
        from sourcing.llm import build_llm_client

        client = build_llm_client(s.llm_provider, s.llm_model, s.llm_api_key, s.llm_effort)
        data, usage = await client.json_call(
            system="Reply with the requested JSON only.",
            prompt="Return {\"ok\": true}.",
            schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"], "additionalProperties": False},
            max_tokens=100)
        if not data or data.get("ok") is not True:
            raise RuntimeError(f"unexpected response: {data!r}")
        return (f"{s.llm_provider}/{s.llm_model} effort={s.llm_effort} "
               f"reachable (${usage.cost_usd:.6f})")

    async def chat():
        from sourcing.notify.chat import ChatNotifier

        if not s.chat_webhook_url:
            raise RuntimeError("CHAT_WEBHOOK_URL is empty, nothing was sent")
        await ChatNotifier(s.chat_webhook_url).send("SourcingAI access check: Chat webhook works.")
        return "message sent"

    await check("env", env)
    await check("google sheet", sheet)
    if local:
        report("firestore", True, f"skipped (STORE_BACKEND={s.store_backend}, state in "
               f"{s.store_file if s.store_backend == 'file' else 'memory'})")
    else:
        await check("firestore", firestore)
    # One pair of lines per vendor: a deploy for several vendors needs every login readable.
    for vendor in vendors:
        await check(f"secret manager {vendor}", lambda v=vendor: secret_manager(v))
        await check(f"vendor {vendor}", lambda v=vendor: vendor_registry(v))
    # Both browsers are checked when they're set up, so one run shows whether
    # either can be used (BROWSER_BACKEND only picks the one a run uses).
    report("browser backend", s.browser_backend in ("browserbase", "local"),
           f"BROWSER_BACKEND={s.browser_backend}")
    if s.browser_backend == "browserbase" or s.browserbase_api_key:
        await check("browserbase", browserbase)
    else:
        report("browserbase", True, "skipped (BROWSER_BACKEND=local, no BROWSERBASE_API_KEY)")
    await check("local chromium", local_chromium)
    await check("llm", llm)
    if send_chat:
        await check("google chat", chat)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", default="proponent",
                        help="vendor(s) whose login to read, comma-separated (proponent,dasi,aviall)")
    parser.add_argument("--user", default="", help="requester email (default: shared login only)")
    parser.add_argument("--chat", action="store_true", help="also post a test message to Chat")
    args = parser.parse_args()
    vendors = [v.strip() for v in args.vendor.split(",") if v.strip()]
    raise SystemExit(asyncio.run(main(vendors, args.user, args.chat)))
