"""Worker entrypoint: search every NEW row of the sheet and write the offers.

  python -m service.job_main                  # SHEET_ID from .env, bulk search (2+ parts)
  python -m service.job_main --sheet <id>     # another sheet (Google Sheet id or .xlsx path)
  python -m service.job_main --one-by-one     # one search per part instead of bulk
  python -m service.job_main --dummy          # dry run: fake vendor, no browser / GSM / LLM
  python -m service.job_main --all-sheets     # SHEET_ID + every sheet in the registry (Cloud Run)
  python -m service.job_main --browser local  # Chromium on this machine instead of Browserbase

Everything else (LLM model and effort, store, keys) comes from .env. Flags apply
to this run only. Run with --help for the full list.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from sourcing.config import Settings, get_settings, quiet_http_logs
from sourcing.notify.chat import ChatNotifier
from sourcing.pipeline import Deps, Pipeline, default_adapter_factory
from sourcing.sheets import open_sheet
from sourcing.sinks.sheet import SheetSink

# Runs with fewer parts than this are searched one by one (one part doesn't need bulk).
BULK_MIN_PARTS = 2
from sourcing.store import RERUN_FLAG, RERUN_HOLDER, WORKER_LOCK, Store, make_store

logger = logging.getLogger("sourcing.job")


def new_run_id() -> str:
    # Cloud Run sets CLOUD_RUN_EXECUTION (e.g. sourcing-worker-abc12); reuse it so
    # Firestore jobs line up with Cloud Run's execution list.
    return os.environ.get("CLOUD_RUN_EXECUTION") or (
        datetime.now(timezone.utc).strftime("local-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    )


def with_vendor_overrides(factory, overrides: dict[str, dict]):
    """Wrap an adapter factory so per-run overrides win over the stored vendor
    config. Keys under "*" apply to every vendor. Nothing is saved to the store."""
    if not overrides:
        return factory

    def wrapped(vendor_id: str, selectors: dict, config: dict):
        merged = dict(config)
        for scope in ("*", vendor_id):
            merged.update(overrides.get(scope, {}))
        return factory(vendor_id, selectors, merged)
    return wrapped


def build_browser(settings: Settings):
    """BROWSER_BACKEND=browserbase (remote, saved Contexts, Live View) or local
    (Chromium in this container / on this machine)."""
    if settings.browser_backend == "local":
        from sourcing.browser.local import LocalBrowserClient

        return LocalBrowserClient(headless=settings.local_browser_headless,
                                  live_view=settings.local_browser_live_view)
    if settings.browser_backend == "browserbase":
        from sourcing.browser.browserbase import BrowserbaseClient

        return BrowserbaseClient(settings.browserbase_api_key, settings.browserbase_project_id,
                                 record_sessions=settings.browserbase_record_sessions,
                                 session_timeout_seconds=settings.browserbase_session_timeout)
    raise ValueError(f"Unknown BROWSER_BACKEND {settings.browser_backend!r}; "
                     "choose browserbase or local")


def build_person_handoff(settings: Settings, store: Store, notifier):
    """Asks a person over Chat to finish a 2FA step (sourcing/auth/hitl.py):
    a Live View link with Browserbase, a code form when PUBLIC_BASE_URL says
    where the API runs. None when neither is possible."""
    live_view = (settings.browser_backend == "browserbase"
                 or (settings.browser_backend == "local" and settings.local_browser_live_view))
    if not settings.public_base_url and not live_view:
        return None
    from sourcing.auth.hitl import PersonHandoff
    from sourcing.notify.chat import ChatAppSender

    chat_app = ChatAppSender(settings.chat_app_space) if settings.chat_app_space else None
    return PersonHandoff(store, notifier, settings.public_base_url, chat_app=chat_app,
                         wait_seconds=settings.hitl_code_wait_seconds)


def build_shared_deps(settings: Settings, store: Store, *, dummy: bool,
                      vendor_overrides: Optional[dict[str, dict]] = None) -> Deps:
    """Everything that is the same across every sheet: credentials, browser,
    vendor adapters, notifications. `sheets`/`sink` are filled in per sheet."""
    if dummy:
        from sourcing.vendors.dummy import DummyAdapter, DummyCredentialProvider, NullBrowser

        creds, browser = DummyCredentialProvider(), NullBrowser()
        factory = lambda vendor_id, selectors, config: DummyAdapter(  # noqa: E731
            selectors=selectors, vendor_config=config)
    else:
        from sourcing.credentials.gsm import SecretManagerProvider, parse_overrides
        from sourcing.extract import Extractor
        from sourcing.llm import build_llm_client

        creds = SecretManagerProvider(settings.gcp_project,
                                      parse_overrides(settings.vendor_secret_overrides))
        browser = build_browser(settings)
        llm_client = build_llm_client(settings.llm_provider, settings.llm_model,
                                      settings.llm_api_key, settings.llm_effort)
        factory = default_adapter_factory(Extractor(llm_client), settings.max_page_chars)
    factory = with_vendor_overrides(factory, vendor_overrides or {})

    notifier = ChatNotifier(settings.chat_webhook_url)
    return Deps(
        sheets=None, store=store, creds=creds, browser=browser, sink=None,
        notifier=notifier, adapter_factory=factory,
        code_handoff=None if dummy else build_person_handoff(settings, store, notifier),
        max_login_attempts=settings.max_login_attempts, lease_seconds=settings.lease_seconds,
        public_base_url=settings.public_base_url,
        llm_label=("none (dummy run)" if dummy else
                   f"{settings.llm_provider} {settings.llm_model}, effort {settings.llm_effort}"),
    )


async def run_one_sheet(shared: Deps, sheet: dict, run_id: str) -> dict:
    sheet_id = sheet["sheet_id"]
    try:
        sheets = await asyncio.to_thread(
            open_sheet, sheet_id, sheet.get("requests_tab", "Requests"),
            sheet.get("results_tab", "Results"),
        )
    except Exception as exc:
        logger.error("Sheet %s: could not open (%r). Is it shared with the runtime "
                     "service account?", sheet_id, exc)
        return {"sheet_id": sheet_id, "error": repr(exc)[:300]}

    # The RFQ tab always; the Results tab only when asked (--results; client, 2026-10-29).
    results = os.environ.get("WRITE_RESULTS_TAB", "").strip() in ("1", "true", "yes")
    deps = replace(shared, sheets=sheets, sink=SheetSink(sheets, rfq=True, results=results))
    # Google Sheet: last 8 chars of its id; local Excel: the file name ("test")
    if sheet_id.lower().endswith(".xlsx"):
        tag = os.path.splitext(os.path.basename(sheet_id))[0]
    else:
        tag = sheet_id[-8:]
    sheet_run_id = f"{run_id}__{tag}"
    try:
        summary = await Pipeline(deps, sheet_run_id).run()
    except Exception as exc:
        logger.exception("Sheet %s: run failed", sheet_id)
        return {"sheet_id": sheet_id, "error": repr(exc)[:300]}
    return {"sheet_id": sheet_id, **summary}


# A sheet that keeps getting rows can't keep one job running for ever.
MAX_PASSES = 5
# Nor can one that keeps being edited keep it waiting.
MAX_QUIET_WAIT_SECONDS = 600


async def wait_until_quiet(store: Store, quiet_seconds: int, clock=None, sleep=None) -> float:
    """Wait until no registered sheet was edited in the last `quiet_seconds`.

    The Sheets add-on calls /trigger on every edit and /trigger notes the time
    (`last_edit_at`); add-ons can't run the 90 s timer the per-sheet script used,
    so the job started by the first edit waits here instead, and a row is not
    read while its quantity or vendor is still being typed. Returns the seconds
    waited. Re-read every few seconds: edits keep coming in while it waits."""
    clock = clock or time.time
    sleep = sleep or asyncio.sleep
    start = clock()
    while True:
        latest = max((float(s.get("last_edit_at") or 0)
                      for s in store.list_enabled_sheets()), default=0.0)
        left = latest + quiet_seconds - clock()
        waited = clock() - start
        if left <= 0:
            return waited
        if waited >= MAX_QUIET_WAIT_SECONDS:
            logger.warning("Sheets still being edited after %ds: reading them now",
                           MAX_QUIET_WAIT_SECONDS)
            return waited
        if waited == 0:
            logger.info("A sheet was edited %ds ago: waiting until it's quiet for %ds",
                        quiet_seconds - left, quiet_seconds)
        await sleep(min(left, 5))


async def run(dummy: bool, vendor_overrides: Optional[dict[str, dict]] = None,
              all_sheets: bool = False) -> int:
    settings = get_settings()
    store = make_store(settings.store_backend, settings.store_project, settings.store_file)
    for n in range(MAX_PASSES):
        run_id = new_run_id()
        if not store.acquire_lock(WORKER_LOCK, run_id, settings.job_lock_seconds):
            logger.info("Another worker run holds the lock; exiting")
            return 0
        try:
            if all_sheets:
                await wait_until_quiet(store, settings.trigger_quiet_seconds)
        except BaseException:
            store.release_lock(WORKER_LOCK, run_id)
            raise
        # This pass reads the sheet now: a request made before it is covered.
        store.release_lock(RERUN_FLAG, RERUN_HOLDER)
        try:
            await _one_pass(settings, store, run_id, dummy, vendor_overrides, all_sheets)
        finally:
            store.release_lock(WORKER_LOCK, run_id)
        # Rows added while this pass ran (Apps Script -> /trigger): read again.
        # The lock is free by now, so a /trigger in between starts its own job.
        if not store.is_locked(RERUN_FLAG):
            break
        logger.info("Rows were added during the run: reading the sheet again (pass %d)", n + 2)
    else:
        logger.warning("Stopped after %d passes; rows still waiting go with the next trigger "
                       "or sweep", MAX_PASSES)
    return 0


async def _one_pass(settings: Settings, store: Store, run_id: str, dummy: bool,
                    vendor_overrides: Optional[dict[str, dict]], all_sheets: bool) -> None:
    default = {"sheet_id": settings.sheet_id, "requests_tab": settings.requests_tab,
               "results_tab": settings.results_tab}
    if all_sheets:
        # Cloud Run: SHEET_ID plus every sheet added with scripts/manage_sheets.py
        if settings.sheet_id:
            store.register_sheet(settings.sheet_id, {
                **default, "label": "default (SHEET_ID env var)"})
        sheets = store.list_enabled_sheets()
    else:
        sheets = [default] if settings.sheet_id else []
    if not sheets:
        logger.warning("No sheet to process: set SHEET_ID in .env or pass --sheet%s",
                       "" if not all_sheets else
                       ", or add one with scripts/manage_sheets.py add")
        return

    logger.info("Run %s: mode=%s, browser=%s, store=%s%s, llm=%s/%s (effort %s), chat=%s, "
                "vendor secret overrides=%s", run_id, "DUMMY" if dummy else "REAL",
                "none" if dummy else settings.browser_backend, settings.store_backend,
                f" ({settings.store_file})" if settings.store_backend == "file" else "",
                settings.llm_provider, settings.llm_model, settings.llm_effort,
                "on" if settings.chat_webhook_url else "off",
                settings.vendor_secret_overrides or "none")
    if vendor_overrides:
        logger.info("Vendor config for this run: %s", vendor_overrides)
    shared = build_shared_deps(settings, store, dummy=dummy,
                               vendor_overrides=vendor_overrides)
    logger.info("Processing %d sheet(s): %s", len(sheets),
                ", ".join(s["sheet_id"] for s in sheets))
    results = await asyncio.gather(*(run_one_sheet(shared, s, run_id) for s in sheets))
    failed = [r["sheet_id"] for r in results if r.get("error")]
    if failed:
        logger.warning("Sheets that failed to run: %s", failed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search every NEW row of the sheet (SHEET_ID in .env), in bulk, "
                    "and write the offers to its Results tab. LLM model/effort and keys come "
                    "from .env; flags apply to this run only.")
    parser.add_argument("--sheet", metavar="ID",
                        help="search this sheet instead of SHEET_ID (Google Sheet id or .xlsx path)")
    # Bulk is the default since 2026-10-16 (client's choice): ~3x faster and half the
    # LLM cost of one by one, same offers in runs 07/08.
    search = parser.add_mutually_exclusive_group()
    search.add_argument("--bulk", nargs="?", const=BULK_MIN_PARTS, type=int, metavar="N",
                        help=f"bulk search for runs of at least N parts (default {BULK_MIN_PARTS}, "
                             "used without this flag too); smaller runs go one by one")
    # REMOVE-IF-PER-PART-DROPPED: only needed while one-by-one stays selectable.
    search.add_argument("--one-by-one", action="store_true",
                        help="one search per part for this run instead of bulk")
    parser.add_argument("--dummy", action="store_true",
                        help="dry run: fake vendor, no browser / Secret Manager / LLM")
    parser.add_argument("--browser", choices=("browserbase", "local"),
                        help="browser for this run (default: BROWSER_BACKEND, else browserbase); "
                             "local = Chromium on this machine / in the container")
    parser.add_argument("--headed", action="store_true",
                        help="with --browser local: show the browser window (local debugging)")
    parser.add_argument("--record", action="store_true",
                        help="record Browserbase sessions (replay link in the log; shows the "
                             "login page, local debugging only)")
    parser.add_argument("--dump-pages", action="store_true",
                        help="save each results page to debug_pages/")
    parser.add_argument("--results", action="store_true",
                        help="also write each offer to the Results tab (the RFQ tab, in the "
                             "client's RFQ layout, is always written)")
    parser.add_argument("--rfq", action="store_true",
                        help="no effect: the RFQ tab is always written (kept so older "
                             "commands still run)")
    parser.add_argument("--all-sheets", action="store_true",
                        help="also process every sheet in the registry (used by the Cloud Run job)")
    parser.add_argument("--set", action="append", default=[], metavar="[VENDOR:]KEY=VALUE",
                        help="advanced: override one vendor config key, e.g. "
                             "--set proponent:logout_after_run=false (value read as JSON if it "
                             "parses). Repeatable.")
    return parser


def env_overrides(args: argparse.Namespace) -> dict[str, str]:
    """Env vars to set from the flags that were given."""
    env: dict[str, str] = {}
    if args.sheet:
        env["SHEET_ID"] = args.sheet
    if args.dummy:
        env["DUMMY_MODE"] = "1"
    if args.browser:
        env["BROWSER_BACKEND"] = args.browser
    if args.headed:
        env["LOCAL_BROWSER_HEADLESS"] = "0"
    if args.record:
        env["BROWSERBASE_RECORD_SESSIONS"] = "1"
    if args.dump_pages:
        env["DEBUG_DUMP_PAGES"] = "1"
    if args.results:
        env["WRITE_RESULTS_TAB"] = "1"
    return env


def vendor_overrides(args: argparse.Namespace,
                     parser: argparse.ArgumentParser) -> dict[str, dict]:
    """{"*" or vendor_id: {config key: value}} for this run. Search is bulk (runs of
    --bulk N parts or more) unless --one-by-one was given, whatever the stored vendor
    config says."""
    out: dict[str, dict] = {"*": {
        "bulk_min_parts": None if args.one_by_one else (args.bulk or BULK_MIN_PARTS)}}
    for item in args.set:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            parser.error(f"--set {item!r}: expected [VENDOR:]KEY=VALUE")
        scope, _, key = key.strip().rpartition(":")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        out.setdefault(scope or "*", {})[key] = value
    return out


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    quiet_http_logs()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Flags beat .env: set them before Settings is (re)read
    os.environ.update(env_overrides(args))
    get_settings.cache_clear()
    try:
        return asyncio.run(_stop_on_sigterm(run(dummy=get_settings().dummy_mode,
                                                vendor_overrides=vendor_overrides(args, parser),
                                                all_sheets=args.all_sheets)))
    except asyncio.CancelledError:
        logger.warning("Run stopped by SIGTERM; browser sessions were closed")
        return 143


async def _stop_on_sigterm(coro):
    """SIGTERM (Cloud Run stopping the job, the local API) ends the run like Ctrl+C:
    the run is cancelled, so its `finally` blocks log out and release the Browserbase
    sessions instead of leaving them open until their timeout (2026-11-13)."""
    task = asyncio.current_task()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError, ValueError):
        pass                              # not the main thread / not supported here
    return await coro


if __name__ == "__main__":
    raise SystemExit(main())
