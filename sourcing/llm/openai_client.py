"""OpenAI, via Structured Outputs (response_format=json_schema). Not yet run
against a real page — add pricing per model below before trusting its cost
numbers; unlisted models cost $0 in LlmUsage rather than guess."""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import LlmUsage
from .base import LlmClient

logger = logging.getLogger("sourcing.llm.openai")

# $ per 1M tokens (input, output, cached input). Fill in from
# https://openai.com/api/pricing before relying on these cost numbers.
PRICING: dict[str, tuple[float, float, float]] = {}


class OpenAIClient(LlmClient):
    def __init__(self, api_key: str, model: str) -> None:
        import openai

        self._client = openai.AsyncOpenAI(api_key=api_key or None)
        self.model = model

    def _usage(self, response) -> LlmUsage:
        u = response.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        price_in, price_out, price_cache = PRICING.get(self.model, (0.0, 0.0, 0.0))
        billable_in = u.prompt_tokens - cached
        cost = (billable_in * price_in + u.completion_tokens * price_out
                + cached * price_cache) / 1_000_000
        return LlmUsage(1, u.prompt_tokens, u.completion_tokens, cached, cost)

    async def json_call(self, *, system: str, prompt: str, schema: dict,
                        max_tokens: int) -> tuple[Optional[dict], LlmUsage]:
        response = await self._client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "result", "schema": schema, "strict": True}},
        )
        usage = self._usage(response)
        choice = response.choices[0]
        if choice.finish_reason == "length" or getattr(choice.message, "refusal", None):
            logger.warning("OpenAI stopped with %s (request %s)",
                           choice.finish_reason, response.id)
            return None, usage
        return json.loads(choice.message.content), usage
