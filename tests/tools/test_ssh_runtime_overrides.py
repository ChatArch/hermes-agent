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


def test_execute_code_top_level_dispatch_uses_task_override_backend(monkeypatch):
    """Top-level execute_code must route by effective backend, not global config only."""
    from tools import code_execution_tool as code_exec
    from tools import terminal_tool as tt

    task_id = "execute-session-ssh"
    calls = []

    monkeypatch.setenv("TERMINAL_ENV", "local")
    tt.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
        },
    )

    def fake_execute_remote(code, remote_task_id, enabled_tools):
        calls.append((code, remote_task_id, enabled_tools))
        return '{"status":"success","remote":true}'

    monkeypatch.setattr(code_exec, "_execute_remote", fake_execute_remote)
    try:
        result = code_exec.execute_code("print('hello')", task_id=task_id, enabled_tools=["terminal"])
    finally:
        tt.clear_task_env_overrides(task_id)

    assert calls == [("print('hello')", task_id, ["terminal"])]
    assert '"remote":true' in result



def test_execute_code_top_level_dispatch_uses_session_context_override(monkeypatch):
    """Gateway session ContextVar fallback must affect top-level execute_code dispatch."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import code_execution_tool as code_exec
    from tools import terminal_tool as tt

    session_key = "agent:main:feishu:dm:chat:thread"
    calls = []

    monkeypatch.setenv("TERMINAL_ENV", "local")
    tt.register_task_env_overrides(
        session_key,
        {
            "env_type": "ssh",
            "ssh_host": "example.internal",
            "ssh_user": "rex",
            "ssh_key": "/redacted/key",
        },
    )

    def fake_execute_remote(code, remote_task_id, enabled_tools):
        calls.append((code, remote_task_id, enabled_tools))
        return '{"status":"success","remote":true}'

    monkeypatch.setattr(code_exec, "_execute_remote", fake_execute_remote)
    monkeypatch.setattr(
        "tools.approval.check_execute_code_guard",
        lambda code, env_type, **kwargs: {"approved": True},
    )
    tokens = set_session_vars(platform="feishu", chat_id="chat", thread_id="thread", session_key=session_key)
    try:
        result = code_exec.execute_code("print('hello')", task_id=None, enabled_tools=["terminal"])
    finally:
        clear_session_vars(tokens)
        tt.clear_task_env_overrides(session_key)

    assert calls == [("print('hello')", None, ["terminal"])]
    assert '"remote":true' in result


def test_execute_code_remote_preserves_bound_ssh_cwd(monkeypatch):
    """execute_code internals must not leak temp/root cwd into SSH session cwd."""
    from tools import code_execution_tool as code_exec

    class FakeSSHEnv:
        def __init__(self):
            self.cwd = "/home/rex/work"
            self.calls = []

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, cwd="", timeout=None, **kwargs):
            effective_cwd = cwd or self.cwd
            self.calls.append({"command": command, "cwd": cwd, "effective_cwd": effective_cwd})

            # Mirror the BaseEnvironment contract enough for this regression:
            # commands that explicitly cd update the backend cwd to that dir;
            # otherwise the cwd argument/default becomes the persisted cwd.
            if command.startswith("cd /tmp/hermes_exec_"):
                sandbox = command.split(" && ", 1)[0].removeprefix("cd ")
                self.cwd = sandbox
                return {"output": "sandbox ran\n", "returncode": 0}
            self.cwd = effective_cwd

            if "command -v python3" in command:
                return {"output": "OK\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = FakeSSHEnv()
    monkeypatch.setattr(code_exec, "_get_or_create_env", lambda task_id: (env, "ssh"))
    monkeypatch.setattr(code_exec, "_load_config", lambda: {"timeout": 30, "max_tool_calls": 5})

    result = code_exec._execute_remote("print('hello')", task_id="ssh-session", enabled_tools=[])

    assert '"status": "success"' in result
    assert env.cwd == "/home/rex/work"




def test_execute_code_remote_keeps_followup_relative_operations_in_bound_cwd(monkeypatch):
    """After execute_code, later relative terminal/file operations stay in the SSH cwd.

    This is the user-facing safety contract: execute_code internals may use
    /tmp or / internally, but a later relative operation must not suddenly run
    from /.  Otherwise a safe-looking relative command could target the wrong
    remote tree.
    """
    from tools import code_execution_tool as code_exec

    class FakeSSHEnv:
        def __init__(self):
            self.cwd = "/home/rex/work"
            self.calls = []

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, cwd="", timeout=None, **kwargs):
            effective_cwd = cwd or self.cwd
            self.calls.append({"command": command, "cwd": cwd, "effective_cwd": effective_cwd})
            if command.startswith("cd /tmp/hermes_exec_"):
                sandbox = command.split(" && ", 1)[0].removeprefix("cd ")
                self.cwd = sandbox
                return {"output": "sandbox ran\n", "returncode": 0}
            self.cwd = effective_cwd
            if "command -v python3" in command:
                return {"output": "OK\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = FakeSSHEnv()
    monkeypatch.setattr(code_exec, "_get_or_create_env", lambda task_id: (env, "ssh"))
    monkeypatch.setattr(code_exec, "_load_config", lambda: {"timeout": 30, "max_tool_calls": 5})

    result = code_exec._execute_remote("print('hello')", task_id="ssh-session", enabled_tools=[])
    followup = env.execute("rm -rf .trash/safe-relative-probe")

    assert '"status": "success"' in result
    assert followup["returncode"] == 0
    assert env.calls[-1]["command"] == "rm -rf .trash/safe-relative-probe"
    assert env.calls[-1]["effective_cwd"] == "/home/rex/work"
    assert env.cwd == "/home/rex/work"


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
