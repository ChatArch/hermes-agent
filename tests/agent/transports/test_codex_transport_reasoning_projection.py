"""OpenAI reasoning projection on the Responses wire."""

import pytest
from agent.transports import get_transport


@pytest.fixture
def transport():
    import agent.transports.codex
    return get_transport("codex_responses")


class TestOpenAIReasoningWireProjection:
    """Explicit ``reasoning_effort: none`` and non-reasoning OpenAI models on the Responses wire
    (#75227, #76255): a disable is sent as ``effort: none`` where the model accepts it — omitting the
    field leaves the model's default effort on — and chat-era models on api.openai.com, which 400 on any
    ``reasoning`` key, get no ``reasoning`` field at all."""

    OPENAI = "https://api.openai.com/v1"

    def _reasoning(self, transport, model, reasoning_config, base_url=OPENAI):
        kw = transport.build_kwargs(model=model, messages=[{"role": "user", "content": "Hi"}], tools=[],
                                    base_url=base_url, reasoning_config=reasoning_config)
        return kw.get("reasoning")

    def test_explicit_none_is_sent_and_unset_keeps_the_default(self, transport):
        assert self._reasoning(transport, "gpt-5.6-sol", {"enabled": False}) == {"effort": "none"}
        assert self._reasoning(transport, "gpt-5.6-sol", None) == {"effort": "medium", "summary": "auto"}
        # Astra's vocabulary has no ``none``: nothing to send, never an escalated level.
        assert self._reasoning(transport, "gpt-6-astra", {"enabled": False}) is None

    def test_disable_the_route_cannot_express_is_reported_once(self, transport, caplog):
        """#75227: a disable the vocabulary cannot carry (Astra has no ``none``) is reported as an unsupported
        configuration — the model's default effort stays on — instead of silently omitted; once per model."""
        import logging
        from agent.transports import codex as codex_transport
        codex_transport._UNPROJECTABLE_DISABLE_WARNED.discard("gpt-6-astra")
        with caplog.at_level(logging.WARNING, logger="agent.transports.codex"):
            for _ in range(2):
                assert self._reasoning(transport, "gpt-6-astra", {"enabled": False}) is None
        warned = [r.getMessage() for r in caplog.records
                  if r.name == "agent.transports.codex" and r.levelno >= logging.WARNING]
        assert len(warned) == 1 and "gpt-6-astra" in warned[0], caplog.text

    @pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1-mini", "openai/gpt-4o", "ft:gpt-4o-mini:acme::abc1"])
    def test_chat_era_openai_models_get_no_reasoning_field_on_the_official_origin(self, transport, model):
        for rc in (None, {"enabled": True, "effort": "high"}, {"enabled": False}):
            assert self._reasoning(transport, model, rc) is None, (model, rc)
        # Reasoning models on the same origin and the same id on a relay keep the dial (the relay may translate).
        assert self._reasoning(transport, "o4-mini", None) == {"effort": "medium", "summary": "auto"}
        assert self._reasoning(transport, model, {"enabled": True, "effort": "high"},
                               base_url="https://relay.example.com/v1") == {"effort": "high", "summary": "auto"}
