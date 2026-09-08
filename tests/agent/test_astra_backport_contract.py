"""Model-contract regression coverage, independent of module extraction layout."""
from agent import model_metadata
from agent.reasoning_effort import codex_supported_efforts


def test_astra_codex_max_effort_is_preserved():
    assert "max" in codex_supported_efforts("gpt-6-astra")
    assert "none" not in codex_supported_efforts("gpt-6-astra")


def test_astra_oauth_fallback_is_below_direct_api_window():
    context, source = model_metadata._resolve_codex_oauth_context_length_with_source("gpt-6-astra")
    assert source == "fallback"
    assert context < model_metadata.DEFAULT_CONTEXT_LENGTHS["gpt-6-astra"]
