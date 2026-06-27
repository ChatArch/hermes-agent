"""Regression tests for session-scoped terminal environment keys."""

import contextvars
import json
import os

import tools.terminal_tool as terminal_tool
from gateway.session_context import clear_session_vars, set_session_vars


def setup_function():
    terminal_tool._task_env_overrides.clear()
    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()


def teardown_function():
    terminal_tool._task_env_overrides.clear()
    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()


def _resolve_in_session(session_key: str, task_id: str | None = None) -> str:
    """Resolve an environment key inside an isolated ContextVar context."""
    def _run() -> str:
        tokens = set_session_vars(session_key=session_key)
        try:
            return terminal_tool._resolve_container_task_id(task_id)
        finally:
            clear_session_vars(tokens)

    return contextvars.Context().run(_run)


def test_terminal_env_key_uses_gateway_session_key_from_contextvars(monkeypatch):
    """Gateway sessions should not collapse to the shared default env."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    assert _resolve_in_session("feishu:chat-a:thread-1") == "session:feishu:chat-a:thread-1"


def test_terminal_env_key_uses_process_session_key_when_context_is_unset(monkeypatch):
    """CLI/legacy entrypoints that expose HERMES_SESSION_KEY should isolate too."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "cli-session-1")

    assert contextvars.Context().run(terminal_tool._resolve_container_task_id, None) == "session:cli-session-1"


def test_terminal_env_key_differs_between_gateway_sessions(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    key_a = _resolve_in_session("feishu:chat-a:thread-1")
    key_b = _resolve_in_session("feishu:chat-b:thread-2")

    assert key_a == "session:feishu:chat-a:thread-1"
    assert key_b == "session:feishu:chat-b:thread-2"
    assert key_a != key_b


def test_terminal_env_key_reuses_same_gateway_session(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    def _run() -> tuple[str, str]:
        tokens = set_session_vars(session_key="feishu:chat-a:thread-1")
        try:
            return (
                terminal_tool._resolve_container_task_id(None),
                terminal_tool._resolve_container_task_id(None),
            )
        finally:
            clear_session_vars(tokens)

    first, second = contextvars.Context().run(_run)
    assert first == second == "session:feishu:chat-a:thread-1"


def test_terminal_env_key_without_session_stays_default(monkeypatch):
    """Plain CLI/tool tests without a session key keep the historical default."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    assert contextvars.Context().run(terminal_tool._resolve_container_task_id, None) == "default"


def test_backend_image_override_still_takes_precedence(monkeypatch):
    """Benchmark/SSH/docker overrides that requested hard isolation keep task ids."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-should-not-win")
    terminal_tool.register_task_env_overrides("task-123", {"docker_image": "example/image:latest"})

    assert terminal_tool._resolve_container_task_id("task-123") == "task-123"


def test_cwd_only_override_uses_session_key_not_raw_task_id(monkeypatch):
    """ACP/gateway CWD overrides should get session isolation, not task-id isolation."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-for-cwd")
    terminal_tool.register_task_env_overrides("task-cwd", {"cwd": "/workspace/project"})

    assert contextvars.Context().run(terminal_tool._resolve_container_task_id, "task-cwd") == "session:session-for-cwd"


class FakeEnv:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.cwd = "/fake"
        self.commands: list[str] = []

    def execute(self, command: str, **_kwargs):
        self.commands.append(command)
        return {"output": self.task_id, "returncode": 0}


def test_terminal_tool_creates_separate_environments_for_separate_sessions(monkeypatch):
    created: list[str] = []

    def fake_create_environment(**kwargs):
        created.append(kwargs["task_id"])
        return FakeEnv(kwargs["task_id"])

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    def run_in_session(session_key: str):
        def _run():
            tokens = set_session_vars(session_key=session_key)
            try:
                return json.loads(terminal_tool.terminal_tool("printf ok"))
            finally:
                clear_session_vars(tokens)

        return contextvars.Context().run(_run)

    result_a = run_in_session("feishu:chat-a:thread-1")
    result_b = run_in_session("feishu:chat-b:thread-2")

    assert result_a["output"] == "session:feishu:chat-a:thread-1"
    assert result_b["output"] == "session:feishu:chat-b:thread-2"
    assert created == [
        "session:feishu:chat-a:thread-1",
        "session:feishu:chat-b:thread-2",
    ]
    assert set(terminal_tool._active_environments) == set(created)


def test_terminal_tool_reuses_environment_within_same_session(monkeypatch):
    created: list[str] = []

    def fake_create_environment(**kwargs):
        created.append(kwargs["task_id"])
        return FakeEnv(kwargs["task_id"])

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    def _run():
        tokens = set_session_vars(session_key="feishu:chat-a:thread-1")
        try:
            first = json.loads(terminal_tool.terminal_tool("printf first"))
            second = json.loads(terminal_tool.terminal_tool("printf second"))
            return first, second
        finally:
            clear_session_vars(tokens)

    first, second = contextvars.Context().run(_run)

    assert first["output"] == second["output"] == "session:feishu:chat-a:thread-1"
    assert created == ["session:feishu:chat-a:thread-1"]
    env = terminal_tool._active_environments["session:feishu:chat-a:thread-1"]
    assert env.commands == ["printf first", "printf second"]


def test_code_execution_environment_uses_session_key(monkeypatch):
    from tools import code_execution_tool as code_exec

    created: list[str] = []

    def fake_create_environment(**kwargs):
        created.append(kwargs["task_id"])
        return FakeEnv(kwargs["task_id"])

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    def get_env_for_session(session_key: str):
        def _run():
            tokens = set_session_vars(session_key=session_key)
            try:
                return code_exec._get_or_create_env("code-task")
            finally:
                clear_session_vars(tokens)

        return contextvars.Context().run(_run)

    env_a, type_a = get_env_for_session("feishu:chat-a:thread-1")
    env_b, type_b = get_env_for_session("feishu:chat-b:thread-2")

    assert type_a == type_b == "local"
    assert isinstance(env_a, FakeEnv)
    assert isinstance(env_b, FakeEnv)
    assert env_a.task_id == "session:feishu:chat-a:thread-1"
    assert env_b.task_id == "session:feishu:chat-b:thread-2"
    assert created == [
        "session:feishu:chat-a:thread-1",
        "session:feishu:chat-b:thread-2",
    ]
