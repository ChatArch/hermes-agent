"""Real typed-entrypoint guards for ChatArch-local gateway features.

These tests enter through GatewayRunner._handle_message() instead of calling
internal handlers directly. They guard the custom registry seam: command
metadata can move to chatarch_custom, but real user entrypoints must still reach
the same gateway handlers.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
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


def _event(text: str, *, thread_id=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(thread_id=thread_id),
        message_id="om_cmd",
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda _source: True
    runner._check_slash_access = lambda _source, _command: None
    runner._draining = False
    runner._external_drain_active = False
    runner._busy_input_mode = "interrupt"
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]))
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.side_effect = lambda source: build_session_key(source)
    runner._handle_thread_command = AsyncMock(return_value="thread handled")
    runner._handle_template_command = AsyncMock(return_value="template handled")
    runner._handle_ssh_command = AsyncMock(return_value="ssh handled")
    return runner


def test_chatarch_custom_registry_exports_local_commands():
    from chatarch_custom.gateway.local_features import (
        active_session_bypass_commands,
        local_gateway_handler_name,
    )
    from hermes_cli.commands import resolve_command

    assert resolve_command("t").name == "thread"
    assert resolve_command("tpl").name == "template"
    assert resolve_command("ssh").name == "ssh"
    assert local_gateway_handler_name("thread") == "_handle_thread_command"
    assert local_gateway_handler_name("template") == "_handle_template_command"
    assert local_gateway_handler_name("ssh") == "_handle_ssh_command"
    assert {"t", "thread", "tpl", "template", "ssh"} <= active_session_bypass_commands()


@pytest.mark.asyncio
async def test_thread_alias_t_reaches_cold_path_thread_handler():
    """Regression guard for the exact `/t ...` user entrypoint."""
    runner = _runner()
    event = _event("/t 你好")

    result = await runner._handle_message(event)

    assert result == "thread handled"
    runner._handle_thread_command.assert_awaited_once_with(event)
    runner._handle_template_command.assert_not_awaited()
    runner._handle_ssh_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_template_alias_tpl_reaches_cold_path_template_handler():
    runner = _runner()
    event = _event("/tpl list")

    result = await runner._handle_message(event)

    assert result == "template handled"
    runner._handle_template_command.assert_awaited_once_with(event)
    runner._handle_thread_command.assert_not_awaited()
    runner._handle_ssh_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_ssh_reaches_cold_path_ssh_handler():
    runner = _runner()
    event = _event("/ssh list")

    result = await runner._handle_message(event)

    assert result == "ssh handled"
    runner._handle_ssh_command.assert_awaited_once_with(event)
    runner._handle_thread_command.assert_not_awaited()
    runner._handle_template_command.assert_not_awaited()
