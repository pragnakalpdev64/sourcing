"""Provider-agnostic LLM interface.

`extract.py` builds the prompt and JSON schema once; a provider only turns
that into a structured-output API call and reports token usage back.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import LlmUsage


class LlmError(Exception):
    pass


class LlmClient(ABC):
    model: str

    @abstractmethod
    async def json_call(self, *, system: str, prompt: str, schema: dict,
                        max_tokens: int) -> tuple[Optional[dict], LlmUsage]:
        """Return the schema-shaped response, or None if the model declined/was cut off."""
