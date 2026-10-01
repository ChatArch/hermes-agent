"""ChatArch's Codex default is policy, not a claim about backend capacity."""

import pytest


@pytest.mark.parametrize("origin", ["live", "memory", "offline", "unknown"])
@pytest.mark.parametrize("advertised", [200_000, 1_500_000])
def test_unconfigured_codex_context_has_a_floor_across_discovery_paths(monkeypatch, origin, advertised):
    from agent import model_metadata as metadata

    model = "unlisted-codex-fixture" if origin == "unknown" else "gpt-5.5"
    catalogue = {model: advertised} if origin in {"live", "memory"} else {}
    monkeypatch.setattr(metadata, "_fetch_codex_oauth_context_lengths_with_source",
                        lambda *args, **kwargs: (catalogue, origin == "live"))
    # No network is involved, but the public resolver, provider routing and
    # fallback selection are real. A larger discovered value remains larger.
    actual = metadata.get_model_context_length(
        model, provider="openai-codex", api_key="fixture",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    expected = max(1_000_000, advertised) if catalogue else 1_000_000
    assert actual == expected


@pytest.mark.parametrize("configured", [96_000, 2_000_000])
def test_explicit_codex_context_wins_without_discovery(monkeypatch, configured):
    from agent import model_metadata as metadata

    def forbidden(*args, **kwargs):
        pytest.fail("an explicit context must bypass provider discovery")

    monkeypatch.setattr(metadata, "_fetch_codex_oauth_context_lengths_with_source", forbidden)
    assert metadata.get_model_context_length(
        "unlisted-codex-fixture", provider="openai-codex",
        config_context_length=configured,
    ) == configured
