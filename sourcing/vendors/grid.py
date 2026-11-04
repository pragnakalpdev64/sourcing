"""Read a results grid's own data instead of its flattened page text.

Many portals render results with a JS grid (jqGrid on Procart) that holds every
row as a record with named fields. Read as page text, the table loses its rows
and columns and the extractor has to guess which value belongs to which part;
read as records, each part's fields arrive together and labeled. Callers fall
back to page text when a page has no such grid.
"""
from __future__ import annotations

import html
import json
import re
from typing import Optional

# All rows of a jqGrid, every page included when it was loaded with
# loadonce:true (getGridParam('data')); else the rows on the current page.
# Returns null when there is no jQuery / jqGrid / grid. Functions and DOM
# references are dropped so the result is plain JSON.
GRID_RECORDS_JS = """
(selector) => {
  const $ = window.jQuery;
  if (!$ || !$.fn || !$.fn.jqGrid) return null;
  let grid = selector ? $(selector) : $();
  if (!grid.length) grid = $('table.ui-jqgrid-btable').first();
  if (!grid.length) return null;
  let rows = grid.jqGrid('getGridParam', 'data');
  if (!rows || !rows.length) {
    rows = (grid.jqGrid('getDataIDs') || []).map(id =>
      grid.jqGrid('getLocalRow', id) || grid.jqGrid('getRowData', id));
  }
  const total = grid.jqGrid('getGridParam', 'records');
  // Text of each drawn row's expanded detail row (a jqGrid subgrid), keyed by
  // row id: product details such as the part condition live only there.
  const details = {};
  for (const id of (grid.jqGrid('getDataIDs') || [])) {
    const tr = document.getElementById(id);
    const sub = tr && tr.nextElementSibling;
    if (sub && /ui-subgrid/.test(sub.className || '')) {
      const text = (sub.innerText || '').replace(/[ \\t]+/g, ' ').replace(/\\n\\s*\\n+/g, '\\n').trim();
      if (text) details[id] = text.slice(0, 4000);
    }
  }
  const seen = new WeakSet();
  const clean = JSON.stringify(rows || [], (k, v) => {
    if (typeof v === 'function' || (v && v.nodeType)) return undefined;
    if (v && typeof v === 'object') { if (seen.has(v)) return undefined; seen.add(v); }
    return v;
  });
  return {id: grid.attr('id') || null, total: typeof total === 'number' ? total : null,
          rows: JSON.parse(clean), details};
}
"""

# Grid bookkeeping fields that say nothing about the part.
NOISE_KEYS = {"_id_", "id", "rowid", "rn", "cb", "subgrid"}
MAX_VALUE_CHARS = 500


def _text(value) -> str:
    """A field value as plain text: HTML tags stripped, entities decoded."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"\s+", " ", text).strip()[:MAX_VALUE_CHARS]


def _fields(record: dict, indent: str = "  ") -> list[str]:
    lines = []
    for key, value in record.items():
        if key.lower() in NOISE_KEYS or value is None or value is False or value == "":
            continue
        if isinstance(value, list):
            items = [v for v in value if isinstance(v, dict)]
            for n, item in enumerate(items, start=1):
                sub = _fields(item, indent + "    ")
                if sub:
                    lines.append(f"{indent}{key} #{n}:")
                    lines.extend(sub)
            if not items and value:
                lines.append(f"{indent}{key}: {_text(', '.join(map(str, value)))}")
            continue
        if isinstance(value, dict):
            sub = _fields(value, indent + "    ")
            if sub:
                lines.append(f"{indent}{key}:")
                lines.extend(sub)
            continue
        text = _text(value)
        if text:
            lines.append(f"{indent}{key}: {text}")
    return lines


ROW_ID_KEYS = ("_id_", "id", "Id", "rowid")


def format_records(rows: list[dict], details: Optional[dict] = None) -> str:
    """Grid records as labeled text blocks, one per result line, each followed by
    the text of its expanded detail row when there is one."""
    details = details or {}
    blocks = []
    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        body = _fields(row)
        rid = next((str(row[k]) for k in ROW_ID_KEYS if row.get(k) not in (None, "")), None)
        extra = details.get(rid) if rid is not None else None
        if extra:
            body.append("  Expanded details (as shown on the page):")
            body.extend("    " + line for line in extra.splitlines() if line.strip())
        if body:
            blocks.append(f"Record {n}:\n" + "\n".join(body))
    return "\n\n".join(blocks)


def records_header(count: int, grid_id: Optional[str]) -> str:
    return (f"Results read from the page's results grid{f' (#{grid_id})' if grid_id else ''}: "
            f"{count} record(s), one per result line. Field names are the site's own.\n\n")


def parse_grid_result(result) -> Optional[tuple[str, int, Optional[int]]]:
    """GRID_RECORDS_JS result -> (text, records, grid's own total), or None."""
    if not isinstance(result, dict):
        return None
    rows = [r for r in (result.get("rows") or []) if isinstance(r, dict)]
    if not rows:
        return None
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    return (records_header(len(rows), result.get("id")) + format_records(rows, details),
            len(rows), result.get("total"))


def dumps(result) -> str:
    return json.dumps(result, indent=1, default=str)
