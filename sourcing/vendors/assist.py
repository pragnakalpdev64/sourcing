"""PageAssist: the LLM does a page action the coded way couldn't.

Team rule (2026-11-04): a search must give complete, correct results, never a
quietly incomplete one. On 2026-11-04 Proponent changed its warehouse dropdown
(the <select> hidden inside a custom control): the coded pick did nothing, every
other warehouse was left out, and nothing said so. So, for page actions that
fill in data (open a dropdown and pick an entry, open a pop-up):

  1 the adapter's coded way first (no LLM)
  2 a recipe the LLM found on an earlier run (saved with the vendor's selectors,
    no LLM)
  3 the LLM: it sees the visible clickable elements near the control and the goal,
    and picks ONE element per step (at most `max_steps`); the program clicks it
  4 after every step the adapter's own check says whether the goal is reached.
    Only a checked result counts; the LLM never decides that the data is right
  5 still not reached: the adapter flags the line (the sheet shows the gap)

Never offered to the LLM: anything that orders, changes or signs out (cart,
checkout, order, buy, update, save, delete, logout, ...). Every use is in
`events`, which the run summary shows per vendor.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("sourcing.vendors.assist")

# LLM picks per adapter per run (each a small call); a page whose UI changed
# needs a few, then the saved recipe does the rest.
MAX_CALLS = 12
# Never clicked, whatever the LLM says: words on the element (text, label, id,
# class, href).
RISKY = re.compile(r"cart|checkout|\border\b|\bbuy\b|purchase|\bpay\b|delete|remove|"
                   r"log ?out|sign ?out|log ?off|password|\bupdate\b|\bsave\b|submit|"
                   r"wish ?list|compare|request (a )?quote", re.I)
STEP_WAIT_MS = 800

# Visible clickable elements inside `scope` (a CSS selector; its closest
# `closest` ancestor when given), marked data-sourcing-act="<i>".
_ELEMENTS_JS = """
([scope, closest, max]) => {
  document.querySelectorAll('[data-sourcing-act]').forEach(e => e.removeAttribute('data-sourcing-act'));
  let root = document;
  if (scope) {
    const s = document.querySelector(scope);
    if (!s) return [];
    root = (closest && s.closest(closest)) || s.parentElement || s;
  }
  const visible = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const clean = t => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const els = [...root.querySelectorAll(
      'button, a[href], select, summary, [role=button], [role=combobox], [role=option], '
      + '[role=menuitem], [role=tab], [role=link], [onclick], [tabindex="0"]')]
    .filter(visible).slice(0, max);
  return els.map((el, i) => {
    el.setAttribute('data-sourcing-act', String(i));
    return {index: i, tag: el.tagName.toLowerCase(), role: el.getAttribute('role'),
            id: el.id || null, cls: clean(el.className && el.className.baseVal === undefined
                                          ? el.className : ''),
            text: clean(el.tagName === 'SELECT' ? '' : el.innerText),
            options: el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)) : null,
            aria_label: el.getAttribute('aria-label'), href: el.getAttribute('href'),
            data_value: el.getAttribute('data-value')};
  });
}
"""


def _risky(el: dict) -> bool:
    words = " ".join(str(el.get(k) or "") for k in ("text", "aria_label", "id", "href"))
    return bool(RISKY.search(words))


def _step_selector(el: dict, option_label: Optional[str]) -> Optional[str]:
    """A selector for the same kind of element on another row / run: its role,
    else its first class, else its tag. The step that picked `option_label` is
    found again by that text."""
    if el.get("role"):
        base = f"[role='{el['role']}']"
    elif el.get("cls"):
        first = el["cls"].split()[0]
        if not re.fullmatch(r"[A-Za-z_][\w-]*", first):
            return None
        base = f".{first}"
    else:
        base = el.get("tag") or None
    if base and option_label and (el.get("text") or "").strip() == option_label.strip():
        return base + "::OPTION"
    return base


class PageAssist:
    """One per adapter (one account, one run)."""

    def __init__(self, adapter, max_calls: int = MAX_CALLS) -> None:
        self.adapter = adapter
        self.max_calls = max_calls
        self.calls = 0
        self._counts: dict[str, int] = {}

    @property
    def events(self) -> list[str]:
        """What happened, once per kind with how often ("... (x6)")."""
        return [t + (f" (x{n})" if n > 1 else "") for t, n in self._counts.items()]

    async def act(self, page, *, key: str, goal: str, check: Callable[[], Awaitable[bool]],
                  scope: Optional[str] = None, closest: Optional[str] = None,
                  option_label: Optional[str] = None, max_steps: int = 2) -> bool:
        """Reach `goal` (checked by `check`) with a saved recipe, else LLM-picked
        clicks. `key` names the kind of action, for the saved recipe."""
        if await self._recipe(page, key, check, scope, closest, option_label):
            return True
        extractor = getattr(self.adapter, "extractor", None)
        if extractor is None or not hasattr(extractor, "pick_element"):
            self._event(f"{key}: not reached (no LLM to ask)")
            return False
        done: list[str] = []
        steps: list[Optional[str]] = []
        for _ in range(max_steps):
            if self.calls >= self.max_calls:
                self._event(f"{key}: not reached (LLM page-action limit {self.max_calls} used)")
                return False
            elements = [e for e in await self._elements(page, scope, closest) if not _risky(e)]
            if not elements:
                break
            ask = (f"the ONE element to click next to reach this goal: {goal}. "
                   + (f"Already clicked: {'; '.join(done)}. " if done else "")
                   + "Only an element that shows or chooses data; nothing that orders or changes "
                   "anything")
            self.calls += 1
            index, usage = await extractor.pick_element(elements, ask)
            self.adapter.usage.add(usage)
            if index is None:
                break
            el = next((e for e in elements if e["index"] == index), None)
            if el is None:
                break
            if not await self._do(page, el, option_label):
                break
            done.append(el.get("text") or el.get("aria_label") or el.get("tag"))
            steps.append(_step_selector(el, option_label))
            if await check():
                logger.info("Page action %s: reached with the LLM's %d click(s): %s", key,
                            len(done), " -> ".join(done))
                self._event(f"{key}: done with the LLM ({len(done)} click(s))")
                if all(steps):
                    self.adapter.learned[f"assist_{key}"] = json.dumps(steps)
                return True
        self._event(f"{key}: not reached, even with the LLM")
        return False

    async def _recipe(self, page, key, check, scope, closest, option_label) -> bool:
        raw = self.adapter.learned.get(f"assist_{key}") or self.adapter.selectors.get(
            f"assist_{key}")
        try:
            steps = json.loads(raw) if raw else None
        except ValueError:
            steps = None
        if not steps:
            return False
        for step in steps:
            elements = await self._elements(page, scope, closest)
            el = self._match(elements, step, option_label)
            if el is None or _risky(el) or not await self._do(page, el, option_label):
                return False
        if await check():
            self._event(f"{key}: done with the saved recipe")
            return True
        return False

    @staticmethod
    def _match(elements: list[dict], step: str, option_label: Optional[str]) -> Optional[dict]:
        option = step.endswith("::OPTION")
        base = step.removesuffix("::OPTION")
        for el in elements:
            kind = (f"[role='{el['role']}']" if el.get("role") else
                    f".{el['cls'].split()[0]}" if el.get("cls") else el.get("tag"))
            if base not in (kind, el.get("tag")):
                continue
            if option and (el.get("text") or "").strip() != (option_label or "").strip():
                continue
            return el
        return None

    async def _elements(self, page, scope, closest) -> list[dict]:
        try:
            return await page.evaluate(_ELEMENTS_JS, [scope, closest, 60]) or []
        except Exception as exc:
            logger.info("Page action: elements not listed (%s)", str(exc).splitlines()[0][:200])
            return []

    @staticmethod
    async def _do(page, el: dict, option_label: Optional[str]) -> bool:
        target = f"[data-sourcing-act='{el['index']}']"
        try:
            if el.get("tag") == "select":
                if not option_label or option_label not in (el.get("options") or []):
                    return False
                await page.select_option(target, label=option_label, timeout=5000)
            else:
                await page.click(target, timeout=5000)
            await page.wait_for_timeout(STEP_WAIT_MS)
            return True
        except Exception as exc:
            logger.info("Page action: click on %s failed (%s)", el.get("text") or el.get("tag"),
                        str(exc).splitlines()[0][:200])
            return False

    def _event(self, text: str) -> None:
        self._counts[text] = self._counts.get(text, 0) + 1
        logger.info("Page action %s", text)
