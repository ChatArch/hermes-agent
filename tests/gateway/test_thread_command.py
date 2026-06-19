"""Tests for Feishu /thread command routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource, build_session_key


def _source(*, thread_id=None):
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_name="Feishu Chat",
        chat_type="group",
        user_id="ou_user",
        user_name="tester",
        thread_id=thread_id,
    )


def _event(text="/thread summarize", *, thread_id=None, message_id="om_cmd"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(thread_id=thread_id),
        message_id=message_id,
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache_lock = None
    runner._session_db = None
    runner._format_session_info = lambda: ""
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]))
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.side_effect = lambda source: build_session_key(source)
    runner.session_store._entries = {}
    runner.session_store.reset_session.return_value = MagicMock(session_id="new-session")
    runner._dispatch_event_to_agent = AsyncMock(return_value="assistant answer")
    runner._interrupt_and_clear_session = AsyncMock()
    runner._set_session_reasoning_override = MagicMock()
    runner._clear_session_boundary_security_state = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_thread_command_in_normal_feishu_chat_creates_thread_and_runs_prompt():
    runner = _runner()
    adapter = MagicMock()
    adapter.create_thread = AsyncMock(
        return_value=SendResult(success=True, message_id="om_seed", thread_id="omt_new")
    )
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="om_seed"))
    adapter.release_retargeted_session_guard = MagicMock(return_value=True)
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/thread summarize this")

    result = await runner._handle_thread_command(event)

    assert result is None
    adapter.create_thread.assert_awaited_once_with("oc_chat", "⏳", reply_to="om_cmd")
    adapter.release_retargeted_session_guard.assert_called_once_with(build_session_key(_source()))
    adapter.edit_message.assert_awaited_once_with(
        "oc_chat",
        "om_seed",
        "assistant answer",
        finalize=True,
    )
    assert event.text == "summarize this"
    assert event.message_type == MessageType.TEXT
    assert event.source.thread_id == "omt_new"
    assert event.source.parent_chat_id == "oc_chat"
    assert event.message_id == "om_cmd"
    assert event.reply_to_message_id == "om_seed"
    runner._dispatch_event_to_agent.assert_awaited_once()
    dispatched_event, dispatched_source, dispatched_key = runner._dispatch_event_to_agent.await_args.args
    assert dispatched_event is event
    assert dispatched_source.thread_id == "omt_new"
    assert dispatched_key == build_session_key(dispatched_source)


@pytest.mark.asyncio
async def test_thread_command_inside_existing_feishu_thread_resets_and_runs_prompt():
    runner = _runner()
    event = _event("/thread restart with new plan", thread_id="omt_existing", message_id="om_restart")

    result = await runner._handle_thread_command(event)

    assert result == "assistant answer"
    runner.session_store.reset_session.assert_called_once_with(build_session_key(_source(thread_id="omt_existing")))
    assert event.text == "restart with new plan"
    assert event.source.thread_id == "omt_existing"
    assert event.reply_to_message_id == "om_restart"
    runner._dispatch_event_to_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_thread_command_inside_running_thread_interrupts_before_reset():
    runner = _runner()
    event = _event("/thread restart", thread_id="omt_existing")
    thread_key = build_session_key(_source(thread_id="omt_existing"))
    runner._running_agents[thread_key] = object()

    await runner._handle_thread_command(event)

    runner._interrupt_and_clear_session.assert_awaited_once()
    assert runner._interrupt_and_clear_session.await_args.args[:2] == (thread_key, event.source)


@pytest.mark.asyncio
async def test_thread_command_requires_prompt_and_feishu_platform():
    runner = _runner()
    assert await runner._handle_thread_command(_event("/thread")) == "Usage: /thread <prompt>"

    non_feishu = MessageEvent(
        text="/thread hi",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm"),
        message_id="m1",
    )
    assert "only on Feishu" in await runner._handle_thread_command(non_feishu)
