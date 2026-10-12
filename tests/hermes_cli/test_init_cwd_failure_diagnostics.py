"""The /init cwd fallback must retain diagnostics without exposing payloads."""

import logging

from hermes_cli import init_command

_PRIVATE_CANARY = "private-init-cwd-payload-marker"


def _assert_boundary_warning(caplog):
    records = [r for r in caplog.records if r.name == "hermes_cli.init_command"
               and r.levelno >= logging.WARNING]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info and record.exc_info[2] is not None
    rendered = logging.Formatter("%(message)s").format(record)
    assert _PRIVATE_CANARY not in rendered
    assert "Traceback" in rendered and "_resolve_session_cwd" in rendered
    assert record.exc_info[1].__cause__ is None and record.exc_info[1].__context__ is None


def test_session_cwd_failure_uses_runtime_cwd_with_private_diagnostic(monkeypatch, tmp_path, caplog):
    from tools import terminal_tool
    from agent import runtime_cwd

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _key: (_ for _ in ()).throw(
        RuntimeError(_PRIVATE_CANARY)))
    monkeypatch.setattr(runtime_cwd, "resolve_agent_cwd", lambda: str(workspace))
    with caplog.at_level(logging.WARNING, logger="hermes_cli.init_command"):
        prompt = init_command.build_init_prompt_for_cwd(session_key="desktop:123")
    assert f"for the project at: {workspace}" in prompt
    _assert_boundary_warning(caplog)


def test_runtime_cwd_failure_uses_process_cwd_with_private_diagnostic(monkeypatch, tmp_path, caplog):
    from tools import terminal_tool
    from agent import runtime_cwd

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _key: None)
    monkeypatch.setattr(runtime_cwd, "resolve_agent_cwd", lambda: (_ for _ in ()).throw(
        RuntimeError(_PRIVATE_CANARY)))
    with caplog.at_level(logging.WARNING, logger="hermes_cli.init_command"):
        prompt = init_command.build_init_prompt_for_cwd(session_key="desktop:123")
    assert f"for the project at: {tmp_path}" in prompt
    _assert_boundary_warning(caplog)


def test_unavailable_diagnostics_keep_original_fallback(monkeypatch, tmp_path):
    from tools import terminal_tool
    from agent import runtime_cwd

    monkeypatch.setattr(init_command, "private_exc_info", lambda _exc: None, raising=False)
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _key: (_ for _ in ()).throw(
        RuntimeError(_PRIVATE_CANARY)))
    monkeypatch.setattr(runtime_cwd, "resolve_agent_cwd", lambda: str(tmp_path))
    assert f"for the project at: {tmp_path}" in init_command.build_init_prompt_for_cwd(session_key="desktop:123")
