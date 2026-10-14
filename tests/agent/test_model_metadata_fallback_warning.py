"""Context fallback diagnostics and deduplication contracts."""

import pytest
from unittest.mock import patch

from agent.model_metadata import DEFAULT_FALLBACK_CONTEXT, get_model_context_length


class TestFallbackWarning:
    """When all 9 detection methods fail, the 10th fallback should log a
    warning so users with small-context models (8K, 32K) don't silently get
    256K and hit hard-to-debug API context-length errors.

    The warning is deduped per (model, base_url) — the fallback result is
    deliberately never cached, so without dedup it would repeat on every
    resolution (e.g. once per gateway message via session hygiene).
    """

    @pytest.fixture(autouse=True)
    def _reset_warned_set(self):
        from agent import model_metadata as mm
        mm._FALLBACK_WARNED.clear()
        yield
        mm._FALLBACK_WARNED.clear()

    @staticmethod
    def _patch_all_lookups():
        from contextlib import ExitStack
        stack = ExitStack()
        for target, value in [
            ("agent.model_metadata.get_cached_context_length", None),
            ("agent.model_metadata.fetch_model_metadata", {}),
            ("agent.model_metadata.fetch_endpoint_model_metadata", {}),
            ("agent.model_metadata._query_ollama_api_show", None),
            ("agent.model_metadata._query_anthropic_context_length", None),
            ("agent.model_metadata._endpoint_scoped_context_length", None),
            ("agent.model_metadata._resolve_endpoint_context_length", None),
            ("agent.models_dev.lookup_models_dev_context", None),
        ]:
            stack.enter_context(patch(target, return_value=value))
        return stack

    def test_warning_emitted_on_fallback(self, caplog):
        import logging

        with self._patch_all_lookups():
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "totally-unknown-model-xyz",
                )

        assert result == DEFAULT_FALLBACK_CONTEXT
        # The warning must mention the model name and the config override hint.
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("totally-unknown-model-xyz" in r.getMessage() for r in warning_msgs)
        assert any("model.context_length" in r.getMessage() for r in warning_msgs)

    def test_warning_fires_once_per_model(self, caplog):
        """Repeated resolutions of the same unknown model warn only once."""
        import logging

        with self._patch_all_lookups():
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                for _ in range(3):
                    get_model_context_length("totally-unknown-model-xyz")

        fallback_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 1

    def test_warning_emitted_on_custom_endpoint_fallback(self, caplog):
        """The sibling step-3b fallback (custom/local endpoint, probes down,
        no catalog match) is the same silent-256K bug class and must warn too."""
        import logging

        with self._patch_all_lookups(), \
             patch("agent.model_metadata._query_local_context_length", return_value=None):
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "totally-unknown-model-xyz",
                    base_url="http://192.168.1.50:8080/v1",
                )

        assert result == DEFAULT_FALLBACK_CONTEXT
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("totally-unknown-model-xyz" in r.getMessage() for r in warning_msgs)
        assert any("model.context_length" in r.getMessage() for r in warning_msgs)

    def test_no_warning_when_cached(self, caplog):
        """No fallback warning when the context length is found in the cache."""
        import logging

        with patch(
            "agent.model_metadata.get_cached_context_length",
            return_value=32_000,
        ):
            with caplog.at_level(logging.WARNING, logger="agent.model_metadata"):
                result = get_model_context_length(
                    "some-model",
                    base_url="http://127.0.0.1:1/v1",
                )

        assert result == 32_000
        fallback_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 0
