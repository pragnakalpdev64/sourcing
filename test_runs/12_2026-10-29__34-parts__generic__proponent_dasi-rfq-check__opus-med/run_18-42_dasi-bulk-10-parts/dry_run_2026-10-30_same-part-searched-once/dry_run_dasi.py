"""Dry run of the real pipeline + DASI adapter on a saved DASI results page.

No portal: login is skipped and every store.dasi.com request is answered with
the saved bulk results page (debug_pages/20261029-182551-dasi-bulk.html). The
LLM extraction is real (LLM_* from .env). Writes a local .xlsx (Requests, RFQ,
Results) so the RFQ lines can be checked.

    .venv/bin/python <this file> <saved page.html> <out.xlsx>
"""
import asyncio
import logging
import os
import sys

ROOT = os.getcwd()
sys.path.insert(0, ROOT)

import openpyxl  # noqa: E402

from sourcing.browser.local import LocalBrowserClient  # noqa: E402
from sourcing.config import Settings  # noqa: E402
from sourcing.credentials.base import MemoryCredentialProvider  # noqa: E402
from sourcing.extract import Extractor  # noqa: E402
from sourcing.llm import build_llm_client  # noqa: E402
from sourcing.notify.chat import ChatNotifier  # noqa: E402
from sourcing.pipeline import Deps, Pipeline, default_adapter_factory  # noqa: E402
from sourcing.sheets import open_sheet  # noqa: E402
from sourcing.sinks.sheet import SheetSink  # noqa: E402
from sourcing.store import MemoryStore  # noqa: E402

# Yesterday's 10 parts, plus the same part asked again further down: MS20470AD3-4
# in LB (DASI has LB lots), MS24665-132 for 5 pieces.
REQUESTS = [
    ("MS24665-132", "1", "EA"), ("MS20470AD4-3", "1", "EA"), ("MS20470AD4-4", "1", "EA"),
    ("MS20470AD3-4", "1", "EA"), ("MS20470AD5-4", "1", "EA"), ("NAS1149C0863R", "1", "EA"),
    ("NAS1149C0432R", "1", "EA"), ("NAS1149C0332R", "1", "EA"), ("NAS1149D0863H", "1", "EA"),
    ("NAS1352-5LE16P", "1", "EA"),
    ("MS20470AD3-4", "1", "LB"), ("MS24665-132", "5", "EA"),
]


def make_sheet(path: str) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Requests"
    ws.append(["RFQ ID", "Vendor", "Part Number", "Qty Needed", "UOM", "Condition Wanted",
               "Requested By", "Status", "Run ID", "Notes"])
    for i, (part, qty, uom) in enumerate(REQUESTS):
        ws.append([f"DRY{i + 2}", "dasi", part, qty, uom, None, "dry@example.com", None, None, None])
    wb.save(path)


async def main(saved_page: str, out: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    html = open(saved_page, encoding="utf-8", errors="ignore").read()
    make_sheet(out)
    os.environ["WRITE_RESULTS_TAB"] = "1"
    settings = Settings.from_env()
    llm = build_llm_client(settings.llm_provider, settings.llm_model, settings.llm_api_key,
                           settings.llm_effort)
    real_factory = default_adapter_factory(Extractor(llm), settings.max_page_chars)

    def factory(vendor_id, selectors, config):
        adapter = real_factory(vendor_id, selectors, config)

        async def login(page, cred):
            async def serve(route):
                if "store.dasi.com/product-search" in route.request.url:
                    await route.fulfill(status=200, content_type="text/html", body=html)
                else:
                    await route.abort()          # no request leaves the machine
            await page.route("**/*", serve)

        async def yes(page):
            return True

        async def no(page):
            return False

        adapter.login, adapter.is_authenticated, adapter.logout = login, yes, yes
        adapter._signed_out_here = no
        return adapter

    sheets = open_sheet(out, "Requests", "Results")
    deps = Deps(
        sheets=sheets, store=MemoryStore(),
        creds=MemoryCredentialProvider({"sourcing-vendor-dasi": {"username": "dry", "password": "dry"}}),
        browser=LocalBrowserClient(headless=True),
        sink=SheetSink(sheets, rfq=True, results=True),
        notifier=ChatNotifier(""), adapter_factory=factory,
        llm_label=f"{settings.llm_provider} {settings.llm_model}, effort {settings.llm_effort}",
    )
    summary = await Pipeline(deps, "dry-run").run()
    print("SUMMARY", summary)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
