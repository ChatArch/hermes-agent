"""Unit tests for Copilot/GitHub Models reasoning effort normalization."""

from __future__ import annotations

import pytest


@pytest.fixture
def copilot_profile():
    """Resolve the registered Copilot provider profile."""
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("copilot")
    assert profile is not None, "copilot provider profile must be registered"
    return profile


class TestCopilotReasoningEffort:
    @pytest.mark.parametrize("effort", ["xhigh", "max", "ultra"])
    def test_stronger_efforts_clamp_to_high_when_only_high_is_supported(
        self,
        copilot_profile,
        monkeypatch,
        effort,
    ):
        import hermes_cli.models as models

        monkeypatch.setattr(
            models,
            "github_model_reasoning_efforts",
            lambda _model: ["low", "medium", "high"],
        )

        extra_body, top_level = copilot_profile.build_api_kwargs_extras(
            model="gpt-5.4",
            supports_reasoning=True,
            reasoning_config={"enabled": True, "effort": effort},
        )

        assert extra_body == {"reasoning": {"effort": "high"}}
        assert top_level == {}

    def test_native_max_is_preserved_when_catalog_supports_it(
        self,
        copilot_profile,
        monkeypatch,
    ):
        import hermes_cli.models as models

        monkeypatch.setattr(
            models,
            "github_model_reasoning_efforts",
            lambda _model: ["low", "medium", "high", "max"],
        )

        extra_body, _ = copilot_profile.build_api_kwargs_extras(
            model="future-model",
            supports_reasoning=True,
            reasoning_config={"enabled": True, "effort": "max"},
        )

        assert extra_body == {"reasoning": {"effort": "max"}}

    def test_disabled_reasoning_omits_reasoning_payload(
        self,
        copilot_profile,
        monkeypatch,
    ):
        import hermes_cli.models as models

        monkeypatch.setattr(
            models,
            "github_model_reasoning_efforts",
            lambda _model: ["low", "medium", "high"],
        )

        extra_body, _ = copilot_profile.build_api_kwargs_extras(
            model="gpt-5.4",
            supports_reasoning=True,
            reasoning_config={"enabled": False},
        )

        assert extra_body == {}
