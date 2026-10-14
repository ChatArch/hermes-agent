"""MCP stderr tail failures remain empty without exposing private paths or errors."""

import logging
import os
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import plugin_host_diagnostics
import hermes_constants
from tools import mcp_tool_config as config

_PRIVATE = "private-mcp-tail-marker"


def _fail(*_args, **_kwargs):
    try:
        raise ValueError(_PRIVATE)
    except ValueError as cause:
        raise RuntimeError(_PRIVATE) from cause


@pytest.mark.parametrize("diagnostics", [True, False])
def test_tail_failure_keeps_empty_fallback_and_private_chain(monkeypatch, caplog, diagnostics):
    monkeypatch.setattr(config, "_get_mcp_stderr_log", _fail)
    if not diagnostics:
        monkeypatch.setattr(plugin_host_diagnostics, "private_exc_info", lambda _error: None)
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        assert config._tail_server_stderr("private-server-name") == ""
    rows = [r for r in caplog.records if r.name == "tools.mcp_tool" and r.levelno >= logging.WARNING]
    if not diagnostics:
        assert rows == []
        return
    assert len(rows) == 1
    rendered = logging.Formatter("%(message)s").format(rows[0])
    assert "MCP stderr tail unavailable" in rendered and "_tail_server_stderr" in rendered
    assert _PRIVATE not in rendered and "private-server-name" not in rendered and "in _fail" not in rendered
    assert rows[0].exc_info[1].__cause__ is None and rows[0].exc_info[1].__context__ is None


def test_missing_file_keeps_empty_fallback_with_fixed_diagnostic(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "_get_mcp_stderr_log", lambda: SimpleNamespace(name=str(tmp_path / "private-missing.log")))
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        assert config._tail_server_stderr("private-server-name") == ""
    rows = [r for r in caplog.records if r.name == "tools.mcp_tool" and r.levelno >= logging.WARNING]
    assert len(rows) == 1
    assert "private-missing.log" not in logging.Formatter("%(message)s").format(rows[0])


def test_healthy_tail_preserves_original_text_and_no_diagnostic(tmp_path, monkeypatch, caplog):
    path = tmp_path / "mcp-stderr.log"
    path.write_text("2026-10-14 starting MCP server 'fixture'\nfirst\nsecond\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_mcp_stderr_log", lambda: SimpleNamespace(name=str(path)))
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        assert config._tail_server_stderr("fixture") == "starting MCP server 'fixture'\nfirst\nsecond"
        assert config._tail_server_stderr("other") == ""
    assert not [r for r in caplog.records if r.name == "tools.mcp_tool" and r.levelno >= logging.WARNING]


@pytest.mark.parametrize("sink", ["file", "formatter"])
def test_bad_standard_diagnostic_sink_does_not_replace_empty_fallback(tmp_path, monkeypatch, capsys, sink):
    monkeypatch.setattr(config, "_get_mcp_stderr_log", _fail)
    handler = logging.FileHandler(tmp_path, delay=True) if sink == "file" else logging.StreamHandler()
    if sink == "formatter":
        handler.setFormatter(logging.Formatter("%(missing_field)s"))
    logger = logging.getLogger("tools.mcp_tool")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    try:
        assert config._tail_server_stderr("private-server-name") == ""
        assert _PRIVATE not in capsys.readouterr().err
    finally:
        handler.close()


def test_unverified_managed_home_refuses_bare_command_instead_of_selecting_unknown_path(tmp_path, monkeypatch, caplog):
    managed_bin = tmp_path / "unverified-home" / "bin"
    user_bin = tmp_path / "user-bin"
    managed_bin.mkdir(parents=True)
    user_bin.mkdir()
    name = "fixture.exe" if sys.platform == "win32" else "fixture"
    for directory in (managed_bin, user_bin):
        exe = directory / name
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", _fail)
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        with pytest.raises(RuntimeError, match="Managed executable directory identity unavailable") as caught:
            config._resolve_stdio_command("fixture", {"PATH": os.pathsep.join([str(managed_bin), str(user_bin)])})
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    rows = [r for r in caplog.records if r.name == "tools.mcp_tool" and r.levelno >= logging.WARNING]
    assert len(rows) == 1
    rendered = logging.Formatter("%(message)s").format(rows[0])
    assert "MCP managed executable identity unavailable" in rendered
    assert _PRIVATE not in rendered and str(tmp_path) not in rendered and "in _fail" not in rendered


def test_verified_managed_home_keeps_user_command_precedence(tmp_path, monkeypatch, caplog):
    managed_bin = tmp_path / "home" / "bin"
    user_bin = tmp_path / "user-bin"
    managed_bin.mkdir(parents=True)
    user_bin.mkdir()
    name = "fixture.exe" if sys.platform == "win32" else "fixture"
    for directory in (managed_bin, user_bin):
        exe = directory / name
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: managed_bin.parent)
    command, env = config._resolve_stdio_command("fixture", {"PATH": os.pathsep.join([str(managed_bin), str(user_bin)])})
    assert command == str(user_bin / name)
    assert env["PATH"] == os.pathsep.join([str(managed_bin), str(user_bin)])
    assert not [r for r in caplog.records if r.name == "tools.mcp_tool" and r.levelno >= logging.WARNING]
