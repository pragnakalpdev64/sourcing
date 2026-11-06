"""A rejected LLM key stops an account's searches (runs 2026-11-05 16:37 / 16:44: an
invalid Anthropic key made the pipeline search Incora's live portal ~40 times)."""
from __future__ import annotations

import httpx

from sourcing.pipeline import llm_key_rejected


def _anthropic_error(status: int):
    import anthropic

    cls = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
           529: anthropic.InternalServerError}[status]
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com"))
    return cls("invalid x-api-key", response=response, body=None)


def test_only_an_llm_providers_401_or_403_counts():
    assert llm_key_rejected(_anthropic_error(401))
    assert llm_key_rejected(_anthropic_error(403))
    assert not llm_key_rejected(_anthropic_error(529))          # overloaded: retry is fine

    class PortalError(Exception):
        status_code = 401                                      # a vendor page, not the LLM
    assert not llm_key_rejected(PortalError())
    assert not llm_key_rejected(RuntimeError("401"))
