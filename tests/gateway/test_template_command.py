"""Tests for Feishu /template thread launcher."""

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


def _event(text="/template prd draft a PRD", *, thread_id=None, message_id="om_cmd"):
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


def _adapter():
    adapter = MagicMock()
    adapter.create_thread = AsyncMock(
        return_value=SendResult(success=True, message_id="om_seed", thread_id="omt_new")
    )
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="om_seed"))
    adapter.release_retargeted_session_guard = MagicMock(return_value=True)
    return adapter


def test_template_command_registered_for_gateway():
    from hermes_cli.commands import ACTIVE_SESSION_BYPASS_COMMANDS, resolve_command

    cmd = resolve_command("template")
    alias = resolve_command("tpl")

    assert cmd is not None
    assert cmd.name == "template"
    assert alias is cmd
    assert cmd.gateway_only is True
    assert cmd.args_hint == "<name|list|create|update|use> [instruction...]"
    assert set(cmd.subcommands) >= {"list", "create", "update", "use"}
    assert "template" in ACTIVE_SESSION_BYPASS_COMMANDS
    assert "tpl" in ACTIVE_SESSION_BYPASS_COMMANDS


@pytest.mark.asyncio
async def test_template_command_lists_available_templates_without_starting_thread(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    templates_dir = tmp_path / "templates"
    (templates_dir / "prd").mkdir(parents=True)
    (templates_dir / "prd" / "SKILL.md").write_text(
        "---\nname: prd\ndescription: Write PRDs\n---\nBody", encoding="utf-8"
    )
    (templates_dir / "bugfix").mkdir(parents=True)
    (templates_dir / "bugfix" / "SKILL.md").write_text(
        "---\nname: bugfix\ndescription: Debug and fix bugs\n---\nBody", encoding="utf-8"
    )
    (templates_dir / "Bad_Name").mkdir(parents=True)
    (templates_dir / "Bad_Name" / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: invalid\n---\nBody", encoding="utf-8"
    )

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/tpl list")

    result = await runner._handle_template_command(event)

    assert "Available templates" in result
    assert "`bugfix` — Debug and fix bugs" in result
    assert "`prd` — Write PRDs" in result
    assert "Bad_Name" not in result
    assert "/template <name>" in result
    adapter.create_thread.assert_not_called()
    runner._dispatch_event_to_agent.assert_not_called()


@pytest.mark.asyncio
async def test_template_command_list_handles_empty_template_store(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template list")

    result = await runner._handle_template_command(event)

    assert "No templates found" in result
    assert "/template create <name>" in result
    adapter.create_thread.assert_not_called()
    runner._dispatch_event_to_agent.assert_not_called()


@pytest.mark.asyncio
async def test_template_command_uses_skill_shaped_template_from_dedicated_store(monkeypatch, tmp_path):
    template_dir = tmp_path / "templates"
    prd_dir = template_dir / "prd"
    prd_dir.mkdir(parents=True)
    (prd_dir / "SKILL.md").write_text(
        """---
name: prd
description: Write mobile-readable PRDs.
---

# PRD Template

Write a concise PRD with goals, constraints, solution, and acceptance criteria.
""",
        encoding="utf-8",
    )

    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template prd draft a PRD")

    result = await runner._handle_template_command(event)

    assert result == "assistant answer"
    adapter.create_thread.assert_awaited_once_with("oc_chat", "⏳", reply_to="om_cmd")
    adapter.release_retargeted_session_guard.assert_called_once_with(build_session_key(_source()))
    adapter.edit_message.assert_awaited_once_with(
        "oc_chat",
        "om_seed",
        "撤回",
        finalize=True,
    )
    assert event.source.thread_id == "omt_new"
    assert event.source.parent_chat_id == "oc_chat"
    assert event.reply_to_message_id == "om_seed"
    assert "invoked the \"prd\" template" in event.text
    assert "Write mobile-readable PRDs" in event.text
    assert "User instruction: draft a PRD" in event.text
    assert "thread-isolated /template use invocation" in event.text
    dispatched_event, dispatched_source, dispatched_key = runner._dispatch_event_to_agent.await_args.args
    assert dispatched_event is event
    assert dispatched_source.thread_id == "omt_new"
    assert dispatched_key == build_session_key(dispatched_source)


@pytest.mark.asyncio
async def test_template_command_create_starts_thread_with_template_authoring_prompt(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template create prd 按工作区规范写 PRD")

    result = await runner._handle_template_command(event)

    assert result == "assistant answer"
    assert event.source.thread_id == "omt_new"
    assert "Create a new Hermes template" in event.text
    assert "User requested template name: prd" in event.text
    assert "SKILL.md format" in event.text
    assert str(tmp_path / "templates" / "prd" / "SKILL.md") in event.text
    assert "按工作区规范写 PRD" in event.text
    runner._dispatch_event_to_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_template_command_create_inside_existing_thread_does_not_create_nested_thread(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event(
        "/template create prd 把当前讨论沉淀成 PRD 模板",
        thread_id="omt_existing",
    )
    original_source = event.source
    expected_key = build_session_key(original_source)

    result = await runner._handle_template_command(event)

    assert result == "assistant answer"
    adapter.create_thread.assert_not_called()
    adapter.release_retargeted_session_guard.assert_not_called()
    adapter.edit_message.assert_not_called()
    assert event.source.thread_id == "omt_existing"
    assert event.source.parent_chat_id is None
    assert event.reply_to_message_id == "om_cmd"
    assert "Create a new Hermes template" in event.text
    assert "User requested template name: prd" in event.text
    assert "把当前讨论沉淀成 PRD 模板" in event.text
    dispatched_event, dispatched_source, dispatched_key = runner._dispatch_event_to_agent.await_args.args
    assert dispatched_event is event
    assert dispatched_source.thread_id == "omt_existing"
    assert dispatched_key == expected_key


@pytest.mark.asyncio
async def test_template_command_update_starts_thread_with_template_update_prompt(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template update prd 加上移动端 Feishu 文档规范")

    result = await runner._handle_template_command(event)

    assert result == "assistant answer"
    assert event.source.thread_id == "omt_new"
    assert "Update an existing Hermes template" in event.text
    assert "User requested template name: prd" in event.text
    assert "加上移动端 Feishu 文档规范" in event.text
    runner._dispatch_event_to_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_template_command_unknown_use_target_returns_guidance(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    templates_dir = tmp_path / "templates"
    (templates_dir / "prd").mkdir(parents=True)
    (templates_dir / "prd" / "SKILL.md").write_text("---\nname: prd\ndescription: PRD\n---\nBody", encoding="utf-8")

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template missing do something")

    result = await runner._handle_template_command(event)

    assert "Unknown template `missing`" in result
    assert "/template create missing" in result
    assert "prd" in result
    adapter.create_thread.assert_not_called()
    runner._dispatch_event_to_agent.assert_not_called()


@pytest.mark.asyncio
async def test_template_command_rejects_invalid_template_name_before_prompt_or_thread(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    runner = _runner()
    adapter = _adapter()
    runner.adapters = {Platform.FEISHU: adapter}
    event = _event("/template create ../skills/pwn do not escape")

    result = await runner._handle_template_command(event)

    assert "Invalid template name `../skills/pwn`" in result
    assert "lowercase letters, numbers, and hyphens" in result
    adapter.create_thread.assert_not_called()
    runner._dispatch_event_to_agent.assert_not_called()


@pytest.mark.asyncio
async def test_template_command_rejects_when_current_thread_agent_is_running(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)

    prd_dir = tmp_path / "templates" / "prd"
    prd_dir.mkdir(parents=True)
    (prd_dir / "SKILL.md").write_text("---\nname: prd\ndescription: PRD\n---\nBody", encoding="utf-8")

    runner = _runner()
    event = _event("/template prd do it", thread_id="omt_existing")
    thread_key = build_session_key(_source(thread_id="omt_existing"))
    runner._running_agents[thread_key] = object()

    result = await runner._handle_template_command(event)

    assert "Agent is running in this thread" in result
    runner._dispatch_event_to_agent.assert_not_called()


@pytest.mark.asyncio
async def test_template_command_requires_feishu_and_arguments():
    runner = _runner()

    assert await runner._handle_template_command(_event("/template")) == "Usage: /template <name|list|create|update|use> [instruction...]"

    non_feishu = MessageEvent(
        text="/template prd hi",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm"),
        message_id="m1",
    )
    assert "only on Feishu" in await runner._handle_template_command(non_feishu)
