"""Tests for GitHub Models reasoning payload normalization in AIAgent."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
def test_github_models_stronger_efforts_clamp_to_high(monkeypatch, effort):
    from run_agent import AIAgent
    import hermes_cli.models as models

    monkeypatch.setattr(
        models,
        "github_model_reasoning_efforts",
        lambda _model: ["low", "medium", "high"],
    )

    agent = object.__new__(AIAgent)
    setattr(agent, "model", "gpt-5.4")
    setattr(agent, "reasoning_config", {"enabled": True, "effort": effort})

    assert agent._github_models_reasoning_extra_body() == {"effort": "high"}


def test_github_models_preserves_native_max_when_catalog_supports_it(monkeypatch):
    from run_agent import AIAgent
    import hermes_cli.models as models

    monkeypatch.setattr(
        models,
        "github_model_reasoning_efforts",
        lambda _model: ["low", "medium", "high", "max"],
    )

    agent = object.__new__(AIAgent)
    setattr(agent, "model", "future-model")
    setattr(agent, "reasoning_config", {"enabled": True, "effort": "max"})

    assert agent._github_models_reasoning_extra_body() == {"effort": "max"}
