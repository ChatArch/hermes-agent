"""Account-gated Astra must not be injected through the saved-model picker path."""
import pytest
from hermes_cli.model_switch_providers import list_authenticated_providers


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-astra-900k"])
@pytest.mark.parametrize("discovered", [False, True])
def test_authenticated_picker_requires_discovered_astra(monkeypatch, model, discovered):
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda *a, **kw: {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    catalog = ["gpt-5.6-sol"] + ([model] if discovered else [])
    monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", lambda slug, **kw: list(catalog))
    rows = list_authenticated_providers(
        current_provider="openai-api", current_model=model, user_providers={}, custom_providers=[]
    )
    row = next(row for row in rows if row["slug"] == "openai-api")
    assert (model in row["models"]) is discovered
    assert row["total_models"] == len(row["models"])
