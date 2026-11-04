"""Vendor registry: vendor records in the store decide which vendors exist, their
names, aliases and login sites (docs/GENERIC_VENDOR_PLAN.md phase 2)."""
from __future__ import annotations

import json

import pytest

from sourcing.config import get_settings
from sourcing.credentials.base import VendorCredential
from sourcing.store import FileStore, MemoryStore
from sourcing.vendors import adapter_class
from sourcing.vendors.generic import GenericAdapter
from sourcing.vendors.proponent import ProponentAdapter
from sourcing.vendors.registry import VendorRegistry

from .test_pipeline import JANE, make, run

WENCOR = {"display_name": "Wencor", "aliases": ["Wencor Group", "WG"], "adapter": "generic",
          "enabled": True, "login_url": "https://shop.wencor.com/login",
          "allowed_hosts": ["shop.wencor.com"]}


def registry(**records) -> VendorRegistry:
    store = MemoryStore()
    for vendor_id, record in records.items():
        store.update_vendor(vendor_id, record)
    return VendorRegistry(store)


def test_vendor_cell_resolves_by_id_alias_or_built_in_name():
    reg = registry(wencor=WENCOR)
    assert reg.resolve_id("Wencor") == "wencor"
    assert reg.resolve_id("wencor group") == "wencor"        # alias, any case / spacing
    assert reg.resolve_id(" WG ") == "wencor"
    assert reg.resolve_id("Procart") == "proponent"           # built-in alias, no record
    assert reg.resolve_id("Boeing") == "boeing"               # unknown: its slug
    assert reg.resolve_id("") == ""


def test_problem_per_vendor_state():
    reg = registry(wencor={**WENCOR, "enabled": False}, acme={**WENCOR, "adapter": "magic"},
                   example=WENCOR)
    assert reg.problem("proponent") is None                   # built-in, no record
    assert reg.problem("example") is None                     # generic adapter, enabled
    assert "not enabled yet" in reg.problem("wencor") and not reg.enabled("wencor")
    assert "adapter 'magic' is not available" in reg.problem("acme")
    assert "not supported yet" in reg.problem("boeing")


def test_built_in_vendor_can_be_disabled_or_renamed_by_a_record():
    reg = registry(proponent={"enabled": False, "display_name": "Proponent (Procart)"})
    assert not reg.enabled("proponent")
    assert reg.display_name("proponent") == "Proponent (Procart)"
    assert registry().display_name("proponent") == "Proponent"


def test_record_adapter_picks_the_class():
    assert adapter_class("proponent") is ProponentAdapter
    assert adapter_class("acme", {"adapter": "proponent"}) is ProponentAdapter
    assert adapter_class("acme", {"adapter": "generic"}) is GenericAdapter
    assert adapter_class("acme", {"adapter": "magic"}) is None
    assert adapter_class("acme") is None


def test_file_store_keeps_vendor_records(tmp_path):
    path = str(tmp_path / "store.json")
    FileStore(path).update_vendor("wencor", WENCOR)
    store = FileStore(path)
    store.update_vendor("wencor", {"enabled": False})              # merged, not replaced
    reloaded = FileStore(path).list_vendors()
    assert reloaded["wencor"]["enabled"] is False
    assert reloaded["wencor"]["login_url"] == WENCOR["login_url"]


def test_record_name_and_website_reach_the_adapter():
    adapter = ProponentAdapter(vendor_config={"display_name": "Procart",
                                              "website": "https://procart.proponent.com/"})
    assert adapter.display_name == "Procart"
    cred = VendorCredential("proponent", "", "sourcing-vendor-proponent", "u", "p", None)
    sources = [source for source, _ in adapter.login_url_candidates(cred)]
    assert sources == ["vendor record website", "adapter default"]
    off_site = ProponentAdapter(vendor_config={"website": "https://evil.example.com/"})
    assert all(url != "https://evil.example.com/" for _, url in off_site.login_url_candidates(cred))


# -- in the pipeline -------------------------------------------------------------

@pytest.mark.asyncio
async def test_alias_in_the_sheet_runs_as_that_vendor():
    deps, sheet, _ = make([{"vendor": "PC Portal", "part": "P1", "by": JANE}])
    deps.store.update_vendor("proponent", {"aliases": ["pc portal"],
                                           "display_name": "Proponent (Procart)"})
    summary = await run(deps)
    assert sheet.status_of("P1") == "DONE"
    assert summary["counts"] == {"DONE": 1}


@pytest.mark.asyncio
async def test_disabled_vendor_rows_fail_with_the_reason():
    deps, sheet, portal = make([{"vendor": "Proponent", "part": "P1", "by": JANE}])
    deps.store.update_vendor("proponent", {"enabled": False})
    await run(deps)
    assert sheet.status_of("P1") == "FAILED" and "not enabled yet" in sheet.notes[2]
    assert portal.logins == []                                 # never logged in


@pytest.mark.asyncio
async def test_record_without_a_built_adapter_fails_clearly():
    deps, sheet, _ = make([{"vendor": "Wencor", "part": "P1", "by": JANE}])
    deps.store.update_vendor("wencor", {**WENCOR, "adapter": "magic"})
    await run(deps)
    assert sheet.status_of("P1") == "FAILED"
    assert "adapter 'magic' is not available" in sheet.notes[2]


# -- scripts/manage_vendors.py ---------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch):
    import scripts.manage_vendors as manage_vendors

    path = tmp_path / "store.json"
    monkeypatch.setenv("STORE_BACKEND", "file")
    monkeypatch.setenv("STORE_FILE", str(path))
    get_settings.cache_clear()
    yield manage_vendors.main, lambda: json.loads(path.read_text())["vendors"]
    get_settings.cache_clear()


def test_cli_add_checks_the_login_site(cli, capsys):
    main, _ = cli
    assert main(["add", "acme", "--login-url", "http://shop.acme.com/login",
                 "--hosts", "shop.acme.com"]) == 1
    assert main(["add", "acme", "--login-url", "https://evil.example.com/login",
                 "--hosts", "shop.acme.com"]) == 1
    assert main(["add", "acme", "--login-url", "https://shop.acme.com/login"]) == 1   # no hosts
    assert "Nothing saved." in capsys.readouterr().out


def test_cli_add_enable_set(cli, capsys):
    main, vendors = cli
    assert main(["add", "Acme", "--name", "Acme", "--login-url",
                 "https://shop.acme.com/login", "--hosts", "shop.acme.com",
                 "--aliases", "Acme Group, WG"]) == 0
    record = vendors()["acme"]
    assert record["enabled"] is False and record["adapter"] == "generic"
    assert record["aliases"] == ["Acme Group", "WG"]
    with pytest.raises(SystemExit):                            # already has a record
        main(["add", "acme", "--login-url", "https://shop.acme.com/x", "--hosts", "shop.acme.com"])

    assert main(["enable", "acme"]) == 0
    assert vendors()["acme"]["enabled"] is True
    assert main(["set", "acme", "adapter=magic"]) == 1          # unknown adapter refused

    assert main(["set", "acme", "currency=USD", 'not_sold_phrases=["no longer sold"]']) == 0
    assert vendors()["acme"]["not_sold_phrases"] == ["no longer sold"]
    assert main(["set", "acme", "login_url=https://evil.example.com/"]) == 1     # refused
    assert vendors()["acme"]["login_url"] == "https://shop.acme.com/login"
    assert main(["disable", "acme"]) == 0


def test_cli_built_in_vendor_can_be_disabled_and_enabled(cli):
    main, vendors = cli
    assert main(["disable", "proponent"]) == 0
    assert vendors()["proponent"] == {"enabled": False}
    assert main(["enable", "proponent"]) == 0
    assert vendors()["proponent"]["enabled"] is True


def test_cli_export_then_import_into_a_fresh_store(cli, tmp_path, monkeypatch):
    main, vendors = cli
    seed = tmp_path / "vendors.json"
    assert main(["add", "wencor", "--name", "Wencor", "--login-url",
                 "https://shop.wencor.com/login", "--hosts", "shop.wencor.com"]) == 0
    assert main(["export", str(seed)]) == 0
    exported = json.loads(seed.read_text())
    assert exported["wencor"]["display_name"] == "Wencor" and "added_at" not in exported["wencor"]

    # a fresh store (e.g. Firestore on Cloud Run) gets the records; re-running is harmless
    fresh = tmp_path / "fresh.json"
    monkeypatch.setenv("STORE_FILE", str(fresh))
    get_settings.cache_clear()
    assert main(["import", str(seed)]) == 0
    assert json.loads(fresh.read_text())["vendors"]["wencor"]["enabled"] is False
    assert main(["set", "wencor", "currency=EUR"]) == 0
    assert main(["import", str(seed)]) == 0                    # existing record kept
    assert json.loads(fresh.read_text())["vendors"]["wencor"]["currency"] == "EUR"

    seed.write_text(json.dumps({"bad": {"login_url": "https://evil.example.com/",
                                        "allowed_hosts": ["shop.bad.com"]}}))
    assert main(["import", str(seed)]) == 1                    # checked like add
    assert "bad" not in json.loads(fresh.read_text())["vendors"]
