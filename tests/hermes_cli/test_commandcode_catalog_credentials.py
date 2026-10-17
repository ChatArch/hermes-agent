"""The bundled CommandCode credential contract survives external catalog additions."""
from types import SimpleNamespace

import pytest

from hermes_cli import providers


@pytest.mark.parametrize("catalog_keys", [("CMD_API_KEY",), ("COMMANDCODE_API_KEY", "CMD_API_KEY")])
def test_commandcode_catalog_keeps_bundled_key_and_endpoint_override(monkeypatch, catalog_keys):
    info = SimpleNamespace(name="CommandCode", env=catalog_keys,
                           api="https://api.commandcode.ai/provider/v1", doc="")
    monkeypatch.setattr(providers, "_models_dev_info", lambda *_args, **_kwargs: info)

    resolved = providers.resolve_provider_full("commandcode", {}, [])

    assert resolved is not None
    assert set(resolved.api_key_env_vars) == {"CMD_API_KEY", "COMMANDCODE_API_KEY"}
    assert len(resolved.api_key_env_vars) == len(set(resolved.api_key_env_vars))
    assert resolved.base_url_env_var == "COMMANDCODE_BASE_URL"
    assert resolved.transport == "openai_chat"


def test_commandcode_offline_resolution_keeps_a_concrete_endpoint(monkeypatch):
    monkeypatch.setattr(providers, "_models_dev_info", lambda *_args, **_kwargs: None)

    resolved = providers.resolve_provider_full("commandcode", {}, [])

    assert resolved is not None
    assert resolved.base_url == "https://api.commandcode.ai/provider/v1"
    assert "COMMANDCODE_API_KEY" in resolved.api_key_env_vars
