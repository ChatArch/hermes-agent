"""Tests for section-scoped SSH runtime overrides."""


def test_task_env_override_selects_ssh_backend(monkeypatch):
    from tools import terminal_tool as tt

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    overrides = {
        "env_type": "ssh",
        "ssh_host": "example.internal",
        "ssh_user": "rex",
        "ssh_port": 2222,
        "ssh_key": "/redacted/key",
        "ssh_identities_only": True,
        "ssh_known_hosts": "/redacted/known_hosts",
        "ssh_host_key_policy": "strict",
        "cwd": "/home/rex/work",
    }

    config = tt.apply_task_env_overrides(tt._get_env_config(), overrides)

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.internal"
    assert config["ssh_user"] == "rex"
    assert config["ssh_port"] == 2222
    assert config["ssh_key"] == "/redacted/key"
    assert config["ssh_identities_only"] is True
    assert config["ssh_known_hosts"] == "/redacted/known_hosts"
    assert config["ssh_host_key_policy"] == "strict"
    assert config["cwd"] == "/home/rex/work"


def test_register_identical_ssh_override_does_not_evict_live_environment(monkeypatch):
    from tools import terminal_tool as tt

    task_id = "stable-ssh-session"
    overrides = {
        "env_type": "ssh",
        "ssh_host": "example.internal",
        "ssh_user": "rex",
        "ssh_key": "/redacted/key",
    }

    class FakeEnv:
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    env = FakeEnv()
    tt.register_task_env_overrides(task_id, overrides)
    with tt._env_lock:
        tt._active_environments[task_id] = env
    try:
        tt.register_task_env_overrides(task_id, dict(overrides))
        with tt._env_lock:
            assert tt._active_environments.get(task_id) is env
        assert env.cleaned is False
    finally:
        tt.clear_task_env_overrides(task_id)
        with tt._env_lock:
            tt._active_environments.pop(task_id, None)
            tt._last_activity.pop(task_id, None)


def test_register_changed_ssh_override_evicts_live_environment(monkeypatch):
    from tools import terminal_tool as tt

    task_id = "changed-ssh-session"

    class FakeEnv:
        cleaned = False

        def cleanup(self):
            self.cleaned = True

    env = FakeEnv()
    tt.register_task_env_overrides(
        task_id,
        {"env_type": "ssh", "ssh_host": "one.internal", "ssh_user": "rex"},
    )
    with tt._env_lock:
        tt._active_environments[task_id] = env
    try:
        tt.register_task_env_overrides(
            task_id,
            {"env_type": "ssh", "ssh_host": "two.internal", "ssh_user": "rex"},
        )
        with tt._env_lock:
            assert task_id not in tt._active_environments
        assert env.cleaned is True
    finally:
        tt.clear_task_env_overrides(task_id)
        with tt._env_lock:
            tt._active_environments.pop(task_id, None)
            tt._last_activity.pop(task_id, None)


def test_file_tools_create_ssh_environment_from_task_override(monkeypatch):
    from tools import file_tools
    from tools import terminal_tool as tt

    captured = {}
    monkeypatch.setenv("TERMINAL_ENV", "local")
    tt.register_task_env_overrides(
        "file-session-123",
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
            "ssh_identities_only": True,
            "ssh_known_hosts": "/redacted/known_hosts",
            "ssh_host_key_policy": "strict",
        },
    )

    class FakeEnv:
        pass

    class FakeOps:
        def __init__(self, env):
            self.env = env

    def fake_create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnv()

    monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
    monkeypatch.setattr(file_tools, "ShellFileOperations", FakeOps)
    try:
        ops = file_tools._get_file_ops("file-session-123")
    finally:
        tt.clear_task_env_overrides("file-session-123")
        with tt._env_lock:
            tt._active_environments.pop("file-session-123", None)
            tt._last_activity.pop("file-session-123", None)
        file_tools.clear_file_ops_cache("file-session-123")

    assert isinstance(ops.env, FakeEnv)
    assert captured["env_type"] == "ssh"
    assert captured["ssh_config"]["host"] == "example.internal"
    assert captured["ssh_config"]["user"] == "rex"
    assert captured["ssh_config"]["key"] == "/redacted/key"
    assert captured["ssh_config"]["identities_only"] is True
    assert captured["ssh_config"]["known_hosts"] == "/redacted/known_hosts"
    assert captured["ssh_config"]["host_key_policy"] == "strict"


def test_execute_code_uses_resolve_task_overrides_for_raw_task_id(monkeypatch):
    from tools import code_execution_tool as code_exec
    from tools import terminal_tool as tt

    captured = {}

    monkeypatch.setenv("TERMINAL_ENV", "local")
    tt.register_task_env_overrides(
        "session-123",
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
            "ssh_identities_only": True,
            "ssh_known_hosts": "/redacted/known_hosts",
            "ssh_host_key_policy": "strict",
        },
    )

    class FakeEnv:
        pass

    def fake_create_environment(**kwargs):
        captured.update(kwargs)
        return FakeEnv()

    monkeypatch.setattr(code_exec, "_create_environment", fake_create_environment, raising=False)
    monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
    try:
        env, env_type = code_exec._get_or_create_env("session-123")
    finally:
        tt.clear_task_env_overrides("session-123")
        with tt._env_lock:
            tt._active_environments.pop("session-123", None)
            tt._last_activity.pop("session-123", None)

    assert isinstance(env, FakeEnv)
    assert env_type == "ssh"
    assert captured["env_type"] == "ssh"
    assert captured["ssh_config"]["host"] == "example.internal"
    assert captured["ssh_config"]["user"] == "rex"
    assert captured["ssh_config"]["key"] == "/redacted/key"
    assert captured["ssh_config"]["identities_only"] is True
    assert captured["ssh_config"]["known_hosts"] == "/redacted/known_hosts"
    assert captured["ssh_config"]["host_key_policy"] == "strict"


def test_prompt_builder_uses_task_override_backend(monkeypatch):
    from agent import prompt_builder
    from tools import terminal_tool as tt

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-prompt")
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", lambda backend: "  user: rex\n  cwd: /home/rex")
    tt.register_task_env_overrides(
        "session-prompt",
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
            "ssh_identities_only": True,
            "ssh_known_hosts": "/redacted/known_hosts",
            "ssh_host_key_policy": "strict",
        },
    )
    try:
        hints = prompt_builder.build_environment_hints()
    finally:
        tt.clear_task_env_overrides("session-prompt")

    assert "Terminal backend: ssh" in hints
    assert "all operate inside this ssh environment" in hints
    assert "Host: macOS" not in hints
