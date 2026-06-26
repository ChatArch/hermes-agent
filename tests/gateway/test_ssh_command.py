"""Tests for gateway /ssh V0 list/status/test behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _event(text="/ssh list", *, thread_id=None):
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
      cwd: /home/rexwzh/Playground
""",
        encoding="utf-8",
    )

    targets = load_ssh_targets(config_path=hermes_config)
    rendered = render_ssh_targets(targets)

    assert [t.alias for t in targets] == ["rex.oray"]
    assert targets[0].source == "hermes"
    assert targets[0].cwd == "/home/rexwzh/Playground"
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

Host main.github.com
  HostName github.com
  User git
"""

    targets = parse_system_ssh_config(config_text)
    rendered = render_ssh_targets(targets)

    assert [t.alias for t in targets] == ["rex.oray", "main.github.com"]
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

    assert "SSH status" in result
    assert "section binding: none" in result
    assert "current backend: local" in result
    assert "/ssh list" in result
    assert build_session_key(_source(thread_id="omt_thread")) in result


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

    assert "SSH targets" in result
    assert "rex.oray" in result
    assert "rexwzh" in result
    assert "id_ed25519" not in result
    assert "[REDACTED_PATH]" in result


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
    assert overrides["cwd"] == "/srv/app"


@pytest.mark.asyncio
async def test_ssh_use_in_parent_chat_requires_new_thread(monkeypatch):
    import gateway.run as gateway_run
    from gateway.ssh_targets import SshTarget

    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="rex.oray", host="rexwzh.oray", user="rexwzh")],
        raising=False,
    )
    runner = _runner()
    event = _event("/ssh use rex.oray", thread_id=None)

    result = await runner._handle_ssh_command(event)

    assert "Feishu Thread" in result
    assert "--new-thread" in result


@pytest.mark.asyncio
async def test_ssh_off_clears_current_thread_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import set_ssh_binding, get_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="rex.oray", cwd="/srv/app")
    runner = _runner()
    event = _event("/ssh off", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "SSH disabled" in result
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

    assert "current backend: ssh" in result
    assert "rex.oray" in result
    assert "/secret/key" not in result
    assert "[REDACTED_PATH]" in result


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
