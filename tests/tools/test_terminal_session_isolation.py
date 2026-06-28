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
    try:
        from tools.file_tools import clear_file_ops_cache

        clear_file_ops_cache()
    except Exception:
        pass


def teardown_function():
    terminal_tool._task_env_overrides.clear()
    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()
    try:
        from tools.file_tools import clear_file_ops_cache

        clear_file_ops_cache()
    except Exception:
        pass


def _expected_session_key(raw: str) -> str:
    return terminal_tool._session_environment_key_from_raw(raw)


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

    assert _resolve_in_session("feishu:chat-a:thread-1") == _expected_session_key("feishu:chat-a:thread-1")


def test_terminal_env_key_sanitizes_session_key_for_backend_resource_names():
    key = terminal_tool._session_environment_key_from_raw("agent:main:platform:chat/thread with spaces")

    assert key.startswith("session-agent-main-platform-chat-thread-")
    assert "/" not in key
    assert ":" not in key
    assert " " not in key
    assert "_" not in key
    assert len(key) <= 57
    assert len(f"hermes-{key}") <= 64


def test_terminal_env_key_truncates_slug_before_docker_label_limit():
    raw = "agent:main:feishu:dm:" + "x" * 200
    key = terminal_tool._session_environment_key_from_raw(raw)

    assert len(key) <= 57
    assert key.startswith("session-agent-main-feishu-dm-xxxxxxxx")
    assert key.endswith(terminal_tool.hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


def test_terminal_env_key_uses_process_session_key_when_context_is_unset(monkeypatch):
    """CLI/legacy entrypoints that expose HERMES_SESSION_KEY should isolate too."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "cli-session-1")

    assert contextvars.Context().run(terminal_tool._resolve_container_task_id, None) == _expected_session_key("cli-session-1")


def test_terminal_env_key_differs_between_gateway_sessions(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    key_a = _resolve_in_session("feishu:chat-a:thread-1")
    key_b = _resolve_in_session("feishu:chat-b:thread-2")

    assert key_a == _expected_session_key("feishu:chat-a:thread-1")
    assert key_b == _expected_session_key("feishu:chat-b:thread-2")
    assert key_a != key_b


def test_feishu_thread_session_key_controls_terminal_env_boundary():
    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key

    def source(thread_id: str, user_id: str = "ou_user") -> SessionSource:
        return SessionSource(
            platform=Platform.FEISHU,
            chat_type="group",
            chat_id="oc_chat",
            user_id=user_id,
            thread_id=thread_id,
        )

    thread_a_user_1 = build_session_key(source("omt_thread_a", "ou_a"))
    thread_a_user_2 = build_session_key(source("omt_thread_a", "ou_b"))
    thread_b = build_session_key(source("omt_thread_b", "ou_a"))

    # Feishu thread sessions are shared by participants inside one thread, but
    # different threads get different session-derived terminal env keys.
    assert thread_a_user_1 == thread_a_user_2
    assert _expected_session_key(thread_a_user_1) != _expected_session_key(thread_b)


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
    assert first == second == _expected_session_key("feishu:chat-a:thread-1")


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

    assert contextvars.Context().run(terminal_tool._resolve_container_task_id, "task-cwd") == _expected_session_key("session-for-cwd")


def test_session_cwd_override_is_visible_to_transient_tool_task_in_context():
    """TUI/Desktop registers cwd under session_key; tools may pass turn-local task ids."""
    session_key = "agent:main:local:dm:tui-session"
    terminal_tool.register_task_env_overrides(session_key, {"cwd": "/workspace/project"})

    def _run():
        tokens = set_session_vars(session_key=session_key)
        try:
            return terminal_tool.resolve_task_overrides("transient-tool-task")
        finally:
            clear_session_vars(tokens)

    assert contextvars.Context().run(_run) == {"cwd": "/workspace/project"}


def test_ssh_backend_override_takes_precedence_over_session_env_key(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    task_id = "internal-transcript-session-id"
    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "ssh_host": "example.invalid",
            "ssh_user": "hermes",
            "ssh_port": 22,
        },
    )

    def _run():
        tokens = set_session_vars(session_key="agent:main:feishu:group:oc_chat:omt_thread")
        try:
            return terminal_tool._resolve_container_task_id(task_id), terminal_tool.resolve_task_overrides(task_id)
        finally:
            clear_session_vars(tokens)

    resolved, overrides = contextvars.Context().run(_run)
    assert resolved == task_id
    assert overrides["env_type"] == "ssh"
    assert overrides["ssh_host"] == "example.invalid"


class FakeEnv:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.cwd = "/fake"
        self.commands: list[str] = []

    def execute(self, command: str, **_kwargs):
        self.commands.append(command)
        return {"output": self.task_id, "returncode": 0}


def test_cwd_override_updates_existing_session_env_when_registered_outside_context():
    """TUI/Desktop can register a session cwd by raw session id outside ContextVars."""
    session_key = "agent:main:local:dm:tui-session"
    env_key = _expected_session_key(session_key)
    env = FakeEnv(env_key)
    env.cwd = "/old"

    with terminal_tool._env_lock:
        terminal_tool._active_environments[env_key] = env

    terminal_tool.register_task_env_overrides(session_key, {"cwd": "/new"})

    assert env.cwd == "/new"


def test_evict_task_environment_removes_session_key_when_called_without_context():
    session_key = "agent:main:local:dm:tui-session"
    env_key = _expected_session_key(session_key)

    with terminal_tool._env_lock:
        terminal_tool._active_environments[env_key] = FakeEnv(env_key)
        terminal_tool._last_activity[env_key] = 1.0

    terminal_tool.evict_task_environment(session_key)

    assert env_key not in terminal_tool._active_environments
    assert env_key not in terminal_tool._last_activity


def test_image_override_change_evicts_existing_hard_isolated_env():
    task_id = "benchmark-task"
    terminal_tool.register_task_env_overrides(task_id, {"docker_image": "old/image:latest"})
    with terminal_tool._env_lock:
        terminal_tool._active_environments[task_id] = FakeEnv(task_id)
        terminal_tool._last_activity[task_id] = 1.0

    terminal_tool.register_task_env_overrides(task_id, {"docker_image": "new/image:latest"})

    assert task_id not in terminal_tool._active_environments
    assert task_id not in terminal_tool._last_activity


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
    key_a = _expected_session_key("feishu:chat-a:thread-1")
    key_b = _expected_session_key("feishu:chat-b:thread-2")

    assert result_a["output"] == key_a
    assert result_b["output"] == key_b
    assert created == [key_a, key_b]
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
    key = _expected_session_key("feishu:chat-a:thread-1")

    assert first["output"] == second["output"] == key
    assert created == [key]
    env = terminal_tool._active_environments[key]
    assert env.commands == ["printf first", "printf second"]


def test_compaction_session_id_rotation_keeps_same_gateway_session_env(monkeypatch):
    """Compression can rotate internal transcript ids without changing thread env."""
    created: list[str] = []

    def fake_create_environment(**kwargs):
        created.append(kwargs["task_id"])
        return FakeEnv(kwargs["task_id"])

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    session_key = "agent:main:feishu:group:oc_chat:omt_thread"

    def run_turn(internal_session_id: str):
        def _run():
            tokens = set_session_vars(session_key=session_key, session_id=internal_session_id)
            try:
                return json.loads(terminal_tool.terminal_tool("printf ok", task_id=internal_session_id))
            finally:
                clear_session_vars(tokens)

        return contextvars.Context().run(_run)

    first = run_turn("session-before-compress")
    second = run_turn("session-after-compress")
    key = _expected_session_key(session_key)

    assert first["output"] == second["output"] == key
    assert created == [key]


def test_terminal_file_and_code_execution_share_session_environment(monkeypatch):
    from tools import code_execution_tool as code_exec
    from tools import file_tools

    created: list[str] = []

    def fake_create_environment(**kwargs):
        created.append(kwargs["task_id"])
        return FakeEnv(kwargs["task_id"])

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    session_key = "agent:main:feishu:group:oc_chat:omt_thread"

    def _run():
        tokens = set_session_vars(session_key=session_key)
        try:
            terminal_result = json.loads(terminal_tool.terminal_tool("printf terminal", task_id="terminal-turn"))
            file_ops = file_tools._get_file_ops("file-turn")
            code_env, code_env_type = code_exec._get_or_create_env("code-turn")
            return terminal_result, file_ops.env, code_env, code_env_type
        finally:
            clear_session_vars(tokens)

    terminal_result, file_env, code_env, code_env_type = contextvars.Context().run(_run)
    key = _expected_session_key(session_key)

    assert terminal_result["output"] == key
    assert file_env is code_env is terminal_tool._active_environments[key]
    assert code_env_type == "local"
    assert created == [key]


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
    key_a = _expected_session_key("feishu:chat-a:thread-1")
    key_b = _expected_session_key("feishu:chat-b:thread-2")

    assert type_a == type_b == "local"
    assert isinstance(env_a, FakeEnv)
    assert isinstance(env_b, FakeEnv)
    assert env_a.task_id == key_a
    assert env_b.task_id == key_b
    assert created == [key_a, key_b]
