"""Claude. The original provider; unverified against the real Proponent pages
(see README 'First real Proponent run'), but proven against the Anthropic API."""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..models import LlmUsage
from .base import LlmClient

logger = logging.getLogger("sourcing.llm.anthropic")

# $ per 1M tokens (input, output, cache read).
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class AnthropicClient(LlmClient):
    def __init__(self, api_key: str, model: str, effort: str) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key or None)
        self.model = model
        self.effort = effort

    def _usage(self, response) -> LlmUsage:
        u = response.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        price_in, price_out, price_cache = PRICING.get(self.model, (0.0, 0.0, 0.0))
        cost = (u.input_tokens * price_in + u.output_tokens * price_out
                + cache_read * price_cache) / 1_000_000
        return LlmUsage(1, u.input_tokens, u.output_tokens, cache_read, cost)

    async def json_call(self, *, system: str, prompt: str, schema: dict,
                        max_tokens: int) -> tuple[Optional[dict], LlmUsage]:
        response = await self._client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": schema}},
            # Re-run on another model server-side if a safety classifier declines.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        usage = self._usage(response)
        if response.stop_reason in ("refusal", "max_tokens"):
            logger.warning("Claude stopped with %s (request %s)",
                           response.stop_reason, response._request_id)
            return None, usage
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text), usage
