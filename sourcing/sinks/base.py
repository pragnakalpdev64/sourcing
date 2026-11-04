"""Output interface. The pipeline only ever calls `sink.write()`.

SheetSink (prototype) writes the Results tab. AvSightSink (later) will write
into AvSight via the Salesforce API, with no change to the search code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from ..models import ResultRow


@dataclass
class WriteOutcome:
    ok: bool
    error: Optional[str] = None
    # Results saved but the same rows' RFQ tab write failed: the pipeline keeps
    # them and writes them on the next run (SheetSink.write_rfq).
    rfq_error: Optional[str] = None


class ResultSink(Protocol):
    async def write(self, rows: list[ResultRow]) -> list[WriteOutcome]: ...
