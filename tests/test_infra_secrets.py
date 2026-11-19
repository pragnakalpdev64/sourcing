import pytest

from sourcing import config


@pytest.fixture
def env(monkeypatch):
    for var in list(config.INFRA_SECRETS) + [config._LOADED_MARK, "K_SERVICE"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-from-env")
    return monkeypatch


def test_off_by_default_keeps_env(env):
    env.delenv("INFRA_SECRETS", raising=False)
    assert config.load_infra_secrets(fetch=lambda n: "x") == []
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "old-from-env"


def test_gsm_replaces_env_values(env):
    import os
    env.setenv("INFRA_SECRETS", "gsm")
    loaded = config.load_infra_secrets(fetch=lambda n: f"{n}-value\n")
    assert set(loaded) == set(config.INFRA_SECRETS)
    assert os.environ["ANTHROPIC_API_KEY"] == "sourcing-anthropic-api-key-value"
    # once per process tree: the local worker inherits them, no second fetch
    assert config.load_infra_secrets(fetch=lambda n: 1 / 0) == []


def test_unreadable_secret_stops_instead_of_using_env(env):
    env.setenv("INFRA_SECRETS", "gsm")

    def fetch(name):
        raise PermissionError("denied")

    with pytest.raises(RuntimeError, match="sourcing-anthropic-api-key"):
        config.load_infra_secrets(fetch=fetch)


def test_cloud_run_never_fetches(env):
    env.setenv("INFRA_SECRETS", "gsm")
    env.setenv("K_SERVICE", "sourcing-ai-dev")
    assert config.load_infra_secrets(fetch=lambda n: 1 / 0) == []
