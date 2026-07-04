"""Regression tests for SSH/session backend path routing in file tools.

Section-scoped SSH Mode should behave like the system-level SSH backend for
execution-plane tools: paths passed to the backend must stay backend paths. The
host Python process may keep local bookkeeping keys, but it must not hand a
host-resolved macOS path to a remote SSH shell.
"""

import json
from pathlib import Path

from tools.file_operations import PatchResult, WriteResult


class _RecordingFileOps:
    def __init__(self):
        self.write_calls = []
        self.patch_calls = []

    def write_file(self, path, content):
        self.write_calls.append((path, content))
        return WriteResult(bytes_written=len(content.encode("utf-8")))

    def patch_replace(self, path, old_string, new_string, replace_all=False):
        self.patch_calls.append((path, old_string, new_string, replace_all))
        return PatchResult(success=True, files_modified=[path])


def _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops):
    def fake_resolve(path, task_id="default"):
        if str(path) == requested:
            return Path(host_resolved)
        return Path(path)

    monkeypatch.setattr(file_tools, "_resolve_path_for_task", fake_resolve)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id="default": ops)
    monkeypatch.setattr(file_tools, "_check_file_staleness", lambda *args, **kwargs: None)
    monkeypatch.setattr(file_tools, "_path_resolution_warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(file_tools, "_update_read_timestamp", lambda *args, **kwargs: None)
    monkeypatch.setattr(file_tools.file_state, "check_stale", lambda *args, **kwargs: None)
    monkeypatch.setattr(file_tools.file_state, "note_write", lambda *args, **kwargs: None)


def _register_ssh_task(terminal_tool, task_id):
    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
            "cwd": "/home/rex/work",
        },
    )


def _configure_system_ssh_backend(monkeypatch):
    """Configure the original process/global terminal backend as SSH."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.internal")
    monkeypatch.setenv("TERMINAL_SSH_USER", "rex")
    monkeypatch.setenv("TERMINAL_SSH_KEY", "/redacted/key")
    monkeypatch.setenv("TERMINAL_CWD", "/home/rex/work")


def test_write_file_system_ssh_backend_passes_backend_path_not_host_resolved(monkeypatch):
    """The original system-level terminal SSH backend has the same path contract."""
    from tools import file_tools

    requested = "/home/rex/work/report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _configure_system_ssh_backend(monkeypatch)
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)

    result = json.loads(file_tools.write_file_tool(requested, "hello\n"))

    assert not result.get("error"), result
    assert ops.write_calls == [(requested, "hello\n")]
    assert result["resolved_path"] == requested
    assert result["files_modified"] == [requested]


def test_patch_replace_system_ssh_backend_passes_backend_path_not_host_resolved(monkeypatch):
    """replace-mode patch also follows system-level terminal SSH backend paths."""
    from tools import file_tools

    requested = "/home/rex/work/report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _configure_system_ssh_backend(monkeypatch)
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)

    result = json.loads(
        file_tools.patch_tool(
            mode="replace",
            path=requested,
            old_string="before",
            new_string="after",
        )
    )

    assert not result.get("error"), result
    assert result["success"] is True
    assert ops.patch_calls == [(requested, "before", "after", False)]
    assert result["resolved_path"] == requested
    assert result["files_modified"] == [requested]


def test_write_file_system_ssh_backend_keeps_relative_backend_path(monkeypatch):
    """System-level terminal SSH resolves display lexically but writes relative backend path."""
    from tools import file_tools

    requested = "report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    display_path = "/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _configure_system_ssh_backend(monkeypatch)
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)

    result = json.loads(file_tools.write_file_tool(requested, "hello\n"))

    assert not result.get("error"), result
    assert ops.write_calls == [(requested, "hello\n")]
    assert result["resolved_path"] == display_path
    assert result["files_modified"] == [display_path]


def test_patch_replace_system_ssh_backend_keeps_relative_backend_path(monkeypatch):
    """System-level terminal SSH patch keeps relative backend path for shell I/O."""
    from tools import file_tools

    requested = "report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    display_path = "/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _configure_system_ssh_backend(monkeypatch)
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)

    result = json.loads(
        file_tools.patch_tool(
            mode="replace",
            path=requested,
            old_string="before",
            new_string="after",
        )
    )

    assert not result.get("error"), result
    assert result["success"] is True
    assert ops.patch_calls == [(requested, "before", "after", False)]
    assert result["resolved_path"] == display_path
    assert result["files_modified"] == [display_path]


def test_write_file_ssh_session_passes_backend_path_not_host_resolved(monkeypatch):
    """write_file must not pass local/macOS-resolved paths to an SSH backend."""
    from tools import file_tools
    from tools import terminal_tool

    task_id = "ssh-write-session"
    requested = "/home/rex/work/report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)
    _register_ssh_task(terminal_tool, task_id)

    try:
        result = json.loads(file_tools.write_file_tool(requested, "hello\n", task_id=task_id))
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)

    assert not result.get("error"), result
    assert ops.write_calls == [(requested, "hello\n")]
    assert result["resolved_path"] == requested
    assert result["files_modified"] == [requested]


def test_patch_replace_ssh_session_passes_backend_path_not_host_resolved(monkeypatch):
    """replace-mode patch must edit the backend path under SSH Mode."""
    from tools import file_tools
    from tools import terminal_tool

    task_id = "ssh-patch-session"
    requested = "/home/rex/work/report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)
    _register_ssh_task(terminal_tool, task_id)

    try:
        result = json.loads(
            file_tools.patch_tool(
                mode="replace",
                path=requested,
                old_string="before",
                new_string="after",
                task_id=task_id,
            )
        )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)

    assert not result.get("error"), result
    assert result["success"] is True
    assert ops.patch_calls == [(requested, "before", "after", False)]
    assert result["resolved_path"] == requested
    assert result["files_modified"] == [requested]


def test_write_file_ssh_session_keeps_relative_backend_path(monkeypatch):
    """Section/session SSH Mode resolves display lexically but writes relative backend path."""
    from tools import file_tools
    from tools import terminal_tool

    task_id = "ssh-write-session-relative"
    requested = "report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    display_path = "/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)
    _register_ssh_task(terminal_tool, task_id)

    try:
        result = json.loads(file_tools.write_file_tool(requested, "hello\n", task_id=task_id))
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)

    assert not result.get("error"), result
    assert ops.write_calls == [(requested, "hello\n")]
    assert result["resolved_path"] == display_path
    assert result["files_modified"] == [display_path]


def test_patch_replace_ssh_session_keeps_relative_backend_path(monkeypatch):
    """Section/session SSH Mode patch keeps relative backend path for shell I/O."""
    from tools import file_tools
    from tools import terminal_tool

    task_id = "ssh-patch-session-relative"
    requested = "report.md"
    host_resolved = "/System/Volumes/Data/home/rex/work/report.md"
    display_path = "/home/rex/work/report.md"
    ops = _RecordingFileOps()
    _install_common_stubs(monkeypatch, file_tools, requested, host_resolved, ops)
    _register_ssh_task(terminal_tool, task_id)

    try:
        result = json.loads(
            file_tools.patch_tool(
                mode="replace",
                path=requested,
                old_string="before",
                new_string="after",
                task_id=task_id,
            )
        )
    finally:
        terminal_tool.clear_task_env_overrides(task_id)
        file_tools.clear_file_ops_cache(task_id)

    assert not result.get("error"), result
    assert result["success"] is True
    assert ops.patch_calls == [(requested, "before", "after", False)]
    assert result["resolved_path"] == display_path
    assert result["files_modified"] == [display_path]
