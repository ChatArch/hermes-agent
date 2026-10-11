"""Typed user SSH commands share the fail-closed apply boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


@pytest.mark.asyncio
async def test_typed_local_switch_does_not_claim_success_after_projection_failure(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.ssh_bindings import set_ssh_binding
    from tools import terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = SessionSource(platform=Platform.FEISHU, chat_id="test-chat", chat_type="group",
                           user_id="test-user", thread_id="test-thread")
    key = build_session_key(source)
    set_ssh_binding(key, alias="prior")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="test")})
    runner.adapters = {}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]))

    def failed_clear(task_id):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(terminal_tool, "clear_task_env_overrides", failed_clear)
    event = MessageEvent(text="/ssh use local", message_type=MessageType.TEXT,
                         source=source, message_id="test-message")
    result = await runner._handle_ssh_command(event)
    assert "blocked" in result.lower()
    assert "switched" not in result.lower()
