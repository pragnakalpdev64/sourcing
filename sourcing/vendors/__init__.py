"""Vendor adapters in code. Which vendors exist, their names, aliases and login
URLs come from the vendor records in the store (see registry.py); this module
only maps an adapter name to its class."""
from __future__ import annotations

from typing import Optional

from .aar import AarAdapter
from .aircraft_spruce import AircraftSpruceAdapter
from .aviall import AviallAdapter
from .base import VendorAdapter
from .dasi import DasiAdapter
from .generic import GenericAdapter
from .incora import IncoraAdapter
from .monroe import MonroeAdapter
from .proponent import ProponentAdapter
from .wencor import WencorAdapter

# Adapter names a vendor record can pick with "adapter": "<name>". "generic"
# logs in and searches any portal from its record alone (generic.py).
ADAPTER_TYPES: dict[str, type[VendorAdapter]] = {
    "generic": GenericAdapter,
    "proponent": ProponentAdapter,
    "dasi": DasiAdapter,
    "aviall": AviallAdapter,
    "wencor": WencorAdapter,
    "aar": AarAdapter,
    "monroe": MonroeAdapter,
    "aircraft_spruce": AircraftSpruceAdapter,
    "incora": IncoraAdapter,
}

# Vendors that work without a record (built before the registry), by the
# slugs team members type in the sheet.
ADAPTERS: dict[str, type[VendorAdapter]] = {
    "proponent": ProponentAdapter,
    "procart": ProponentAdapter,
    "dasi": DasiAdapter,
    "aviall": AviallAdapter,
    "wencor": WencorAdapter,
    "aar": AarAdapter,
    "aarcorp": AarAdapter,
    "aar_corp": AarAdapter,
    "monroe": MonroeAdapter,
    "monroeaerospace": MonroeAdapter,
    "monroe_aerospace": MonroeAdapter,
    "aircraft_spruce": AircraftSpruceAdapter,
    "aircraftspruce": AircraftSpruceAdapter,
    "aircraft_spruce_specialty": AircraftSpruceAdapter,
    "aircraft_spruce_specialty_co": AircraftSpruceAdapter,
    "spruce": AircraftSpruceAdapter,
    "incora": IncoraAdapter,
    "shop_incora": IncoraAdapter,
    "wesco": IncoraAdapter,
    "wesco_aircraft": IncoraAdapter,
}


def adapter_class(vendor_id: str, record: Optional[dict] = None) -> Optional[type[VendorAdapter]]:
    """The adapter for a vendor: the record's `adapter` when it names one, else the
    built-in adapter for this id. None = no adapter can run this vendor."""
    name = (record or {}).get("adapter")
    # A record the worker made from the login secret ("generic") before this
    # vendor had its own adapter doesn't keep it on the generic one (DASI, 2026-10-29).
    if name and not ((record or {}).get("auto_created_from") and vendor_id in ADAPTERS):
        return ADAPTER_TYPES.get(name)
    return ADAPTERS.get(vendor_id)


def canonical_vendor_id(vendor_id: str) -> str:
    cls = ADAPTERS.get(vendor_id)
    return cls.vendor_id if cls else vendor_id
