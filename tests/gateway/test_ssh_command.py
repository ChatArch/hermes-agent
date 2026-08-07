"""Tests for gateway /ssh backend list/status/test/use/on/off behavior."""

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
    assert cmd.args_hint == "[list|status|test <backend>|use <backend>|on <backend>|off <backend>]"
    assert set(cmd.subcommands) >= {"list", "status", "test", "use", "on", "off", "help"}
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

    assert "SSH status" in result
    assert "current backend: `local`" in result
    assert "backend type: local" in result
    assert "auto-switch" in result
    assert "`local`: on" in result
    assert build_session_key(_source(thread_id="omt_thread")) in result


@pytest.mark.asyncio
async def test_ssh_list_renders_local_and_targets_without_starting_agent(monkeypatch):
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

    assert "SSH backends" in result
    assert "`local`" in result
    assert "rex.oray" in result
    assert "rexwzh" in result
    assert "auto:off" in result
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
    assert "Backend switched" in result
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
    assert overrides["ssh_alias"] == "rex.oray"
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
    assert "Backend switched" in result
    assert binding is not None
    assert binding.alias == "demo.remote"
    runner.adapters[Platform.FEISHU].create_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_ssh_help_shows_flat_backend_interface():
    runner = _runner()
    event = _event("/ssh help", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "/ssh use <backend>" in result
    assert "/ssh on <backend>" in result
    assert "/ssh off <backend>" in result
    assert "/ssh local" not in result
    assert "/ssh yolo" not in result


@pytest.mark.asyncio
async def test_ssh_use_local_clears_current_thread_binding_without_changing_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import get_backend_auto_policy, set_backend_auto_enabled, set_ssh_binding, get_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    set_backend_auto_enabled(section_key, "local", False)
    runner = _runner()
    event = _event("/ssh use local", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Backend switched" in result
    assert "`local`" in result
    assert get_ssh_binding(section_key) is None
    assert get_backend_auto_policy(section_key, "local").enabled is False


@pytest.mark.asyncio
async def test_ssh_off_local_blocks_future_model_return_but_keeps_current_binding(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import get_backend_auto_policy, get_ssh_binding, set_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    runner = _runner()
    event = _event("/ssh off local", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Backend `local` auto-switch disabled" in result
    assert "require approval" in result
    assert get_backend_auto_policy(section_key, "local").enabled is False
    assert get_ssh_binding(section_key).alias == "demo.remote"


@pytest.mark.asyncio
async def test_ssh_off_can_disable_all_future_auto_switches_while_current_backend_remains(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import get_backend_auto_policy, get_ssh_binding, set_ssh_binding
    from gateway.ssh_targets import SshTarget

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_run,
        "load_ssh_targets",
        lambda: [SshTarget(alias="demo.remote", host="example.invalid", user="hermes")],
        raising=False,
    )
    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    runner = _runner()

    local_result = await runner._handle_ssh_command(_event("/ssh off local", thread_id="omt_thread"))
    remote_result = await runner._handle_ssh_command(_event("/ssh off demo.remote", thread_id="omt_thread"))

    assert "auto-switch disabled" in local_result
    assert "auto-switch disabled" in remote_result
    assert get_backend_auto_policy(section_key, "local").enabled is False
    assert get_backend_auto_policy(section_key, "demo.remote").enabled is False
    assert get_ssh_binding(section_key).alias == "demo.remote"


@pytest.mark.asyncio
async def test_ssh_on_reenables_model_switch_to_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.ssh_bindings import get_backend_auto_policy, set_backend_auto_enabled, set_ssh_binding

    section_key = build_session_key(_source(thread_id="omt_thread"))
    set_ssh_binding(section_key, alias="demo.remote", cwd="/srv/app")
    set_backend_auto_enabled(section_key, "local", False)
    runner = _runner()
    event = _event("/ssh on local", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Backend `local` auto-switch enabled" in result
    assert get_backend_auto_policy(section_key, "local").enabled is True


@pytest.mark.asyncio
async def test_ssh_status_reports_current_thread_binding_and_auto_switch(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    from gateway.ssh_bindings import set_backend_auto_enabled, set_ssh_binding
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
    set_backend_auto_enabled(section_key, "local", False)
    runner = _runner()
    event = _event("/ssh status", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "current backend: `rex.oray`" in result
    assert "backend type: ssh" in result
    assert "`local`: off" in result
    assert "`rex.oray`: off" in result
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
async def test_ssh_test_unknown_backend_does_not_change_binding(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "load_ssh_targets", lambda: [], raising=False)

    runner = _runner()
    event = _event("/ssh test missing-host")

    result = await runner._handle_ssh_command(event)

    assert "Unknown backend" in result
    assert "missing-host" in result


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
    assert "Backend switched" in result
    assert binding is not None
    assert binding.alias == "rex.oray"


@pytest.mark.asyncio
async def test_legacy_ssh_approval_alias_is_not_user_visible():
    runner = _runner()
    event = _event("/ssh yolo on rex.oray", thread_id="omt_thread")

    result = await runner._handle_ssh_command(event)

    assert "Usage:" in result
    assert "/ssh on <backend>" in result
    assert "/ssh yolo" not in result
