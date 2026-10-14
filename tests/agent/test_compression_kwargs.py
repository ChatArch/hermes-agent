"""Compression keyword negotiation and no-LLM rescue routing contracts."""

from types import SimpleNamespace

import pytest

from agent import conversation_compression as cc
from agent.conversation_compression_kwargs import _supported_compression_kwargs


def test_parent_reexport_and_monkeypatch_remain_effective(monkeypatch):
    assert cc._supported_compression_kwargs is _supported_compression_kwargs
    compressor = SimpleNamespace(compress=lambda messages: messages)
    agent = SimpleNamespace(context_compressor=compressor)
    seen = []

    def negotiate(fn, **kwargs):
        seen.append((fn, kwargs))
        return {"local_fallback_only": True}

    monkeypatch.setattr(cc, "_supported_compression_kwargs", negotiate)
    fn, kwargs = cc._resolve_compress_call(
        agent, approx_tokens=123, focus_topic=None, force=True,
        memory_context="", bypass_cooldown=False, local_fallback_only=True,
    )
    assert fn is compressor.compress
    assert kwargs == {"local_fallback_only": True}
    assert seen[0][0] is fn
    assert seen[0][1]["local_fallback_only"] is True


@pytest.mark.parametrize("error", [TypeError, ValueError])
def test_uninspectable_engine_rescue_is_refused_without_invocation(error):
    calls = []

    class Engine:
        @property
        def __signature__(self):
            raise error("signature unavailable")

        def __call__(self, messages, **kwargs):
            calls.append(kwargs)
            return messages

    agent = SimpleNamespace(context_compressor=SimpleNamespace(compress=Engine()))
    with pytest.raises(TypeError, match="context compressor does not support local_fallback_only"):
        cc._resolve_compress_call(
            agent, approx_tokens=123, focus_topic=None, force=True,
            memory_context="", bypass_cooldown=False, local_fallback_only=True,
        )
    assert calls == []


@pytest.mark.parametrize("checkpoint_required", [False, True])
def test_codex_rescue_refuses_before_route_or_checkpoint(monkeypatch, checkpoint_required):
    agent = SimpleNamespace(api_mode="codex_app_server",
                            compression_checkpoint_required=checkpoint_required)
    entered = []
    monkeypatch.setattr(cc, "_begin_compression_attempt", lambda *args, **kwargs: entered.append("begin"))

    def forbidden(*args, **kwargs):
        pytest.fail("Codex route/checkpoint branch must not run for local rescue")

    monkeypatch.setattr(cc, "_route_codex_compaction", forbidden)
    monkeypatch.setattr(cc, "_checkpoint_blocked", forbidden)
    with pytest.raises(TypeError, match="codex_app_server does not support local_fallback_only"):
        cc.compress_context(agent, [], "sys", local_fallback_only=True)
    assert entered == ["begin"]
