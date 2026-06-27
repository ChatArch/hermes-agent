"""Tests for the gateway /interrupt command.

/interrupt is an explicit one-shot soft interrupt command. It exists so a
user can keep the default busy behavior as queue/steer, but still force this
one message to interrupt the active run.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(),
        message_id="m1",
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._busy_input_mode = "queue"
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner, adapter


@pytest.mark.asyncio
async def test_interrupt_command_bypasses_queue_busy_mode_and_interrupts_agent():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())

    running_agent = MagicMock()
    runner._running_agents[sk] = running_agent

    result = await runner._handle_message(_make_event("/interrupt stop that; do this now"))

    assert result is not None
    assert "interrupt" in str(result).lower() or "interrupted" in str(result).lower()
    running_agent.interrupt.assert_called_once_with("stop that; do this now")
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}
    assert runner._queued_events == {}


@pytest.mark.asyncio
async def test_interrupt_preview_collapses_newlines():
    runner, _adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())

    running_agent = MagicMock()
    runner._running_agents[sk] = running_agent

    result = await runner._handle_message(_make_event("/interrupt first line\nsecond line"))

    assert "first line second line" in str(result)
    assert "\n" not in str(result)
    running_agent.interrupt.assert_called_once_with("first line\nsecond line")


@pytest.mark.asyncio
async def test_interrupt_without_payload_returns_usage_and_does_not_interrupt():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())

    running_agent = MagicMock()
    runner._running_agents[sk] = running_agent

    result = await runner._handle_message(_make_event("/interrupt"))

    assert result is not None
    assert "Usage" in str(result) or "usage" in str(result)
    running_agent.interrupt.assert_not_called()
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_interrupt_with_pending_sentinel_reports_starting():
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())
    runner._running_agents[sk] = _AGENT_PENDING_SENTINEL

    result = await runner._handle_message(_make_event("/interrupt take over"))

    assert result is not None
    assert "starting" in str(result).lower()
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_interrupt_failure_does_not_echo_exception_text():
    runner, adapter = _make_runner(_session_entry())
    sk = build_session_key(_make_source())

    running_agent = MagicMock()
    running_agent.interrupt.side_effect = RuntimeError("internal secret detail")
    runner._running_agents[sk] = running_agent

    result = await runner._handle_message(_make_event("/interrupt take over"))

    assert result is not None
    assert "failed" in str(result).lower()
    assert "internal secret detail" not in str(result)
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_interrupt_without_active_agent_reports_no_active_run():
    runner, adapter = _make_runner(_session_entry())

    result = await runner._handle_message(_make_event("/interrupt stop that"))

    assert result is not None
    assert "no active" in str(result).lower() or "not running" in str(result).lower()
    assert runner._pending_messages == {}
    assert adapter._pending_messages == {}
