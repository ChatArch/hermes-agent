"""Tests for gateway /ssh V0 list/status/test behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.cards.actions import CardActionContext, get_card_action_registry
from gateway.platforms.base import CardReply, MessageEvent, MessageType
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


def _event(text="/ssh list", *, thread_id=None):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(thread_id=thread_id),
        message_id="om_cmd",
    )


def _card_text(card):
    parts = []
    for element in getattr(card, "elements", []):
        if hasattr(element, "content"):
            parts.append(element.content)
        if hasattr(element, "text"):
            parts.append(element.text)
        button = getattr(element, "button", None)
        if button is not None:
            parts.append(getattr(button, "text", ""))
        for option in getattr(element, "options", []) or []:
            parts.append(getattr(option, "text", ""))
    return "\n".join(str(part) for part in parts if part)


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._format_session_info = lambda: ""
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]))
    return runner


def test_ssh_command_registered_for_gateway():
    from hermes_cli.commands import ACTIVE_SESSION_BYPASS_COMMANDS, resolve_command

    cmd = resolve_command("ssh")

    assert cmd is not None
    assert cmd.name == "ssh"
    assert cmd.gateway_only is True
    assert cmd.args_hint == "[list|status|test <alias>|use <alias>|off]"
    assert set(cmd.subcommands) >= {"list", "status", "test", "use", "off", "local", "help"}
    assert "ssh" in ACTIVE_SESSION_BYPASS_COMMANDS


def test_load_ssh_targets_reads_hermes_managed_config_not_system_config(tmp_path):
    from gateway.ssh_targets import load_ssh_targets, render_ssh_targets

    hermes_config = tmp_path / "ssh-targets.yaml"
    hermes_config.write_text(
        """
ssh:
  targets:
    rex.oray:
      host: rexwzh.oray
      user: rexwzh
      port: 2222
      identity_file: ~/.hermes/ssh/keys/rex_oray
      identities_only: true
      known_hosts: ~/.hermes/ssh/known_hosts
      host_key_policy: strict
      cwd: /home/rexwzh/Playground
""",
        encoding="utf-8",
    )

    targets = load_ssh_targets(config_path=hermes_config)
    rendered = render_ssh_targets(targets)

    assert [t.alias for t in targets] == ["rex.oray"]
    assert targets[0].source == "hermes"
    assert targets[0].cwd == "/home/rexwzh/Playground"
    assert targets[0].identities_only is True
    assert targets[0].known_hosts == "~/.hermes/ssh/known_hosts"
    assert targets[0].host_key_policy == "strict"
    assert "rex.oray" in rendered
    assert "rexwzh" in rendered
    assert "2222" in rendered
    assert "/home/rexwzh/Playground" in rendered
    assert "rex_oray" not in rendered
    assert "[REDACTED_PATH]" in rendered


def test_parse_system_ssh_config_is_available_only_for_explicit_import():
    from gateway.ssh_targets import parse_system_ssh_config, render_ssh_targets

    config_text = """
Host rex.oray
  HostName rexwzh.oray
  User rexwzh
  Port 2222
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  UserKnownHostsFile ~/.hermes/ssh/known_hosts
  StrictHostKeyChecking yes

Host main.github.com
  HostName github.com
  User git
"""

    targets = parse_system_ssh_config(config_text)
    rendered = render_ssh_targets(targets)

    assert [t.alias for t in targets] == ["rex.oray", "main.github.com"]
    assert targets[0].identities_only is True
    assert targets[0].known_hosts == "~/.hermes/ssh/known_hosts"
    assert targets[0].host_key_policy == "yes"
    assert "rex.oray" in rendered
    assert "rexwzh" in rendered
    assert "2222" in rendered
    assert "id_ed25519" not in rendered
    assert "IdentityFile" not in rendered
    assert "[REDACTED_PATH]" in rendered


@pytest.mark.asyncio
async def test_ssh_status_reports_current_section_without_binding():
    runner = _runner()
    event = _event("/ssh status", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert isinstance(result, CardReply)
    assert result.session_key == build_session_key(_source(thread_id="omt_thread"))
    assert result.card.header.title == "SSH"
    text = _card_text(result.card)
    assert "当前后端：`local`" in text
    assert "当前绑定：`none`" in text
    assert "YOLO" in text


@pytest.mark.asyncio
async def test_ssh_list_renders_targets_without_starting_agent(monkeypatch):
    from gateway.ssh_targets import SshTarget
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [
            SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh", port=22, identity_file="~/.ssh/id_ed25519"),
        ],
        raising=False,
    )

    runner = _runner()
    event = _event("/ssh list")

    result = await runner._handle_ssh_command(event)

    assert isinstance(result, CardReply)
    text = _card_text(result.card)
    assert "SSH" == result.card.header.title
    assert "rex.oray" in text
    assert "rexwzh" in text
    assert "id_ed25519" not in text
    assert "rex.oray" in text


@pytest.mark.asyncio
async def test_ssh_card_actions_bind_and_yolo_target(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import get_ssh_binding, get_ssh_yolo_grant
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [
            SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh", cwd="/home/rexwzh/Playground"),
            SshTarget(alias="hitk", host="hitk.internal", user="zhihong"),
        ],
        raising=False,
    )
    runner = _runner()
    event = _event("/ssh list", thread_id="omt_thread")
    section_key = build_session_key(_source(thread_id="omt_thread"))

    result = await runner._handle_ssh_command(event)
    assert isinstance(result, CardReply)

    yolo_response = await get_card_action_registry().dispatch(
        CardActionContext(
            action="gateway.ssh.action",
            payload={"op": "yolo_set", "values": ["rex.oray", "hitk"]},
            user_id="ou_user",
            chat_id="oc_chat",
            message_id="om_ssh",
            session_key=section_key,
        )
    )
    use_response = await get_card_action_registry().dispatch(
        CardActionContext(
            action="gateway.ssh.action",
            payload={"op": "use", "alias": "rex.oray"},
            user_id="ou_user",
            chat_id="oc_chat",
            message_id="om_ssh",
            session_key=section_key,
        )
    )

    assert yolo_response.kind == "replace_card"
    assert use_response.kind == "replace_card"
    assert list(get_ssh_yolo_grant(section_key).aliases) == ["rex.oray", "hitk"]
    binding = get_ssh_binding(section_key)
    assert binding is not None
    assert binding.alias == "rex.oray"
    assert "当前后端：`ssh`" in _card_text(use_response.card)


@pytest.mark.asyncio
async def test_ssh_use_binds_current_thread(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [
            SshTarget(
                alias="rex.oray",
                host="rexwzh.oray",
                user="rexwzh",
                port=2222,
                identity_file="~/.hermes/ssh/keys/rex_oray",
                identities_only=True,
                known_hosts="~/.hermes/ssh/known_hosts",
                host_key_policy="strict",
                cwd="/home/rexwzh/Playground",
            ),
        ],
        raising=False,
    )
    runner = _runner()
    event = _event("/ssh use rex.oray --cwd /srv/app", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    section_key = build_session_key(_source(thread_id="omt_thread"))
    assert "SSH enabled" in result
    assert "rex.oray" in result
    assert "[REDACTED_PATH]" in result

    from gateway.ssh_bindings import get_ssh_binding, resolve_binding_task_overrides
    target_list = [
        SshTarget(
            alias="rex.oray",
            host="rexwzh.oray",
            user="rexwzh",
            port=2222,
            identity_file="~/.hermes/ssh/keys/rex_oray",
            identities_only=True,
            known_hosts="~/.hermes/ssh/known_hosts",
            host_key_policy="strict",
            cwd="/home/rexwzh/Playground",
        )
    ]

    binding = get_ssh_binding(section_key)
    assert binding is not None
    assert binding.alias == "rex.oray"
    assert binding.cwd == "/srv/app"
    overrides = resolve_binding_task_overrides(section_key, targets=target_list)
    assert overrides["env_type"] == "ssh"
    assert overrides["ssh_host"] == "rexwzh.oray"
    assert overrides["ssh_user"] == "rexwzh"
    assert overrides["ssh_port"] == 2222
    assert overrides["ssh_key"] == "~/.hermes/ssh/keys/rex_oray"
    assert overrides["ssh_identities_only"] is True
    assert overrides["ssh_known_hosts"] == "~/.hermes/ssh/known_hosts"
    assert overrides["ssh_host_key_policy"] == "strict"
    assert overrides["cwd"] == "/srv/app"


@pytest.mark.asyncio
async def test_ssh_use_in_parent_chat_defaults_to_new_thread(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="demo.remote", host="example.invalid", user="hermes")],
        raising=False,
    )
    runner = _runner()
    runner.adapters[Platform.FEISHU] = SimpleNamespace(  # type: ignore[assignment]
        create_thread=AsyncMock(return_value=SimpleNamespace(success=True, thread_id="omt_default", message_id="om_default"))
    )
    event = _event("/ssh use demo.remote", thread_id=None)

    result = await runner._handle_ssh_command(event)

    from gateway.ssh_bindings import get_ssh_binding
    section_key = build_session_key(_source(thread_id="omt_default"))
    binding = get_ssh_binding(section_key)
    assert "SSH enabled" in result
    assert binding is not None
    assert binding.alias == "demo.remote"
    runner.adapters[Platform.FEISHU].create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_ssh_help_prefers_local_and_keeps_off_as_alias():
    runner = _runner()
    event = _event("/ssh help", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert isinstance(result, CardReply)
    assert result.card.header.title == "SSH"
    assert result.fallback_text


@pytest.mark.asyncio
async def test_ssh_local_clears_current_thread_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import set_ssh_binding, get_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    runner = _runner()
    event = _event("/ssh local", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Current backend: local" in result
    assert "SSH binding cleared" in result
    assert get_ssh_binding(section_key) is None


@pytest.mark.asyncio
async def test_ssh_off_is_compatibility_alias_for_local(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import set_ssh_binding, get_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    runner = _runner()
    event = _event("/ssh off", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Current backend: local" in result
    assert get_ssh_binding(section_key) is None


@pytest.mark.asyncio
async def test_ssh_status_reports_current_thread_binding(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import set_ssh_binding
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh", identity_file="/secret/key")],
        raising=False,
    )
    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="rex.oray")
    runner = _runner()
    event = _event("/ssh status", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert isinstance(result, CardReply)
    text = _card_text(result.card)
    assert "当前后端：`ssh`" in text
    assert "rex.oray" in text
    assert "/secret/key" not in text


@pytest.mark.asyncio
async def test_ssh_test_incomplete_target_does_not_pass(monkeypatch):
    import gateway.run as gateway_run
    from gateway.ssh_targets import SshTarget

    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="broken", host=None, user="rex")],
        raising=False,
    )
    runner = _runner()
    event = _event("/ssh test broken")

    result = await runner._handle_ssh_command(event)

    assert "incomplete" in result
    assert "missing host" in result
    assert "No binding was changed" in result


@pytest.mark.asyncio
async def test_ssh_use_incomplete_target_does_not_bind(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import get_ssh_binding
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="broken", host="example.internal", user=None)],
        raising=False,
    )
    runner = _runner()
    event = _event("/ssh use broken", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    section_key = build_session_key(_source(thread_id="omt_thread"))
    assert "incomplete" in result
    assert "missing user" in result
    assert get_ssh_binding(section_key) is None


@pytest.mark.asyncio
async def test_ssh_test_unknown_alias_does_not_change_binding(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "load_ssh_targets", lambda: [], raising=False)

    runner = _runner()
    event = _event("/ssh test missing-host")

    result = await runner._handle_ssh_command(event)

    assert "Unknown SSH target" in result
    assert "missing-host" in result
    assert "No binding was changed" in result


@pytest.mark.asyncio
async def test_ssh_use_explicit_thread_alias_creates_thread_and_binds(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh")],
        raising=False,
    )
    runner = _runner()
    runner.adapters[Platform.FEISHU] = SimpleNamespace(  # type: ignore[assignment]
        create_thread=AsyncMock(return_value=SimpleNamespace(success=True, thread_id="omt_new", message_id="om_new"))
    )
    event = _event("/ssh use rex.oray -t", thread_id=None)

    result = await runner._handle_ssh_command(event)

    from gateway.ssh_bindings import get_ssh_binding
    section_key = build_session_key(_source(thread_id="omt_new"))
    binding = get_ssh_binding(section_key)
    assert "SSH enabled" in result
    assert binding is not None
    assert binding.alias == "rex.oray"


@pytest.mark.asyncio
async def test_ssh_yolo_is_thread_scoped_in_feishu_parent_chat():
    runner = _runner()
    event = _event("/ssh yolo on rex.oray", thread_id=None)

    result = await runner._handle_ssh_command(event)

    assert "Thread-scoped" in result
    assert "/ssh use <alias>" in result


@pytest.mark.asyncio
async def test_ssh_yolo_on_status_and_off(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import get_ssh_yolo_grant
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh")],
        raising=False,
    )
    runner = _runner()

    result = await runner._handle_ssh_command(_event("/ssh yolo on rex.oray", thread_id="omt_thread"))
    status = await runner._handle_ssh_command(_event("/ssh yolo status", thread_id="omt_thread"))
    off = await runner._handle_ssh_command(_event("/ssh yolo off rex.oray", thread_id="omt_thread"))

    section_key = build_session_key(_source(thread_id="omt_thread"))
    grant = get_ssh_yolo_grant(section_key)
    assert "SSH YOLO enabled" in result
    assert "rex.oray" in status
    assert "yolo: off" in off
    assert grant.enabled is False
