from __future__ import annotations

from .base import LlmClient, LlmError

_BUILDERS = {}


def _register():
    if _BUILDERS:
        return
    from .anthropic_client import AnthropicClient
    from .gemini_client import GeminiClient
    from .openai_client import OpenAIClient

    _BUILDERS.update(anthropic=AnthropicClient, openai=OpenAIClient, gemini=GeminiClient)


# Anthropic's output effort levels. No default: model and effort must be set in
# the env so a run never uses a setting nobody chose (Cloud Run had fallen back
# to "low" while every test used "medium", seen 2026-10-15).
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh")


def build_llm_client(provider: str, model: str, api_key: str, effort: str) -> LlmClient:
    _register()
    provider = (provider or "").strip().lower()
    if not provider:
        raise LlmError(f"LLM_PROVIDER is not set; choose one of {sorted(_BUILDERS)}")
    cls = _BUILDERS.get(provider)
    if cls is None:
        raise LlmError(f"Unknown LLM_PROVIDER '{provider}'; choose one of {sorted(_BUILDERS)}")
    if not (model or "").strip():
        raise LlmError("LLM_MODEL is not set")
    if not api_key:
        raise LlmError(f"LLM_PROVIDER={provider} but its API key is not set")
    if provider == "anthropic":
        effort = (effort or "").strip().lower()
        if effort not in ANTHROPIC_EFFORTS:
            raise LlmError(f"LLM_EFFORT is {'not set' if not effort else repr(effort)}; "
                           f"choose one of {list(ANTHROPIC_EFFORTS)}")
        return cls(api_key, model, effort)
    return cls(api_key, model)
