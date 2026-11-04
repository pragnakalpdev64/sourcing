"""Gemini, via response_schema structured output. Not yet run against a real
page. Gemini's schema dialect doesn't support `{"anyOf": [{"type": "string"},
{"type": "null"}]}` the way extract.py's OFFERS_SCHEMA uses it for optional
fields (Gemini wants `"nullable": true` instead) - check the first real
response for silently-dropped fields before trusting this provider's output.
Add pricing per model below before trusting its cost numbers."""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import LlmUsage
from .base import LlmClient

logger = logging.getLogger("sourcing.llm.gemini")

# $ per 1M tokens (input, output, cached input). Fill in from
# https://ai.google.dev/gemini-api/docs/pricing before relying on these cost numbers.
PRICING: dict[str, tuple[float, float, float]] = {}


class GeminiClient(LlmClient):
    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key or None)
        self.model = model

    def _usage(self, response) -> LlmUsage:
        u = response.usage_metadata
        cached = getattr(u, "cached_content_token_count", 0) or 0
        price_in, price_out, price_cache = PRICING.get(self.model, (0.0, 0.0, 0.0))
        billable_in = u.prompt_token_count - cached
        cost = (billable_in * price_in + u.candidates_token_count * price_out
                + cached * price_cache) / 1_000_000
        return LlmUsage(1, u.prompt_token_count, u.candidates_token_count, cached, cost)

    async def json_call(self, *, system: str, prompt: str, schema: dict,
                        max_tokens: int) -> tuple[Optional[dict], LlmUsage]:
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=max_tokens,
            ),
        )
        usage = self._usage(response)
        candidate = response.candidates[0] if response.candidates else None
        finish_reason = str(getattr(candidate, "finish_reason", ""))
        if finish_reason not in ("", "STOP", "FinishReason.STOP") or not response.text:
            logger.warning("Gemini stopped with %s", finish_reason)
            return None, usage
        return json.loads(response.text), usage
