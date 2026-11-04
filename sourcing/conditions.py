"""Part condition codes used in the Results sheet, and how vendor wording maps
to them. One table for every vendor site: the extraction prompt is built from
it, and `normalize_condition` cleans up whatever the model returns."""
from __future__ import annotations

import re
from typing import Optional

# code -> (meaning, wordings vendors use; matched case-insensitively). Exactly the
# client's table (their quote-reading rules, inputai-skill.md, 2026-10-19): only
# these wordings count, so e.g. "Tested", "Service" or "Repair" alone map to nothing.
CONDITION_CODES: dict[str, tuple[str, list[str]]] = {
    "AR": ("As Removed", ["as removed", "ar"]),
    "FN": ("Factory New", ["factory new", "fn"]),
    "NE": ("New", ["new", "ne"]),
    "NS": ("New Surplus", ["new surplus", "ns", "govt new surplus"]),
    "OH": ("Overhauled", ["overhauled", "oh"]),
    "RP": ("Repaired", ["repaired", "rp"]),
    "SV": ("Serviceable", ["serviceable", "sv"]),
}

# wording -> code, longest first so "new surplus" wins over "new"
_WORDINGS = sorted(((w, code) for code, (_, words) in CONDITION_CODES.items() for w in words),
                   key=lambda x: -len(x[0]))


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalize_condition(raw: Optional[str]) -> Optional[str]:
    """Vendor wording -> code (AR, FN, NE, NS, OH, RP, SV). Text that matches
    none of them gives None: the client's rule is "return the code only; if not
    found, omit the field"."""
    if raw is None or not str(raw).strip():
        return None
    text = _clean(str(raw))
    for wording, code in _WORDINGS:
        if text == wording:
            return code
    # "Condition: Factory New", "NE - New", "Govt New Surplus (NS)": whole words
    # only, longest wording first, so "New Surplus" is NS and not NE.
    for wording, code in _WORDINGS:
        if re.search(rf"\b{re.escape(wording)}\b", text):
            return code
    return None


def prompt_table() -> str:
    """The code list as the extraction prompt shows it."""
    return "; ".join(f"{code} = {meaning} ({', '.join(repr(w) for w in words)})"
                     for code, (meaning, words) in CONDITION_CODES.items())
