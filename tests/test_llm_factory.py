import pytest

from sourcing.llm import build_llm_client
from sourcing.llm.base import LlmError


def test_unknown_provider_lists_the_valid_ones():
    with pytest.raises(LlmError, match="anthropic.*gemini.*openai"):
        build_llm_client("bedrock", "some-model", "key", "medium")


def test_missing_api_key_names_the_provider():
    with pytest.raises(LlmError, match="LLM_PROVIDER=openai"):
        build_llm_client("openai", "gpt-5", "", "")


def test_anthropic_builds_without_a_real_network_call():
    client = build_llm_client("anthropic", "some-model", "fake-key", "medium")
    assert client.model == "some-model" and client.effort == "medium"


@pytest.mark.parametrize("provider, model, effort, message", [
    ("", "some-model", "medium", "LLM_PROVIDER is not set"),
    ("anthropic", "", "medium", "LLM_MODEL is not set"),
    ("anthropic", "some-model", "", "LLM_EFFORT is not set"),
    ("anthropic", "some-model", "max", "LLM_EFFORT is 'max'"),
])
def test_no_default_model_effort_or_provider(provider, model, effort, message):
    with pytest.raises(LlmError, match=message):
        build_llm_client(provider, model, "fake-key", effort)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_providers_without_their_sdk_installed_fail_on_import_not_silently(provider):
    # Both SDKs are optional (requirements.txt) and not installed in this env;
    # the point is a clear ModuleNotFoundError, not that the provider itself works.
    with pytest.raises(ImportError):
        build_llm_client(provider, "some-model", "fake-key", "")
